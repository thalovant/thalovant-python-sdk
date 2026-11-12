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
    IntercomProblemCode,
    IntercomQuietHours,
    IntercomRepeat,
    IntercomTargets,
    ThalovantAPIError,
    ThalovantAuthError,
    ThalovantControlPlane,
    ThalovantHubLockedError,
    ThalovantHubUnreachableError,
    ThalovantAPIUnreachableError,
    ThalovantImpersonationNotAllowedError,
    ThalovantIntercomDisabledError,
    ThalovantIntercomNotConfiguredError,
    ThalovantIntercomPlatformDisabledError,
    ThalovantIntercomUnavailableError,
    ThalovantManagedClientError,
    ThalovantNotASpeakerError,
    ThalovantNotCancellableError,
    ThalovantPlanError,
    ThalovantRateLimitedError,
    ThalovantReservedNameError,
    ThalovantSkillNotInstalledError,
    ThalovantTooManyTargetsError,
    ThalovantUrgentBusyError,
    ThalovantUrgentScopeRequiredError,
)
from thalovant.control import INTERCOM_ANNOUNCE_MIN_TIMEOUT
from thalovant.intercom import UNSET, announce_body, announcement_text, idempotency_key, intercom_id, speaker_update_body

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


def test_the_wait_deadline_bounds_a_stalled_read_through_a_requests_style_session(api):
    # A caller's requests session blocks until the API answers; the read must not hold the
    # event loop, or asyncio.wait_for could not stop a five-second read at 0.4 s.
    requests = pytest.importorskip("requests")
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"))
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("completed"))
    api.delays[("GET", f"{BASE}/announcements/{ANN}")] = 2.5
    with requests.Session() as session:
        plane = ThalovantControlPlane(api_url=api.url, access_token="tok", session=session)
        try:
            started = time.monotonic()
            result = plane.announce(HUB, "x", {"all": True}, priority="urgent", repeat=IntercomRepeat(1, 5),
                                    wait=True, wait_timeout=0.4, poll_interval=0.01)
            elapsed = time.monotonic() - started
        finally:
            plane.close()
    assert result.sending and result.id == ANN  # the last answer at the deadline, not the completed one
    assert elapsed < 1.5  # the deadline (0.4 s), not the 2.5 s the read takes


def test_a_requests_style_session_is_read_off_the_event_loop_thread(api):
    requests = pytest.importorskip("requests")
    api.reply("GET", f"{BASE}/settings", 200, {"enabled": False, "plan_allowed": True, "unavailable_reason": None})
    seen: list[int] = []

    class Spy(requests.Session):
        def request(self, *args, **kwargs):
            seen.append(threading.get_ident())
            return super().request(*args, **kwargs)

    async def main():
        with Spy() as session:
            plane = AsyncThalovantControlPlane(api_url=api.url, access_token="tok", session=session)
            try:
                settings = await plane.get_intercom_settings(HUB)
            finally:
                await plane.aclose()
        return threading.get_ident(), settings

    loop_thread, settings = asyncio.run(main())
    assert settings.enabled is False
    assert seen and all(ident != loop_thread for ident in seen)


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
    assert speaker_update_body(dnd={"enabled": False})["dnd"] == {"enabled": False}
    body = speaker_update_body(dnd={"enabled": True, "quiet_hours": {"start": "21:00", "end": "06:30", "timezone": "America/Toronto"}})
    assert body["dnd"] == {"enabled": True, "quiet_hours": {"start": "21:00", "end": "06:30", "timezone": "America/Toronto"}}
    # Switched off, a window and a null zone are fine (the API stores them as they are).
    body = speaker_update_body(dnd={"enabled": False, "quiet_hours": {"start": "21:00", "end": "06:30"}})
    assert body["dnd"] == {"enabled": False, "quiet_hours": {"start": "21:00", "end": "06:30", "timezone": None}}


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


# --------------------------------------------------------------------------------------------------
# The final API (platform security review): scope, speaker model, problem codes, one validation rule.
# --------------------------------------------------------------------------------------------------

SPEAKER_DND = {"enabled": False, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": None}}


def speaker_row(**extra):
    row = {"client_id": "d8d4", "slug": "kitchen-speaker", "name": "Kitchen Speaker", "kind": "voice_satellite",
           "can_play": True, "online": False, "last_seen_at": None,
           "intercom": {"receive": True, "room": "kitchen", "groups": [], "dnd": SPEAKER_DND}}
    row.update(extra)
    return row


def test_the_speaker_list_reads_kind_can_play_and_policy_synced(api, control):
    api.reply("GET", f"{BASE}/speakers", 200, {
        "items": [speaker_row(), speaker_row(client_id="996f", slug="home-link", name="Home link", kind="home_assistant",
                                             can_play=False, intercom={"receive": False, "room": None, "groups": [], "dnd": SPEAKER_DND})],
        "unavailable_reason": None, "policy_synced": False})
    listed = control.list_intercom_speakers(HUB)
    assert listed.policy_synced is False and listed.unavailable_reason is None
    kitchen, link = listed.items
    assert (kitchen.kind, kitchen.can_play, kitchen.policy_synced) == ("voice_satellite", True, None)
    assert (link.kind, link.can_play) == ("home_assistant", False)
    assert listed.as_dict()["items"][1]["can_play"] is False and listed.as_dict()["policy_synced"] is False


def test_a_speaker_without_the_new_fields_keeps_the_apis_defaults(api, control):
    api.reply("GET", f"{BASE}/speakers", 200, {"items": [{"client_id": "c1"}]})
    only = control.list_intercom_speakers(HUB)
    assert only.items[0].kind == "voice_satellite" and only.items[0].can_play is True
    assert only.policy_synced is None and only.unavailable_reason is None
    # garbage is not trusted: only an explicit false says "cannot play"
    api.reply("GET", f"{BASE}/speakers", 200, {"items": [{"client_id": "c1", "kind": 7, "can_play": "no", "policy_synced": "yes"}]})
    odd = control.list_intercom_speakers(HUB).items[0]
    assert (odd.kind, odd.can_play, odd.policy_synced) == ("voice_satellite", True, None)


def test_the_speaker_patch_answer_carries_its_own_policy_synced(api, control):
    api.reply("PATCH", f"{BASE}/speakers/d8d4", 200, speaker_row(policy_synced=False))
    assert control.update_intercom_speaker(HUB, "d8d4", receive=True).policy_synced is False
    api.reply("PATCH", f"{BASE}/speakers/d8d4", 200, speaker_row(policy_synced=True))
    assert control.update_intercom_speaker(HUB, "d8d4", room="kitchen").policy_synced is True


def test_the_platform_reason_is_an_unavailable_reason(api, control):
    api.reply("GET", f"{BASE}/settings", 200, {"enabled": False, "plan_allowed": True, "unavailable_reason": "platform"})
    api.reply("GET", f"{BASE}/speakers", 200, {"items": [], "unavailable_reason": "platform", "policy_synced": True})
    assert control.get_intercom_settings(HUB).unavailable_reason == "platform"
    assert control.list_intercom_speakers(HUB).unavailable_reason == "platform"
    from typing import get_args

    from thalovant import IntercomUnavailableReason

    assert set(get_args(IntercomUnavailableReason)) == {"platform", "plan", "kid_hub", "public_hub"}


def test_a_delivery_keeps_its_detail_code():
    body = announcement(deliveries=[
        {"client_id": "c1", "slug": "k", "name": "K", "status": "failed", "detail": "hub_no_response", "at": None},
        {"client_id": "c2", "slug": "d", "name": "D", "status": "delivered", "detail": None, "at": None},
        {"client_id": "c3", "slug": "e", "name": "E", "status": "failed", "detail": "a_code_from_the_future", "at": None},
    ])
    parsed = Announcement.from_dict(body)
    assert [d.detail for d in parsed.deliveries] == ["hub_no_response", None, "a_code_from_the_future"]
    from thalovant.intercom import DELIVERY_DETAIL_CODES

    assert "hub_no_response" in DELIVERY_DETAIL_CODES and "a_code_from_the_future" not in DELIVERY_DETAIL_CODES
    assert parsed.as_dict()["deliveries"][0]["detail"] == "hub_no_response"


def test_the_scope_names_are_constants():
    from thalovant.intercom import INTERCOM_READ_SCOPE, INTERCOM_SEND_SCOPE, INTERCOM_URGENT_SCOPE

    assert (INTERCOM_READ_SCOPE, INTERCOM_SEND_SCOPE, INTERCOM_URGENT_SCOPE) == (
        "intercom:read", "intercom:send", "intercom:urgent")


# -- announce: replayed -----------------------------------------------------------------------


def test_a_replayed_announcement_says_so_and_a_first_answer_does_not(api, control):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    first = control.announce(HUB, "Dinner", {"all": True}, idempotency_key="k-1")
    assert first.replayed is False and first.as_dict()["replayed"] is False
    api.reply("POST", f"{BASE}/announce", 200, announcement(), {"Idempotency-Replayed": "true"})
    again = control.announce(HUB, "Dinner", {"all": True}, idempotency_key="k-1")
    assert again.replayed is True and again.status == "completed" and again.as_dict()["replayed"] is True


def test_the_replayed_header_is_read_in_any_case_and_only_when_true(api, control):
    for header, expected in ((("idempotency-replayed", "TRUE"), True), (("Idempotency-Replayed", "false"), False),
                             (("Idempotency-Replayed", "1"), False)):
        api.reply("POST", f"{BASE}/announce", 200, announcement(), {header[0]: header[1]})
        assert control.announce(HUB, "Dinner", {"all": True}).replayed is expected


def test_a_replayed_sending_announcement_is_202_and_stays_replayed_after_wait(api, control):
    api.reply("POST", f"{BASE}/announce", 202, announcement("sending", priority="urgent"), {"Idempotency-Replayed": "true"})
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement("completed", priority="urgent"))
    result = control.announce(HUB, "Wake up", {"all": True}, priority="urgent", repeat=IntercomRepeat(2, 5),
                              idempotency_key="k-2", wait=True, poll_interval=0.01)
    assert result.status == "completed" and result.replayed is True


def test_reads_and_cancel_are_never_replayed(api, control):
    api.reply("GET", f"{BASE}/announcements/{ANN}", 200, announcement(), {"Idempotency-Replayed": "true"})
    assert control.get_announcement(HUB, ANN).replayed is False


def test_an_idempotency_key_is_at_most_128_printable_ascii_characters(api, control):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    control.announce(HUB, "hi", {"all": True}, idempotency_key="k" * 128)
    assert len(api.requests[-1]["headers"]["idempotency-key"]) == 128
    sent = len(api.requests)
    for bad in ("k" * 129, "caf\u00e9", "a\r\nb", "", "   ", 7):
        with pytest.raises(ValueError):
            control.announce(HUB, "hi", {"all": True}, idempotency_key=bad)
    assert len(api.requests) == sent
    assert idempotency_key("0f8a7c3e-2b1d-4c5e-9a6f-1d2c3b4a5e6f").startswith("0f8a")


# -- the one text rule --------------------------------------------------------------------------

REFUSED_TEXT = [
    "a<b", "a>b", "<prosody>x</prosody>", "line\nbreak", "tab\there", "nul\x00 x", "bell\x07 x", "c1 \x85 next", "del\x7f x",
    "bidi \u202e override", "isolate \u2066 x", "zero width \u200b space", "bom \ufeff x", "word joiner \u2060 x",
    "tag \U000e0041 x", "private \ue000 x", "line \u2028 sep", "para \u2029 sep", "lone \ud800 surrogate",
]


@pytest.mark.parametrize("text", REFUSED_TEXT)
def test_a_text_the_api_would_refuse_is_refused_first_and_never_echoed(api, control, text):
    with pytest.raises(ValueError) as caught:
        control.announce(HUB, text, {"all": True})
    assert api.requests == []
    assert text not in str(caught.value) and text.strip() not in str(caught.value)


@pytest.mark.parametrize("text", [
    "Dinner is ready", "Caf\u00e9 cr\u00e8me", "\u00ab bonjour \u00bb", "family \U0001F468\u200d\U0001F469\u200d\U0001F467",
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", "no\u00a0break space inside", "a & b, c; d: e", "x" * 280,
])
def test_a_text_the_api_accepts_is_sent(api, control, text):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    control.announce(HUB, text, {"all": True})
    assert api.requests[-1]["body"]["text"] == text


def test_the_text_is_nfc_normalised_trimmed_and_counted_after_that():
    decomposed = "e\u0301" * 200  # 400 code points, 200 once composed
    assert announcement_text(decomposed) == "\u00e9" * 200
    assert announcement_text("  \u00a0 hi \u3000 ") == "hi"
    assert len(announcement_text("\u00e9" * 280)) == 280
    with pytest.raises(ValueError, match="at most 280"):
        announcement_text("\u00e9" * 281)
    with pytest.raises(ValueError, match="empty"):
        announcement_text(" \u00a0 ")
    with pytest.raises(ValueError, match="string"):
        announcement_text(b"bytes")  # type: ignore[arg-type]


def test_lang_is_a_language_tag(api, control):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    for good in ("en", "en-US", "fr-CA", "zho-Hant"):
        control.announce(HUB, "hi", {"all": True}, lang=good)
    sent = len(api.requests)
    for bad in ("", "e", "en_US", "english", "en-", "en-US-x-private-use-long", 5):
        with pytest.raises(ValueError):
            control.announce(HUB, "hi", {"all": True}, lang=bad)  # type: ignore[arg-type]
    assert len(api.requests) == sent


# -- targets --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["speakers", "rooms", "groups"])
def test_a_target_list_holds_at_most_50_names_of_at_most_64_characters(api, control, kind):
    api.reply("POST", f"{BASE}/announce", 200, announcement())
    control.announce(HUB, "hi", {kind: [f"n{i}" for i in range(50)]})
    control.announce(HUB, "hi", {kind: ["n" * 64]})
    sent = len(api.requests)
    with pytest.raises(ValueError, match="at most 50"):
        control.announce(HUB, "hi", {kind: [f"n{i}" for i in range(51)]})
    with pytest.raises(ValueError, match="at most 64"):
        control.announce(HUB, "hi", {kind: ["n" * 65]})
    assert len(api.requests) == sent


# -- do not disturb -------------------------------------------------------------------------------


def quiet(zone, start="22:00", end="07:00"):
    return {"enabled": True, "quiet_hours": {"start": start, "end": end, "timezone": zone}}


@pytest.mark.parametrize("dnd", [
    {"enabled": True},                                                  # no window
    {"enabled": True, "quiet_hours": None},
    quiet(None),                                                        # no zone: UTC would be a guess
    quiet("Mars/Phobos"), quiet("europe/paris"), quiet("Europe/Paris "), quiet(" Europe/Paris"), quiet(""), quiet("Europe/" + "x" * 60),
    quiet("Europe/Paris", "22:00", "22:00"),                            # an empty window is a mistake
    {"enabled": False, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "Mars/Phobos"}},  # any given zone is checked
    {"enabled": True, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "Europe/Paris", "extra": 1}},
    {"enabled": True, "quiet_hours": quiet("Europe/Paris")["quiet_hours"], "notify": True},
    {"enabled": True, "quiet_hours": "22:00-07:00"},
])
def test_an_unusable_do_not_disturb_is_refused_before_anything_is_sent(api, control, dnd):
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd=dnd)
    assert api.requests == []


def test_a_valid_do_not_disturb_is_sent_and_objects_follow_the_same_rule(api, control):
    api.reply("PATCH", f"{BASE}/speakers/c1", 200, speaker_row())
    for zone in ("Europe/Paris", "America/Argentina/Buenos_Aires", "UTC"):
        control.update_intercom_speaker(HUB, "c1", dnd=quiet(zone))
        assert api.requests[-1]["body"]["dnd"]["quiet_hours"]["timezone"] == zone
    control.update_intercom_speaker(HUB, "c1", dnd=IntercomDnd(True, IntercomQuietHours("23:00", "06:00", "Europe/Paris")))
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd=IntercomDnd(True, IntercomQuietHours("23:00", "06:00")))
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd=IntercomDnd(True))
    with pytest.raises(ValueError):
        control.update_intercom_speaker(HUB, "c1", dnd=IntercomDnd(True, IntercomQuietHours("23:00", "23:00", "Europe/Paris")))
    control.update_intercom_speaker(HUB, "c1", dnd=IntercomDnd(False))
    assert api.requests[-1]["body"]["dnd"] == {"enabled": False}


def test_the_zone_shape_is_the_fallback_only_where_there_is_no_timezone_database(monkeypatch):
    from thalovant import intercom as module

    def no_database(name):
        raise module.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(module, "ZoneInfo", no_database)
    module._timezone("Europe/Paris", "zone")        # unknown to this machine, well formed: the API decides
    module._timezone("Etc/GMT+5", "zone")
    for bad in ("Europe Paris", "Europe/Paris/..", "/x", "x/", ""):
        with pytest.raises(ValueError):
            module._timezone(bad, "zone")


def test_the_speaker_update_takes_only_the_documented_fields():
    import inspect

    params = set(inspect.signature(ControlPlaneForSignature.update_intercom_speaker).parameters)
    assert params == {"self", "hub_id", "client_id", "receive", "room", "groups", "dnd"}
    with pytest.raises(TypeError):
        ControlPlaneForSignature(access_token="t").update_intercom_speaker(HUB, "c1", label="x")  # type: ignore[call-arg]


ControlPlaneForSignature = ThalovantControlPlane


# -- the new problem codes ---------------------------------------------------------------------


def problem(status, code, message, **extra):
    return {"status": status, "code": code, "message": message, **extra}


CASES = [
    # (http status, code, class, extra members, attribute checks)
    (403, "urgent_scope_required", ThalovantUrgentScopeRequiredError, {}, {}),
    (429, "intercom_urgent_busy", ThalovantUrgentBusyError, {"scope": "hub", "retry_after_seconds": 30}, {"scope": "hub"}),
    (409, "skill_not_installed", ThalovantSkillNotInstalledError, {}, {}),
    (422, "not_a_speaker", ThalovantNotASpeakerError, {"kind": "home_assistant"}, {"kind": "home_assistant", "not_speakers": ()}),
    (422, "not_a_speaker", ThalovantNotASpeakerError, {"not_speakers": ["home-link", "other"]}, {"kind": None, "not_speakers": ("home-link", "other")}),
    (422, "reserved_name", ThalovantReservedNameError, {}, {}),
    (403, "hub_locked", ThalovantHubLockedError, {}, {}),
    (403, "impersonation_not_allowed", ThalovantImpersonationNotAllowedError, {}, {}),
    (422, "too_many_targets", ThalovantTooManyTargetsError, {"matched": 51}, {"matched": 51}),
    (422, "intercom_unavailable", ThalovantIntercomUnavailableError, {"reason": "kid_hub"}, {"reason": "kid_hub"}),
    (503, "intercom_disabled_platform", ThalovantIntercomPlatformDisabledError, {}, {}),
    (503, "intercom_not_configured", ThalovantIntercomNotConfiguredError, {}, {}),
    (403, "managed_client", ThalovantManagedClientError, {"refused_client_refs": ["client-1"]}, {"refused_client_refs": ("client-1",)}),
    (409, "not_cancellable", ThalovantNotCancellableError, {"announcement_status": "cancelled"}, {"announcement_status": "cancelled"}),
    (502, "hub_unreachable", ThalovantHubUnreachableError, {"announcement_id": ANN}, {"announcement_id": ANN}),
]


@pytest.mark.parametrize(("status", "code", "kind", "extra", "attrs"), CASES)
def test_each_new_problem_code_is_its_own_typed_error(api, control, status, code, kind, extra, attrs):
    api.reply("POST", f"{BASE}/announce", status, problem(status, code, "A sentence from the API.", **extra))
    with pytest.raises(kind) as caught:
        control.announce(HUB, "Dinner", {"all": True})
    error = caught.value
    assert type(error) is kind and error.status_code == status and error.code == code
    assert error.problem[code if False else "code"] == code
    for name, expected in attrs.items():
        assert getattr(error, name) == expected, name


@pytest.mark.parametrize(("status", "code", "kind", "extra", "attrs"), CASES)
def test_the_same_problems_inside_a_detail_object_are_read_too(api, control, status, code, kind, extra, attrs):
    api.reply("POST", f"{BASE}/announce", status, {"detail": {"code": code, "message": "A sentence.", **extra}})
    with pytest.raises(kind) as caught:
        control.announce(HUB, "Dinner", {"all": True})
    for name, expected in attrs.items():
        assert getattr(caught.value, name) == expected, name


def test_the_urgent_scope_code_wins_over_the_generic_insufficient_scopes_refusal(api, control):
    # A 403 that carries both the intercom code and the generic sentence is still the typed error
    # (a kind of ThalovantAuthError, so an existing handler for auth failures catches it too).
    api.reply("POST", f"{BASE}/announce", 403,
              {"status": 403, "code": "urgent_scope_required", "detail": "Insufficient scopes"})
    with pytest.raises(ThalovantUrgentScopeRequiredError) as caught:
        control.announce(HUB, "Dinner", {"all": True}, priority="urgent")
    assert isinstance(caught.value, ThalovantAuthError) and caught.value.status_code == 403


def test_a_top_level_null_or_wrong_type_does_not_hide_the_member_inside_detail(api, control):
    nested = {"code": "hub_unreachable", "announcement_id": "a-1"}
    for masked in (None, 5, ["x"], ""):
        api.reply("POST", f"{BASE}/announce", 502, {"announcement_id": masked, "detail": nested})
        with pytest.raises(ThalovantHubUnreachableError) as caught:
            control.announce(HUB, "Dinner", {"all": True})
        assert caught.value.announcement_id == "a-1", masked
    api.reply("POST", f"{BASE}/announce", 422,
              {"matched": None, "detail": {"code": "too_many_targets", "matched": 51}})
    with pytest.raises(ThalovantTooManyTargetsError) as many:
        control.announce(HUB, "Dinner", {"all": True})
    assert many.value.matched == 51
    api.reply("POST", f"{BASE}/announce", 422,
              {"not_speakers": None, "detail": {"code": "not_a_speaker", "not_speakers": ["home-link"]}})
    with pytest.raises(ThalovantNotASpeakerError) as speakers:
        control.announce(HUB, "Dinner", {"speakers": ["home-link"]})
    assert speakers.value.not_speakers == ("home-link",)


def test_the_typed_errors_keep_their_places_in_the_hierarchy():
    assert issubclass(ThalovantUrgentScopeRequiredError, ThalovantAuthError)          # sign in again with the scope
    assert issubclass(ThalovantUrgentBusyError, ThalovantRateLimitedError)            # still "a 429"
    assert issubclass(ThalovantIntercomUnavailableError, ThalovantIntercomDisabledError)
    assert issubclass(ThalovantIntercomPlatformDisabledError, ThalovantIntercomDisabledError)
    for kind in (ThalovantIntercomNotConfiguredError, ThalovantHubLockedError, ThalovantManagedClientError, ThalovantNotASpeakerError):
        assert issubclass(kind, ThalovantAPIError)


def test_the_urgent_busy_error_carries_the_retry_after(api, control):
    api.reply("POST", f"{BASE}/announce", 429,
              problem(429, "intercom_urgent_busy", "busy", scope="platform", retry_after_seconds=30), {"Retry-After": "30"})
    with pytest.raises(ThalovantUrgentBusyError) as caught:
        control.announce(HUB, "Wake", {"all": True}, priority="urgent", repeat=IntercomRepeat(5, 30))
    assert caught.value.retry_after_seconds == 30 and caught.value.scope == "platform"


def test_a_not_a_speaker_refusal_on_the_patch(api, control):
    api.reply("PATCH", f"{BASE}/speakers/996f", 422, problem(422, "not_a_speaker", "cannot play", kind="home_assistant"))
    with pytest.raises(ThalovantNotASpeakerError) as caught:
        control.update_intercom_speaker(HUB, "996f", receive=True)
    assert caught.value.kind == "home_assistant"


def test_a_problem_without_its_extra_members_has_none_of_them(api, control):
    api.reply("POST", f"{BASE}/announce", 422, problem(422, "too_many_targets", "too many", matched="51"))
    with pytest.raises(ThalovantTooManyTargetsError) as caught:
        control.announce(HUB, "hi", {"all": True})
    assert caught.value.matched is None                       # a string is not a count
    assert ThalovantIntercomUnavailableError("x").reason is None
    assert ThalovantNotASpeakerError("x").not_speakers == () and ThalovantNotCancellableError("x").announcement_status is None
    assert ThalovantUrgentBusyError("x").scope is None and ThalovantManagedClientError("x").refused_client_refs == ()


def test_the_problem_codes_are_constants_and_each_has_a_typed_error():
    codes = {v for k, v in vars(IntercomProblemCode).items() if k.isupper()}
    assert codes == {code for _, code, *_ in CASES} | {"intercom_disabled"}
    assert IntercomProblemCode.URGENT_SCOPE_REQUIRED == "urgent_scope_required"


def test_a_plain_403_insufficient_scopes_is_still_the_auth_error(api, control):
    api.reply("POST", f"{BASE}/announce", 403, {"detail": "Insufficient scopes"})
    with pytest.raises(ThalovantAuthError) as caught:
        control.announce(HUB, "hi", {"all": True})
    assert not isinstance(caught.value, ThalovantUrgentScopeRequiredError)


def test_the_cancel_409_names_the_current_status(api, control):
    api.reply("POST", f"{BASE}/announcements/{ANN}/cancel", 409,
              problem(409, "not_cancellable", "Only an announcement that is still sending can be cancelled.",
                      announcement_status="completed"))
    with pytest.raises(ThalovantNotCancellableError) as caught:
        control.cancel_announcement(HUB, ANN)
    assert caught.value.announcement_status == "completed"


def test_the_guides_name_the_urgent_scope_and_the_new_errors():
    text = (ROOT / "docs/guides/intercom.md").read_text(encoding="utf-8") if (ROOT / "docs/guides/intercom.md").is_file() else None
    if text is None:
        pytest.skip("the docs are not shipped with this install")
    for needle in ("intercom:urgent", "ThalovantUrgentScopeRequiredError", "policy_synced", "can_play", "replayed",
                   "ThalovantNotASpeakerError", "ThalovantTooManyTargetsError", "not_cancellable"):
        assert needle in text, needle
