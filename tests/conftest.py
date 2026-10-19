"""Start the real platform API on a free port for the tests, with short latencies."""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def platform_url(tmp_path_factory):
    os.environ["PLATFORM_LATENCY"] = "0.2"
    os.environ["PLATFORM_DB"] = str(tmp_path_factory.mktemp("platform") / "platform.db")
    import uvicorn
    from services.app import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    os.environ["PLATFORM_URL"] = url
    yield url
    server.should_exit = True


@pytest.fixture
def platform(platform_url):
    httpx.post(platform_url + "/admin/reset")
    return platform_url


@pytest.fixture(scope="session", autouse=True)
def spans():
    """Every test records spans in memory (stage 09): no exporter, nothing leaves the process."""
    from agentkit import telemetry
    return telemetry.setup_in_memory()
