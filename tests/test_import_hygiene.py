"""The SDK runs on aiohttp and cryptography alone.

Each check runs in a fresh interpreter with the libraries the SDK used to need
made unimportable, so an import that crept back in fails here rather than on a
device that installed the core alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent

BLOCKER = """
import sys
BLOCKED = {
    "hivemind_bus_client", "ovos_bus_client", "ovos_utils", "ovos_spec_tools", "langcodes",
    "requests", "urllib3", "yaml", "paho", "poorman_handshake", "websocket", "json_database",
    "thalovant_languages", "noise", "pycryptodome", "Cryptodome",
}

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} is not a core dependency")
        return None

sys.meta_path.insert(0, Block())
"""


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", BLOCKER + code],
        capture_output=True, text=True, timeout=120, cwd=str(TESTS),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_the_sdk_loads_nothing_beyond_the_standard_library():
    loaded = _run("""
import thalovant
from thalovant import (AsyncHubSession, AsyncThalovantClient, AsyncThalovantControlPlane,
                       HubSession, ThalovantClient, ThalovantControlPlane)
import thalovant.cli, thalovant.home, thalovant.intents, thalovant.inventory, thalovant.listing
import thalovant.transport
print(json.dumps(sorted({name.split(".")[0] for name in sys.modules} & {"aiohttp", "cryptography"})))
""".replace("print(json.dumps", "import json\nprint(json.dumps"))
    # aiohttp and cryptography wait for the first connection: importing the
    # SDK costs a CLI or a satellite nothing it does not use.
    assert json.loads(loaded) == []


def test_a_conversation_needs_only_the_core():
    answer = _run("""
import asyncio, tempfile
sys.path.insert(0, ".")
from fake_hub import FakeHub, speak_back
from thalovant import AsyncThalovantClient

async def main():
    hub = FakeHub()
    hub.responder = speak_back
    await hub.start()
    record = hub.register()
    with tempfile.TemporaryDirectory() as state:
        client = AsyncThalovantClient(hub.identity(record), noise_state_dir=state, reply_settle_seconds=0.05)
        try:
            reply = await client.ask("hello from the core", timeout=5)
        finally:
            await client.close()
            await hub.stop()
    print(reply.text)

asyncio.run(main())
""")
    assert answer == "You said hello from the core"


def test_mqtt_names_its_extra_when_paho_is_missing():
    message = _run("""
from thalovant import ThalovantIdentity, ThalovantConnectionError
from thalovant.transport import HiveMindMQTTTransport
try:
    HiveMindMQTTTransport._load_mqtt_module()
except ThalovantConnectionError as error:
    print(error)
""")
    assert "thalovant[mqtt]" in message
