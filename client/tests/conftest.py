"""Fixtures for the client tests.

Most tests use httpx.MockTransport and need nothing but the client. The
`server` fixture starts the real LayerSmith API over real HTTP on a free
loopback port, with a temporary data directory and the fake build backend
from the backend's own tests, so contract tests talk to exactly the API the
web UI uses. It is skipped when the server package is not installed, which
is the normal state of a client-only environment.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from layersmith_client.config import ClientConfig

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read or write the developer's real client configuration."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("LAYERSMITH_SERVER", "LAYERSMITH_CONFIG", "LAYERSMITH_PASSWORD", "LAYERSMITH_USERNAME",
                 "LAYERSMITH_CA_CERT", "LAYERSMITH_TIMEOUT", "LAYERSMITH_PASSWORD_FILE", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LiveServer:
    def __init__(self, url: str, app, data_dir: Path):
        self.url, self.app, self.data_dir = url, app, data_dir

    @property
    def state(self):
        return self.app.state.layersmith

    def config(self, **overrides) -> ClientConfig:
        return ClientConfig(**{"server": self.url, "timeout": 10.0, **overrides})


@pytest.fixture
def server(tmp_path):
    pytest.importorskip("layersmith.api", reason="the LayerSmith server package is not installed")
    uvicorn = pytest.importorskip("uvicorn")
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from layersmith import config as server_config
    from layersmith.api import create_app
    from tests.test_builds import FakeBackend  # the backend suite's fake runtime

    data = tmp_path / "server-data"
    settings = server_config.reset_for_tests(data_dir=data, scanner="none")
    app = create_app(settings)
    app.state.layersmith["build_service"].backend = FakeBackend()
    app.state.layersmith["scan_service"].backend = app.state.layersmith["build_service"].backend

    port = free_port()
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not instance.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    try:
        yield LiveServer(f"http://127.0.0.1:{port}", app, data)
    finally:
        instance.should_exit = True
        thread.join(timeout=10)
