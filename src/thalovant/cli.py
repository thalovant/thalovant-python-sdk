"""Command line interface for the Thalovant SDK."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
import sys
from typing import Any, Sequence

from .client import ThalovantClient
from .control import (
    DEFAULT_CONTROL_API_URL,
    DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    HubSkillOperation,
    ThalovantControlPlane,
)
from .identity import ThalovantIdentity

#: Environment variables the control-plane subcommands read when the matching
#: option is not given. The names match the Node SDK and the MCP server.
API_URL_ENV = "THALOVANT_API_URL"
API_TOKEN_ENV = "THALOVANT_API_TOKEN"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if getattr(args, "control_plane", False):
            # ``skills`` talks to the Thalovant API, not to a hub: no identity
            # file is loaded and no hub connection is opened.
            return args.handler(_control_plane_from_args(args), args)
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

    intents = subparsers.add_parser("intents", help="List what the hub can be asked, per language.")
    intents.add_argument("--lang", action="append", dest="langs", metavar="LANG",
                         help="Language to list; repeat for several. Default en-us.")
    intents.add_argument("--timeout", type=float, default=5.0)
    intents.add_argument("--all", action="store_true",
                         help="Every sentence each intent answers to, not two.")
    intents.set_defaults(handler=_cmd_intents)

    utter = subparsers.add_parser("utter", help="Send an utterance without waiting for a reply.")
    utter.add_argument("text", help="Text to send to the hub.")
    utter.add_argument("--lang", default="en-us")
    utter.add_argument("--session-id")
    utter.set_defaults(handler=_cmd_utter)

    _add_skills_parser(subparsers)

    return parser


def _add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    """``thalovant skills ...``: manage the skills one hub carries, through the API."""

    skills = subparsers.add_parser(
        "skills",
        help="List, add, update, or remove the skills one hub carries (uses the Thalovant API).",
        description=(
            "Manage the skills of one hub through the Thalovant API. Needs an API "
            f"token ({API_TOKEN_ENV} or --token) with hubs:inspect for list and hubs:write "
            "for add, update, and remove. Changes apply live on the hub."
        ),
    )
    skills_commands = skills.add_subparsers(dest="skills_command", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.set_defaults(control_plane=True)
        parser.add_argument(
            "--hub", required=True, metavar="HUB_ID",
            help="Hub id (not slug). Hub-restricted tokens are honoured.",
        )
        parser.add_argument(
            "--api-url",
            default=None,
            help=f"Thalovant API URL. Defaults to ${API_URL_ENV} or {DEFAULT_CONTROL_API_URL}.",
        )
        parser.add_argument(
            "--token",
            default=None,
            help=f"API token. Defaults to ${API_TOKEN_ENV}. Prefer the variable: argv is visible to other processes.",
        )

    def waitable(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--wait",
            action="store_true",
            help="Poll until the change converges; failed operations exit with an error.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
            help=f"Seconds to wait with --wait. Default {DEFAULT_HUB_SKILL_WAIT_TIMEOUT:g}.",
        )

    listing = skills_commands.add_parser("list", help="List the hub's skills and their state.")
    common(listing)
    listing.set_defaults(handler=_cmd_skills_list)

    add = skills_commands.add_parser("add", help="Install a skill on the hub.")
    common(add)
    add.add_argument("skill", help="Skill id from the catalog, e.g. skill-weather.")
    add.add_argument("--version", default="latest", help='"latest" (default) or an exact x.y.z.')
    waitable(add)
    add.set_defaults(handler=_cmd_skills_add)

    update = skills_commands.add_parser("update", help="Move a hub skill to another version.")
    common(update)
    update.add_argument("skill", help="Skill id to update.")
    update.add_argument("--version", required=True, help='"latest" or an exact x.y.z.')
    waitable(update)
    update.set_defaults(handler=_cmd_skills_update)

    remove = skills_commands.add_parser("remove", help="Remove a skill from the hub.")
    common(remove)
    remove.add_argument("skill", help="Skill id to remove.")
    waitable(remove)
    remove.set_defaults(handler=_cmd_skills_remove)


def _control_plane_from_args(args: argparse.Namespace) -> ThalovantControlPlane:
    api_url = args.api_url or os.environ.get(API_URL_ENV) or DEFAULT_CONTROL_API_URL
    token = args.token or os.environ.get(API_TOKEN_ENV)
    if not token:
        raise ValueError(f"an API token is required: set {API_TOKEN_ENV} or pass --token")
    return ThalovantControlPlane(api_url, access_token=token)


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


def _cmd_intents(client: ThalovantClient, args: argparse.Namespace) -> int:
    inventory = client.intents(args.langs or None, timeout=args.timeout)
    if args.json:
        print(json.dumps(inventory.as_dict(), indent=2, sort_keys=True))
        return 0
    if inventory.denied:
        print(
            f"The hub refused {', '.join(inventory.denied)}; listing intent names only. "
            "Allow ovos.intent.list and ovos.intent.describe on this connection for the "
            "sentences.",
            file=sys.stderr,
        )
    for skill in inventory.skills:
        print(_plain(skill.skill_id))
        for intent in skill.intents:
            shown = False
            for lang in inventory.languages:
                examples = intent.examples(lang, 0 if args.all else 2)
                if not examples:
                    continue
                shown = True
                print(f"  {_plain(intent.name)} [{_plain(lang)}]")
                for text in examples:
                    print(f"    {_plain(text)}")
            if not shown:
                print(f"  {_plain(intent.name)}")
    return 0


def _plain(text: str) -> str:
    """Hub-provided text for a terminal: printable characters only."""

    return "".join(ch for ch in str(text) if ch.isprintable())


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


def _cmd_skills_list(api: ThalovantControlPlane, args: argparse.Namespace) -> int:
    listing = api.list_hub_skills(args.hub)
    if args.json:
        print(json.dumps(listing.as_dict(), indent=2, sort_keys=True))
        return 0
    if not listing.data:
        print("no skills on this hub")
        return 0
    for skill in listing.data:
        shown = skill.installed_version or skill.version or "-"
        line = f"{_plain(skill.skill)}\t{_plain(shown)}\t{_plain(skill.state)}"
        if skill.update_available and skill.latest_version:
            line += f"\tlatest={_plain(skill.latest_version)}"
        if not skill.active:
            line += "\tinactive"
        if skill.operator_last_error:
            line += f"\terror={_plain(skill.operator_last_error)}"
        print(line)
    return 0


def _cmd_skills_add(api: ThalovantControlPlane, args: argparse.Namespace) -> int:
    result = api.install_hub_skill(
        args.hub, args.skill, version=args.version, wait=args.wait, timeout=args.timeout
    )
    return _print_skill_operation(result, args)


def _cmd_skills_update(api: ThalovantControlPlane, args: argparse.Namespace) -> int:
    result = api.update_hub_skill(
        args.hub, args.skill, version=args.version, wait=args.wait, timeout=args.timeout
    )
    return _print_skill_operation(result, args)


def _cmd_skills_remove(api: ThalovantControlPlane, args: argparse.Namespace) -> int:
    result = api.remove_hub_skill(args.hub, args.skill, wait=args.wait, timeout=args.timeout)
    return _print_skill_operation(result, args)


def _print_skill_operation(result: HubSkillOperation, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0
    target = _plain(result.skill)
    if result.version:
        target += f"@{_plain(result.version)}"
    if result.operation is None:
        print(f"accepted: {_plain(result.state)} {target} (operation {_plain(result.operation_id)})")
    else:
        print(f"{_plain(result.state)}: {target}")
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
