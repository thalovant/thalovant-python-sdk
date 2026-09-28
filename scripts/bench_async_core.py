"""Measure two SDK builds against one local hub, on the same machine.

    python scripts/bench_async_core.py compare --baseline dist-0.8.7.whl --candidate dist-0.9.0.whl

``compare`` builds a fresh virtual environment for each wheel with uv, starts
the in-process hub from ``tests/fake_hub.py`` in this interpreter (which needs
this checkout's ``src`` and its test dependencies), and runs the same client
measurements in each environment, one after the other:

- import time of ``import thalovant``, less the interpreter's own start-up;
- first connection (XXpsk2 with the argon2id key) and a pinned reconnection
  (KKpsk0), each a fresh client;
- a question answered, on a connected client;
- a question asked the moment the hub drops the link, reconnection included;
- how long a client nobody calls takes to dial back after a drop;
- CPU and resident memory while a connected client sits idle;
- the installed distributions and their size on disk.

``hub`` and ``client`` are the two halves ``compare`` runs as subprocesses.
The client half uses only the public sync API that both builds share.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = "bench-result: "


# -- the hub -------------------------------------------------------------------


def run_hub(out: Path) -> None:
    import asyncio
    import contextlib
    import signal

    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from fake_hub import FakeHub, speak_back

    async def main() -> None:
        hub = FakeHub()
        hub.responder = speak_back
        hub.expected_node_payload = None  # both builds' offers are valid
        await hub.start()
        identity = hub.identity(hub.register())

        async def drop(_request: web.Request) -> web.Response:
            await hub.drop_all()
            return web.Response(text="dropped")

        async def unpin(_request: web.Request) -> web.Response:
            # A hub pins a client's static key on first contact; a first
            # connection from a fresh state directory needs the pin cleared.
            for record in hub.clients.values():
                record.pinned_key = None
            return web.Response(text="unpinned")

        async def sessions(_request: web.Request) -> web.Response:
            return web.Response(text=str(len(hub.sessions)))

        app = web.Application()
        app.router.add_get("/drop", drop)
        app.router.add_get("/unpin", unpin)
        app.router.add_get("/sessions", sessions)
        control = TestServer(app, host="127.0.0.1")
        await control.start_server()
        written = out.with_suffix(".partial")
        written.write_text(json.dumps({
            "identity": identity.as_dict(include_secrets=True),
            "control": f"http://127.0.0.1:{control.port}",
        }))
        written.replace(out)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        await control.close()
        with contextlib.suppress(Exception):
            await hub.stop()

    asyncio.run(main())


# -- the client ----------------------------------------------------------------


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _cpu_seconds() -> float:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _median_ms(samples: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(samples) * 1000, 1),
        "min_ms": round(min(samples) * 1000, 1),
        "max_ms": round(max(samples) * 1000, 1),
        "n": len(samples),
    }


def run_client(identity_file: Path, control: str, rounds: int, idle_seconds: float) -> dict[str, object]:
    import threading
    import urllib.request

    rss_start = _rss_mb()
    import thalovant
    from thalovant import ThalovantClient, ThalovantIdentity

    rss_imported = _rss_mb()
    identity = ThalovantIdentity.from_file(identity_file)
    results: dict[str, object] = {"version": thalovant.__version__}

    def client(state: str) -> ThalovantClient:
        return ThalovantClient(identity, noise_state_dir=state, reply_settle_seconds=0.05)

    with tempfile.TemporaryDirectory() as scratch:
        cold: list[float] = []
        for index in range(max(3, rounds // 2)):
            urllib.request.urlopen(f"{control}/unpin", timeout=5).read()
            each = client(os.path.join(scratch, f"cold-{index}"))
            started = time.perf_counter()
            each.connect(timeout=15)
            cold.append(time.perf_counter() - started)
            each.close()
        results["connect_first_xx"] = _median_ms(cold)

        pinned = os.path.join(scratch, "pinned")
        urllib.request.urlopen(f"{control}/unpin", timeout=5).read()
        first = client(pinned)
        first.connect(timeout=15)  # pin the hub once
        first.close()
        warm: list[float] = []
        for _ in range(rounds):
            each = client(pinned)
            started = time.perf_counter()
            each.connect(timeout=15)
            warm.append(time.perf_counter() - started)
            each.close()
        results["connect_pinned_kk"] = _median_ms(warm)

        held = client(pinned)
        held.connect(timeout=15)
        held.ask("warm up", timeout=15)
        asks: list[float] = []
        for index in range(rounds):
            started = time.perf_counter()
            reply = held.ask(f"question {index}", timeout=15)
            asks.append(time.perf_counter() - started)
            assert reply.text == f"You said question {index}", reply.text
        results["ask_connected"] = _median_ms(asks)

        def hub(path: str) -> str:
            return urllib.request.urlopen(f"{control}/{path}", timeout=5).read().decode()

        recover: list[float] = []
        for index in range(max(3, rounds // 2)):
            dropped = time.perf_counter()
            hub("drop")
            reply = held.ask(f"after drop {index}", timeout=15)
            recover.append(time.perf_counter() - dropped)
            assert reply.text == f"You said after drop {index}", reply.text
        results["ask_right_after_drop"] = _median_ms(recover)

        # Nobody calls: how long until the client has dialled back by itself.
        redial: list[float] = []
        for _ in range(3):
            dropped = time.perf_counter()
            hub("drop")
            while hub("sessions") == "0" and time.perf_counter() - dropped < 30:
                time.sleep(0.01)
            redial.append(time.perf_counter() - dropped)
        results["back_unattended"] = _median_ms(redial)

        rss_connected = _rss_mb()
        threads = threading.active_count()
        cpu_before = _cpu_seconds()
        time.sleep(idle_seconds)
        idle_cpu = _cpu_seconds() - cpu_before
        results["idle"] = {
            "seconds": idle_seconds,
            "cpu_ms": round(idle_cpu * 1000, 1),
            "cpu_percent": round(idle_cpu / idle_seconds * 100, 3),
            "threads": threads,
            "rss_mb_start": round(rss_start, 1),
            "rss_mb_imported": round(rss_imported, 1),
            "rss_mb_connected": round(rss_connected, 1),
            "rss_mb_after_idle": round(_rss_mb(), 1),
        }
        held.close()
    return results


# -- the comparison ------------------------------------------------------------


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _environment(venv: Path, wheel: Path) -> None:
    subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(venv)], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(_python(venv)), str(wheel)], check=True)


def _footprint(venv: Path) -> dict[str, object]:
    code = (
        "import importlib.metadata as m, json, os, sysconfig\n"
        "roots = {sysconfig.get_paths()['purelib'], sysconfig.get_paths()['platlib']}\n"
        "size = sum(os.path.getsize(os.path.join(d, f)) for r in roots for d, _, fs in os.walk(r) for f in fs)\n"
        "names = sorted(d.metadata['Name'].lower() for d in m.distributions())\n"
        "print(json.dumps({'distributions': len(names), 'size_mb': round(size / 1e6, 1), 'names': names}))\n"
    )
    out = subprocess.run([str(_python(venv)), "-c", code], check=True, capture_output=True, text=True)
    return dict(json.loads(out.stdout))


def _import_ms(venv: Path, runs: int = 15) -> dict[str, float]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    def timed(code: str) -> list[float]:
        samples = []
        for _ in range(runs):
            started = time.perf_counter()
            subprocess.run([str(_python(venv)), "-c", code], check=True, env=env)
            samples.append(time.perf_counter() - started)
        return samples

    bare = statistics.median(timed("pass"))
    loaded = statistics.median(timed("import thalovant"))
    return {"median_ms": round((loaded - bare) * 1000, 1), "n": runs}


def compare(baseline: Path, candidate: Path, rounds: int, idle_seconds: float) -> dict[str, object]:
    report: dict[str, object] = {"python": sys.version.split()[0], "machine": os.uname().machine}
    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        hub_file = work / "hub.json"
        hub = subprocess.Popen([sys.executable, __file__, "hub", "--out", str(hub_file)])
        try:
            deadline = time.monotonic() + 30
            while not hub_file.exists():
                if hub.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("the hub did not start")
                time.sleep(0.05)
            hub_config = json.loads(hub_file.read_text())
            identity_file = work / "identity.json"
            identity_file.write_text(json.dumps(hub_config["identity"]))
            identity_file.chmod(0o600)  # the SDK refuses a readable identity file
            for label, wheel in (("baseline", baseline), ("candidate", candidate)):
                venv = work / label
                _environment(venv, wheel)
                measured = subprocess.run(
                    [str(_python(venv)), __file__, "client", "--identity", str(identity_file),
                     "--control", hub_config["control"], "--rounds", str(rounds),
                     "--idle-seconds", str(idle_seconds)],
                    check=True, stdout=subprocess.PIPE, text=True,
                )
                # 0.8.7's dependencies log to stdout; the result is the line marked.
                line = next(row for row in measured.stdout.splitlines() if row.startswith(RESULT))
                section = json.loads(line[len(RESULT):])
                section["import"] = _import_ms(venv)
                section["install"] = _footprint(venv)
                report[label] = section
        finally:
            hub.terminate()
            hub.wait(10)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    hub = commands.add_parser("hub")
    hub.add_argument("--out", type=Path, required=True)
    client = commands.add_parser("client")
    client.add_argument("--identity", type=Path, required=True)
    client.add_argument("--control", required=True)
    client.add_argument("--rounds", type=int, default=10)
    client.add_argument("--idle-seconds", type=float, default=20.0)
    both = commands.add_parser("compare")
    both.add_argument("--baseline", type=Path, required=True, help="the wheel to compare against")
    both.add_argument("--candidate", type=Path, required=True, help="the wheel being measured")
    both.add_argument("--rounds", type=int, default=10)
    both.add_argument("--idle-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if args.command == "hub":
        run_hub(args.out)
    elif args.command == "client":
        print(RESULT + json.dumps(run_client(args.identity, args.control, args.rounds, args.idle_seconds)))
    else:
        print(json.dumps(compare(args.baseline.resolve(), args.candidate.resolve(), args.rounds, args.idle_seconds),
                         indent=2))


if __name__ == "__main__":
    main()
