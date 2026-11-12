"""Intercom: announce, announcements, speakers and settings, against a loopback fake API."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time

import pytest

from thalovant import (
    Announcement,
    AsyncThalovantControlPlane,
    IntercomDnd,
    IntercomQuietHours,
    IntercomRepeat,
    IntercomTargets,
    ThalovantAPIError,
    ThalovantAuthError,
    ThalovantControlPlane,
    ThalovantHubUnreachableError,
    ThalovantAPIUnreachableError,
    ThalovantIntercomDisabledError,
    ThalovantNotCancellableError,
    ThalovantPlanError,
    ThalovantRateLimitedError,
)
from thalovant.control import INTERCOM_ANNOUNCE_MIN_TIMEOUT
from thalovant.intercom import UNSET, announce_body, intercom_id, speaker_update_body

HUB = "0f8a7c3e-2b1d-4c5e-9a6f-1d2c3b4a5e6f"
ANN = "7a1d6c1e-6f0b-4f43-9d7e-3f4f0d9a7c11"


def announcement(status="completed", **extra):
    body = {
        "id": ANN,
        "status": status,
        "priority": "normal",
        "created_at": "2026-11-11T10:00:00Z",
        "deliveries": [
            {"client_id": "c1", "slug": "kitchen", "name": "Kitchen", "status": "delivered", "at": "2026-11-11T10:00:01Z"},
            {"client_id": "c2", "slug": "den", "name": "Den", "status": "brand_new_status", "at": None},
        ],
        "acked_by": None,
    }
    body.update(extra)
    return body


class FakeApi:
    """A loopback server: ``responses`` maps (method, path) to a list of replies, the last one repeating."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[tuple[str, str], list[tuple[int, dict, dict]]] = {}
        self.delays: dict[tuple[str, str], float] = {}
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # pragma: no cover
                pass

            def _handle(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                api.requests.append({
                    "method": self.command, "path": path, "query": self.path.partition("?")[2],
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                })
                time.sleep(api.delays.get((self.command, path), 0.0))
                queue = api.responses.get((self.command, path), [])
                status, body, headers = queue[0] if len(queue) == 1 else queue.pop(0) if queue else (404, {"detail": "nope"}, {})
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def reply(self, method, path, status=200, body=None, headers=None, *, then=()):
        self.responses[(method, path)] = [(status, body or {}, headers or {}), *then]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def api():
    fake = FakeApi()
    yield fake
    fake.close()


@pytest.fixture()
def control(api):
    plane = ThalovantControlPlane(api_url=api.url, access_token="tok")
    yield plane
    plane.close()


BASE = f"/v1/hubs/{HUB}/intercom"


def test_announce_normal_sends_the_contract_body_and_reads_deliveries(api, control):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    result = control.announce(HUB, "  Dinner is ready  ", IntercomTargets.for_rooms("kitchen"), lang="en-US")
    request = api.requests[0]
    assert request["body"] == {"text": "Dinner is ready", "targets": {"rooms": ["kitchen"]},
                               "priority": "normal", "lang": "en-US"}
    assert request["headers"]["authorization"] == "Bearer tok"
    assert "idempotency-key" not in request["headers"]
    assert isinstance(result, Announcement) and result.status == "completed"
    assert [d.status for d in result.deliveries] == ["delivered", "brand_new_status"]
    assert result.as_dict()["deliveries"][0]["slug"] == "kitchen"
    assert not result.sending


def test_announce_urgent_repeat_idempotency_and_mapping_targets(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    result = control.announce(HUB, "Wake up", {"all": True}, priority="urgent",
                              repeat=IntercomRepeat(3, 10), idempotency_key="key-1")
    request = api.requests[0]
    assert request["body"]["repeat"] == {"times": 3, "interval_seconds": 10}
    assert request["body"]["targets"] == {"all": True}
    assert request["headers"]["idempotency-key"] == "key-1"
    assert result.sending and result.priority == "urgent"


def test_announce_wait_polls_until_it_completes(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("sending"),
              then=[(200, announcement("completed", acked_by="Den"), {})])
    result = control.announce(HUB, "Wake up", {"groups": ["kids"]}, priority="urgent",
                              repeat={"times": 2, "interval_seconds": 5}, wait=True, poll_interval=0.01)
    assert result.status == "completed" and result.acked_by == "Den"
    assert sum(r["method"] == "GET" for r in api.requests) == 2


def test_announce_wait_timeout_returns_the_latest_answer(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("sending"))
    result = control.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                              wait=True, wait_timeout=0.05, poll_interval=0.01)
    assert result.sending


def test_get_and_list_announcements(api, control):
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement())
    api.reply("GET", f"{BASE}/announcements", 200, {"items": [announcement()], "next_cursor": "c2"})
    assert control.get_announcement(HUB, ANN).id == ANN
    page = control.list_announcements(HUB, limit=5, cursor="c1")
    assert page.next_cursor == "c2" and len(page.items) == 1
    assert api.requests[-1]["query"] in ("limit=5&cursor=c1", "cursor=c1&limit=5")
    api.reply("GET", f"{BASE}/announcements", 200, {"items": []})
    assert control.list_announcements(HUB).next_cursor is None


def test_speakers_and_the_patch_sends_only_what_is_passed(api, control):
    speaker = {"client_id": "c-1", "slug": "kitchen", "name": "Kitchen", "online": True, "last_seen_at": None,
               "intercom": {"receive": True, "room": "kitchen", "groups": ["downstairs"],
                            "dnd": {"enabled": True, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": None}}}}
    api.reply("GET", f"{BASE}/speakers", 200, {"items": [speaker]})
    api.reply("PATCH", f"{BASE}/speakers/c-1", 200, speaker)
    listed = control.list_intercom_speakers(HUB)
    assert listed.items[0].online and listed.items[0].intercom.dnd.quiet_hours == IntercomQuietHours("22:00", "07:00")
    control.update_intercom_speaker(HUB, "c-1", room=None, receive=True)
    patch = api.requests[-1]
    assert patch["path"] == f"{BASE}/speakers/c-1"
    assert patch["body"] == {"receive": True, "room": None}
    control.update_intercom_speaker(HUB, "c-1", dnd=IntercomDnd(True, IntercomQuietHours("22:00", "07:00", "Europe/Paris")))
    assert api.requests[-1]["body"]["dnd"]["quiet_hours"]["timezone"] == "Europe/Paris"


def test_settings(api, control):
    api.reply("GET", f"{BASE}/settings", 200, {"enabled": False, "plan_allowed": False, "unavailable_reason": "plan"})
    api.reply("PUT", f"{BASE}/settings", 200, {"enabled": True, "plan_allowed": True, "unavailable_reason": None})
    assert control.get_intercom_settings(HUB).unavailable_reason == "plan"
    assert control.set_intercom_settings(HUB, True).enabled
    assert api.requests[-1]["body"] == {"enabled": True}


def test_an_id_is_one_percent_encoded_path_segment(api, control):
    api.reply("GET", "/v1/hubs/a%20b%25c/intercom/settings", 200, {"enabled": False, "plan_allowed": True})
    assert control.get_intercom_settings("a b%c").plan_allowed
    assert api.requests[0]["path"] == "/v1/hubs/a%20b%25c/intercom/settings"


UNSAFE_IDS = [".", "..", "", "   ", "a/b", "/", "a?b", "a#b", "a\\b", "a\x00b", "a\nb", "a\tb", "a\x7fb", "a\x85b", "../x", "x/.."]


@pytest.mark.parametrize("bad", UNSAFE_IDS)
def test_dot_segments_and_unsafe_ids_are_refused_before_any_request(api, control, bad):
    # A hub id, a client id and an announcement id each become one path segment; a dot segment
    # ("." or "..") would be collapsed by a URL resolver into another route, so none is ever sent.
    calls = [
        lambda: control.get_intercom_settings(bad),
        lambda: control.set_intercom_settings(bad, True),
        lambda: control.list_intercom_speakers(bad),
        lambda: control.update_intercom_speaker(bad, "c1", receive=True),
        lambda: control.update_intercom_speaker(HUB, bad, receive=True),
        lambda: control.announce(bad, "hi", {"all": True}),
        lambda: control.get_announcement(bad, ANN),
        lambda: control.get_announcement(HUB, bad),
        lambda: control.cancel_announcement(bad, ANN),
        lambda: control.cancel_announcement(HUB, bad),
        lambda: control.list_announcements(bad),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert api.requests == []


def test_dot_segment_ids_are_refused_on_the_async_surface(api):
    async def refuse(plane, bad):
        for call in (
            lambda: plane.announce(bad, "hi", {"all": True}),
            lambda: plane.get_announcement(HUB, bad),
            lambda: plane.cancel_announcement(HUB, bad),
            lambda: plane.update_intercom_speaker(HUB, bad, receive=True),
        ):
            with pytest.raises(ValueError):
                await call()

    async def run():
        plane = AsyncThalovantControlPlane(api_url=api.url, access_token="tok")
        try:
            for bad in (".", "..", "", "a/b"):
                await refuse(plane, bad)
        finally:
            await plane.aclose()

    asyncio.run(run())
    assert api.requests == []


def test_ordinary_ids_pass_the_id_check():
    for good in (HUB, ANN, "client-1", "c_1", "a.b", "...", "a..b", ".hidden", "name with space", "x" * 200):
        assert intercom_id(good, "id") == good
    for bad in (None, 5, b"x", ["a"]):
        with pytest.raises(ValueError):
            intercom_id(bad, "id")


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind"),
    [
        (403, {"code": "intercom_disabled", "detail": "off"}, {}, ThalovantIntercomDisabledError),
        (422, {"code": "intercom_disabled", "detail": "kid hub"}, {}, ThalovantIntercomDisabledError),
        (502, {"code": "hub_unreachable", "detail": "no hub"}, {}, ThalovantHubUnreachableError),
        (429, {"code": "rate_limited", "detail": "slow"}, {"Retry-After": "30"}, ThalovantRateLimitedError),
        (403, {"code": "plan_limit", "detail": "plan"}, {}, ThalovantPlanError),
        (402, {"detail": "API access requires a paid plan."}, {}, ThalovantPlanError),
        (403, {"detail": "Insufficient scopes"}, {}, ThalovantAuthError),
        (422, {"code": "validation", "detail": "bad"}, {}, ThalovantAPIError),
    ],
)
def test_errors_are_typed(api, control, status, body, headers, kind):
    api.reply("POST", f"{BASE}/announce", status, body, headers)
    with pytest.raises(kind) as caught:
        control.announce(HUB, "hello", {"all": True})
    assert type(caught.value) is kind
    assert caught.value.status_code == status
    if "code" in body:
        assert caught.value.code == body["code"]
    if status == 429:
        assert caught.value.retry_after_seconds == 30


def test_nothing_is_sent_for_invalid_input(api, control):
    bad = [
        ("", {"all": True}), ("   ", {"all": True}), ("x" * 281, {"all": True}), ("a\x07b", {"all": True}), ("a\u2028b", {"all": True}),
        ("hi", {}), ("hi", {"all": True, "rooms": ["a"]}), ("hi", {"nobody": ["a"]}),
        ("hi", IntercomTargets.for_speakers(*[f"s{i}" for i in range(51)])),
    ]
    for text, targets in bad:
        with pytest.raises(ValueError):
            control.announce(HUB, text, targets)
    with pytest.raises(ValueError):
        control.announce(HUB, "hi", {"all": True}, repeat=IntercomRepeat(2, 10))  # normal cannot repeat
    for repeat in (IntercomRepeat(0, 10), IntercomRepeat(11, 10), IntercomRepeat(2, 4), IntercomRepeat(2, 121)):
        with pytest.raises(ValueError):
            control.announce(HUB, "hi", {"all": True}, priority="urgent", repeat=repeat)
    with pytest.raises(ValueError):
        control.announce(HUB, "hi", {"all": True}, priority="loud")
    with pytest.raises(ValueError):
        control.announce("  ", "hi", {"all": True})
    with pytest.raises(ValueError):
        control.get_announcement(HUB, "")
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1")
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd={"enabled": True, "quiet_hours": {"start": "25:00", "end": "07:00"}})
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", groups=["g"] * 9)
    with pytest.raises(ValueError):
        control.set_intercom_settings(HUB, "yes")
    assert api.requests == []


@pytest.mark.parametrize("wait_timeout", ["5", True, float("nan"), -1, float("inf"), float("-inf"), [1]])
def test_a_bad_wait_timeout_is_refused_before_the_post(api, control, wait_timeout):
    with pytest.raises(ValueError, match="wait_timeout"):
        control.announce(HUB, "hi", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                         wait=True, wait_timeout=wait_timeout)
    assert api.requests == []  # nothing was announced


@pytest.mark.parametrize("poll_interval", ["2", True, float("nan"), 0, -1, float("inf")])
def test_a_bad_poll_interval_is_refused_before_the_post(api, control, poll_interval):
    with pytest.raises(ValueError, match="poll_interval"):
        control.announce(HUB, "hi", {"all": True}, wait=True, poll_interval=poll_interval)
    assert api.requests == []


def test_a_zero_wait_timeout_returns_the_sending_answer_without_polling(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    result = control.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                              wait=True, wait_timeout=0)
    assert result.sending
    assert [r["method"] for r in api.requests] == ["POST"]


def test_the_wait_deadline_bounds_a_stalled_status_read(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("completed"))
    api.delays[("GET", f"{BASE}/announcements/{ANN}")] = 5.0  # a read that never answers in time
    started = time.monotonic()
    result = control.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                              wait=True, wait_timeout=0.4, poll_interval=0.01)
    assert result.sending and result.id == ANN  # the latest answer, not an error and not a hang
    assert time.monotonic() - started < 3.0


def test_no_read_starts_once_the_deadline_passed_during_the_sleep(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("completed"))
    started = time.monotonic()
    result = control.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                              wait=True, wait_timeout=0.05, poll_interval=30.0)
    assert result.sending  # the sleep was cut to the deadline, and no read followed it
    assert [r["method"] for r in api.requests] == ["POST"]
    assert time.monotonic() - started < 3.0


def test_a_failed_poll_surfaces_after_the_post_was_accepted(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 403, {"detail": "Insufficient scopes"})
    with pytest.raises(ThalovantAuthError) as caught:
        control.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                         idempotency_key="k-1", wait=True, poll_interval=0.01)
    assert caught.value.status_code == 403
    assert [r["method"] for r in api.requests] == ["POST", "GET"]  # accepted, then the poll failed
    assert api.requests[0]["headers"]["idempotency-key"] == "k-1"


@pytest.mark.parametrize(
    "targets",
    [
        {"speakers": "ab"}, {"rooms": "kitchen"}, {"groups": "kids"}, {"speakers": b"ab"},
        {"speakers": 5}, {"speakers": {"a": 1}}, {"speakers": {"a", "b"}}, {"speakers": ["a", 3]},
        {"all": "yes"}, {"all": 1}, {"all": "true", "speakers": ["a"]},
        IntercomTargets(speakers="ab"), IntercomTargets(rooms="kitchen"), IntercomTargets(all="yes"),
    ],
)
def test_scalar_or_wrong_typed_targets_are_refused_not_split(api, control, targets):
    with pytest.raises(ValueError):
        control.announce(HUB, "hi", targets)
    assert api.requests == []


def test_targets_accept_real_sequences_of_names():
    assert announce_body("hi", {"speakers": ["ab"]})["targets"] == {"speakers": ["ab"]}
    assert announce_body("hi", {"rooms": ("a", "b")})["targets"] == {"rooms": ["a", "b"]}
    assert announce_body("hi", IntercomTargets(groups=["g"]))["targets"] == {"groups": ["g"]}
    assert announce_body("hi", IntercomTargets.for_speakers("ab"))["targets"] == {"speakers": ["ab"]}
    assert announce_body("hi", {"all": True})["targets"] == {"all": True}


@pytest.mark.parametrize(
    "dnd",
    [
        {"enabled": "true"}, {"enabled": "false"}, {"enabled": 1}, {"enabled": 0}, {"enabled": None},
        {}, {"quiet_hours": {"start": "22:00", "end": "07:00"}},  # enabled missing: never stored as false
        IntercomDnd(enabled="true"), IntercomDnd(enabled=1),  # type: ignore[arg-type]
    ],
)
def test_an_invalid_dnd_enabled_is_refused_never_turned_into_false(api, control, dnd):
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd=dnd)
    assert api.requests == []


def test_dnd_with_a_real_boolean_is_sent_as_given():
    assert speaker_update_body(dnd={"enabled": True})["dnd"] == {"enabled": True}
    assert speaker_update_body(dnd={"enabled": False})["dnd"] == {"enabled": False}
    body = speaker_update_body(dnd={"enabled": True, "quiet_hours": {"start": "21:00", "end": "06:30"}})
    assert body["dnd"] == {"enabled": True, "quiet_hours": {"start": "21:00", "end": "06:30", "timezone": None}}


def test_groups_must_be_a_list_not_a_scalar():
    for groups in ("kids", b"kids", 5):
        with pytest.raises(ValueError):
            speaker_update_body(groups=groups)  # type: ignore[arg-type]


def test_body_builders_and_unset():
    assert announce_body("hi", {"speakers": ["a", "b"]})["targets"] == {"speakers": ["a", "b"]}
    assert speaker_update_body(room="x" * 64)["room"] == "x" * 64
    with pytest.raises(ValueError):
        speaker_update_body(room="x" * 65)
    assert repr(UNSET) == "UNSET" and not UNSET


def test_malformed_answers_are_api_errors(api, control):
    api.reply("POST", f"{BASE}/announce", 200, {"status": "completed"})
    with pytest.raises(ThalovantAPIError):
        control.announce(HUB, "hi", {"all": True})
    api.reply("GET", f"{BASE}/announcements", 200, {"nope": 1})
    with pytest.raises(ThalovantAPIError):
        control.list_announcements(HUB)


def test_the_async_surface(api):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    api.reply("GET", f"{BASE}/speakers", 200, {"items": []})

    async def run():
        plane = AsyncThalovantControlPlane(api_url=api.url, access_token="tok")
        try:
            return await plane.announce(HUB, "hi", IntercomTargets.everyone()), await plane.list_intercom_speakers(HUB)
        finally:
            await plane.aclose()

    sent, speakers = asyncio.run(run())
    assert sent.status == "completed" and speakers.items == ()


# --- Amendment 2: cancel, replay, timeout, 502 announcement id, docs rules ----------------------------------

def test_cancel_posts_to_the_cancel_route_and_reads_hub_confirmed(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 200, announcement("cancelled", priority="urgent", hub_confirmed=True))
    result = control.cancel_announcement(HUB, ANN)
    request = api.requests[0]
    assert request["method"] == "POST" and request["path"] == f"{BASE}/announcements/{ANN}/cancel"
    assert request["body"] is None  # no body: the id in the path is all the API needs
    assert request["headers"]["authorization"] == "Bearer tok"
    assert isinstance(result, Announcement) and result.status == "cancelled" and not result.sending
    assert result.priority == "urgent" and result.hub_confirmed is True
    assert result.as_dict()["hub_confirmed"] is True


@pytest.mark.parametrize(("sent", "expected"), [(False, False), (True, True), ("yes", None), (1, None), (None, None)])
def test_hub_confirmed_is_a_real_boolean_or_unknown(api, control, sent, expected):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 200, announcement("cancelled", hub_confirmed=sent))
    assert control.cancel_announcement(HUB, ANN).hub_confirmed is expected


def test_hub_confirmed_is_optional_on_every_other_answer(api, control):
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement())  # no hub_confirmed key at all
    got = control.get_announcement(HUB, ANN)
    assert got.hub_confirmed is None
    assert Announcement.from_dict({"id": "a", "status": "cancelled"}).hub_confirmed is None


def test_a_cancel_the_hub_could_not_confirm_is_still_a_cancelled_announcement(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 200, announcement("cancelled", hub_confirmed=False))
    result = control.cancel_announcement(HUB, ANN)
    assert result.status == "cancelled" and result.hub_confirmed is False


def test_cancel_of_a_finished_announcement_is_a_typed_409(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 409, {"code": "not_cancellable", "detail": "It already completed."})
    with pytest.raises(ThalovantNotCancellableError) as caught:
        control.cancel_announcement(HUB, ANN)
    assert type(caught.value) is ThalovantNotCancellableError and isinstance(caught.value, ThalovantAPIError)
    assert caught.value.status_code == 409 and caught.value.code == "not_cancellable"


def test_another_409_is_not_not_cancellable(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 409, {"code": "something_else", "detail": "no"})
    with pytest.raises(ThalovantAPIError) as caught:
        control.cancel_announcement(HUB, ANN)
    assert type(caught.value) is ThalovantAPIError


def test_cancel_without_the_scope_is_an_auth_error(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 403, {"detail": "Insufficient scopes"})
    with pytest.raises(ThalovantAuthError):
        control.cancel_announcement(HUB, ANN)


def test_cancel_on_the_async_surface(api):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 200, announcement("cancelled"))

    async def run():
        plane = AsyncThalovantControlPlane(api_url=api.url, access_token="tok")
        try:
            return await plane.cancel_announcement(HUB, ANN)
        finally:
            await plane.aclose()

    assert asyncio.run(run()).status == "cancelled"


def test_the_cancelled_status_is_in_the_status_type():
    from typing import get_args

    from thalovant import AnnouncementStatus

    assert "cancelled" in get_args(AnnouncementStatus)


def test_a_502_names_the_announcement_the_api_recorded(api, control):
    api.reply("POST", f"{BASE}/announce", 502, {"code": "hub_unreachable", "detail": "no hub", "announcement_id": ANN})
    with pytest.raises(ThalovantHubUnreachableError) as caught:
        control.announce(HUB, "hello", {"all": True}, idempotency_key="k-1")
    assert caught.value.status_code == 502 and caught.value.announcement_id == ANN
    assert caught.value.problem["announcement_id"] == ANN


def test_a_502_announcement_id_inside_a_detail_object_is_read_too(api, control):
    api.reply("POST", f"{BASE}/announce", 502,
              {"detail": {"code": "hub_unreachable", "detail": "no hub", "announcement_id": ANN}})
    with pytest.raises(ThalovantHubUnreachableError) as caught:
        control.announce(HUB, "hello", {"all": True})
    assert caught.value.announcement_id == ANN


@pytest.mark.parametrize("body", [
    {"code": "hub_unreachable", "detail": "no hub"},
    {"code": "hub_unreachable", "detail": "no hub", "announcement_id": 7},
    {"code": "hub_unreachable", "detail": "no hub", "announcement_id": ""},
])
def test_a_502_without_a_usable_announcement_id_has_none(api, control, body):
    api.reply("POST", f"{BASE}/announce", 502, body)
    with pytest.raises(ThalovantHubUnreachableError) as caught:
        control.announce(HUB, "hello", {"all": True})
    assert caught.value.announcement_id is None
    assert ThalovantHubUnreachableError("x").announcement_id is None


def test_a_replayed_failed_announcement_is_returned_as_failed_and_the_key_is_sent_each_time(api, control):
    # The API replays a stored announcement for the same (hub, key) with Idempotency-Replayed: true and
    # never sends again: a replayed `failed` stays `failed`, and the SDK neither raises nor retries.
    api.reply("POST", f"{BASE}/announce", 200, announcement("failed"), {"Idempotency-Replayed": "true"})
    first = control.announce(HUB, "Dinner is ready", {"all": True}, idempotency_key="11111111-1111-4111-8111-111111111111")
    again = control.announce(HUB, "Dinner is ready", {"all": True}, idempotency_key="11111111-1111-4111-8111-111111111111")
    assert first.status == again.status == "failed"
    assert [r["headers"]["idempotency-key"] for r in api.requests] == ["11111111-1111-4111-8111-111111111111"] * 2
    assert len(api.requests) == 2  # one POST per call: nothing is retried behind the caller's back


def test_announce_never_uses_less_than_the_hub_wait_even_with_a_short_client_timeout(api):
    # The API waits up to 10 s for the hub; a 0.3 s client timeout must not abandon a slower success.
    api.delays[("POST", f"{BASE}/announce")] = 0.6
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement())
    plane = ThalovantControlPlane(api_url=api.url, access_token="tok", timeout=0.3)
    try:
        assert plane.announce(HUB, "hi", {"all": True}).status == "completed"
        api.delays[("GET", f"{BASE}/announcements/{ANN}")] = 0.6
        with pytest.raises(ThalovantAPIUnreachableError):  # the ordinary calls still honour the short timeout
            plane.get_announcement(HUB, ANN)
    finally:
        plane.close()


@pytest.mark.parametrize(("configured", "expected"), [(10.0, 25.0), (0.5, 25.0), (25.0, 25.0), (60.0, 60.0)])
def test_the_announce_timeout_is_at_least_twenty_seconds(api, configured, expected):
    assert INTERCOM_ANNOUNCE_MIN_TIMEOUT >= 20.0
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement())
    seen: dict[str, float] = {}

    async def run():
        plane = AsyncThalovantControlPlane(api_url=api.url, access_token="tok", timeout=configured)
        real = plane._sender.send

        async def spy(method, url, **kwargs):
            seen[method] = kwargs["timeout"]
            return await real(method, url, **kwargs)

        plane._sender.send = spy  # type: ignore[method-assign]
        try:
            await plane.announce(HUB, "hi", {"all": True})
            await plane.get_announcement(HUB, ANN)
        finally:
            await plane.aclose()

    asyncio.run(run())
    assert seen["POST"] == expected  # announce: never below the floor
    assert seen["GET"] == configured  # every other call keeps the client's own timeout


def test_the_default_client_timeout_is_unchanged():
    assert ThalovantControlPlane(access_token="t").timeout == 10.0


ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("**/*.md"))]


@pytest.mark.skipif(not (ROOT / "README.md").is_file(), reason="the docs are not shipped with this install")
def test_no_doc_or_example_uses_a_constant_idempotency_key():
    # A fixed key replays the stored announcement for 24 hours and sends nothing: examples must
    # generate a fresh one per intended announcement, never a literal like "dinner-2026-11-11".
    offenders = []
    for path in DOC_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"idempotency_key\s*=\s*[\"']", line) and "create_hub" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []


@pytest.mark.skipif(not (ROOT / "README.md").is_file(), reason="the docs are not shipped with this install")
def test_the_intercom_docs_do_not_over_promise():
    text = "\n".join(path.read_text(encoding="utf-8") for path in [ROOT / "README.md", ROOT / "docs/guides/intercom.md"])
    lowered = text.lower()
    for phrase in ("nothing listens", "never listens", "spoken once", "stops every announcement at once", "retry with the same"):
        assert phrase not in lowered
    assert "uuid" in lowered and "idempotency-replayed" in lowered and "cancel_announcement" in lowered
