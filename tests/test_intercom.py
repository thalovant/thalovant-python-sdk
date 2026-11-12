"""Intercom: announce, announcements, speakers and settings, against a loopback fake API."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
    ThalovantIntercomDisabledError,
    ThalovantPlanError,
    ThalovantRateLimitedError,
)
from thalovant.intercom import UNSET, announce_body, speaker_update_body

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
    speaker = {"client_id": "c/1", "slug": "kitchen", "name": "Kitchen", "online": True, "last_seen_at": None,
               "intercom": {"receive": True, "room": "kitchen", "groups": ["downstairs"],
                            "dnd": {"enabled": True, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": None}}}}
    api.reply("GET", f"{BASE}/speakers", 200, {"items": [speaker]})
    api.reply("PATCH", f"{BASE}/speakers/c%2F1", 200, speaker)
    listed = control.list_intercom_speakers(HUB)
    assert listed.items[0].online and listed.items[0].intercom.dnd.quiet_hours == IntercomQuietHours("22:00", "07:00")
    control.update_intercom_speaker(HUB, "c/1", room=None, receive=True)
    patch = api.requests[-1]
    assert patch["path"] == f"{BASE}/speakers/c%2F1"  # the id is escaped, never a path segment of its own
    assert patch["body"] == {"receive": True, "room": None}
    control.update_intercom_speaker(HUB, "c/1", dnd=IntercomDnd(True, IntercomQuietHours("22:00", "07:00", "Europe/Paris")))
    assert api.requests[-1]["body"]["dnd"]["quiet_hours"]["timezone"] == "Europe/Paris"


def test_settings(api, control):
    api.reply("GET", f"{BASE}/settings", 200, {"enabled": False, "plan_allowed": False, "unavailable_reason": "plan"})
    api.reply("PUT", f"{BASE}/settings", 200, {"enabled": True, "plan_allowed": True, "unavailable_reason": None})
    assert control.get_intercom_settings(HUB).unavailable_reason == "plan"
    assert control.set_intercom_settings(HUB, True).enabled
    assert api.requests[-1]["body"] == {"enabled": True}


def test_the_hub_id_is_escaped(api, control):
    api.reply("GET", "/v1/hubs/a%2Fb%3Fc/intercom/settings", 200, {"enabled": False, "plan_allowed": True})
    assert control.get_intercom_settings("a/b?c").plan_allowed
    assert api.requests[0]["path"] == "/v1/hubs/a%2Fb%3Fc/intercom/settings"


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
