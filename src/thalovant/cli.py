"""Command line interface for the Thalovant SDK."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sys
from typing import Any, Sequence

from .client import ThalovantClient
from .identity import ThalovantIdentity


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        client = _client_from_args(args)
        with client:
            return args.handler(client, args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"thalovant: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thalovant",
        description="Inspect and interact with a Thalovant HiveMind hub.",
    )
    parser.add_argument(
        "-i",
        "--identity",
        help="Path to a Thalovant/HiveMind identity JSON file. Defaults to THALOVANT_* env vars.",
    )
    parser.add_argument("--host", help="Override identity default_master, e.g. https://hub.example.com.")
    parser.add_argument("--port", type=int, help="Override identity default_port.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Check transport health.")
    health.set_defaults(handler=_cmd_health)

    doctor = subparsers.add_parser("doctor", help="Run preflight diagnostics.")
    doctor.set_defaults(handler=_cmd_doctor)

    ask = subparsers.add_parser("ask", help="Send an utterance and wait for a reply.")
    ask.add_argument("text", help="Text to send to the hub.")
    ask.add_argument("--timeout", type=float, default=12.0)
    ask.add_argument("--lang", default="en-us")
    ask.add_argument("--session-id")
    ask.set_defaults(handler=_cmd_ask)

    listen = subparsers.add_parser("listen", help="Listen for hub events.")
    listen.add_argument("event", help="Event name, e.g. speak.")
    listen.add_argument("--timeout", type=float)
    listen.add_argument("--max-events", type=int)
    listen.add_argument("--session-id")
    listen.set_defaults(handler=_cmd_listen)

    emit = subparsers.add_parser("emit", help="Emit a low-level OVOS/HiveMind event.")
    emit.add_argument("event", help="Event name, e.g. recognizer_loop:utterance.")
    emit.add_argument("--data", default="{}", help="JSON event data.")
    emit.add_argument("--context", default="{}", help="JSON event context.")
    emit.set_defaults(handler=_cmd_emit)

    utter = subparsers.add_parser("utter", help="Send an utterance without waiting for a reply.")
    utter.add_argument("text", help="Text to send to the hub.")
    utter.add_argument("--lang", default="en-us")
    utter.add_argument("--session-id")
    utter.set_defaults(handler=_cmd_utter)

    return parser


def _client_from_args(args: argparse.Namespace) -> ThalovantClient:
    identity = (
        ThalovantIdentity.from_file(args.identity)
        if args.identity
        else ThalovantIdentity.from_env()
    )
    if args.host is not None:
        identity = replace(identity, default_master=args.host.rstrip("/"))
    if args.port is not None:
        identity = replace(identity, default_port=args.port)
    return ThalovantClient(identity)


def _cmd_health(client: ThalovantClient, args: argparse.Namespace) -> int:
    health = client.healthcheck()
    if args.json:
        print(json.dumps(health.as_dict(), indent=2, sort_keys=True))
    else:
        print("ok" if health.ok else "failed")
        print(f"connected={health.connected}")
        print(f"handshake_complete={health.handshake_complete}")
        print(f"transport_alive={health.transport_alive}")
        if health.last_error:
            print(f"last_error={health.last_error}")
    return 0 if health.ok else 1


def _cmd_doctor(client: ThalovantClient, args: argparse.Namespace) -> int:
    report = client.doctor()
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.format())
    return 0 if report.ok else 1


def _cmd_ask(client: ThalovantClient, args: argparse.Namespace) -> int:
    reply = client.ask(
        args.text,
        timeout=args.timeout,
        lang=args.lang,
        session_id=args.session_id,
    )
    if args.json:
        print(json.dumps(reply.as_dict(), indent=2, sort_keys=True))
    else:
        print(reply.text)
    return 0


def _cmd_listen(client: ThalovantClient, args: argparse.Namespace) -> int:
    for event in client.listen(
        args.event,
        timeout=args.timeout,
        max_events=args.max_events,
        session_id=args.session_id,
    ):
        if args.json:
            print(json.dumps(event.as_dict(), sort_keys=True))
        else:
            print(event.text or json.dumps(event.data, sort_keys=True))
    return 0


def _cmd_emit(client: ThalovantClient, args: argparse.Namespace) -> int:
    data = _json_object(args.data, "data")
    context = _json_object(args.context, "context")
    client.emit(args.event, data, context)
    print("sent")
    return 0


def _cmd_utter(client: ThalovantClient, args: argparse.Namespace) -> int:
    client.send_utterance(args.text, lang=args.lang, session_id=args.session_id)
    print("sent")
    return 0


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--{label} must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"--{label} must be a JSON object")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
