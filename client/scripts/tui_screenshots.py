"""Render screenshots of the real TUI against a throwaway LayerSmith server.

    python client/scripts/tui_screenshots.py OUTPUT_DIR [--sizes 160x48,120x36,100x30,80x24]

Development tool for reviewing the design. It needs the server package
(`pip install -e backend`) because it starts the real API in-process, with a
temporary data directory and a fake build runtime from the backend tests.
The data below exists only for these screenshots; the client never falls
back to it. The header shows a synthetic address, never the local one.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

DISPLAY_SERVER = "https://layersmith.example.org"


def start_server(data_dir: Path, populate: bool):
    import httpx
    import uvicorn
    from layersmith import config as server_config
    from layersmith.api import create_app
    from tests.test_builds import FakeBackend

    class DemoBackend(FakeBackend):
        """The backend suite's fake runtime, with a believable log."""

        def build(self, context_dir, image_ref, architecture, on_log):
            for line in [f"STEP 1/6: FROM base for {image_ref}", "STEP 2/6: RUN package install",
                         "Fetched 24.1 MB in 3s (8.0 MB/s)", "Setting up curl (8.5.0-2ubuntu10) ...",
                         "WARNING: htop is only in EPEL on this distribution; skipped",
                         "STEP 3/6: COPY files", "STEP 4/6: ENV TZ=UTC", "STEP 5/6: RUN tests",
                         "curl 8.5.0 (x86_64-pc-linux-gnu)", "STEP 6/6: LABEL org.opencontainers.image.title",
                         f"COMMIT {image_ref}"]:
                on_log(line)
            return super().build(context_dir, image_ref, architecture, lambda line: None)

    settings = server_config.reset_for_tests(data_dir=data_dir, scanner="none")
    app = create_app(settings)
    backend = DemoBackend()
    app.state.layersmith["build_service"].backend = backend
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    if populate:
        service = app.state.layersmith["build_service"]
        service.enqueue = lambda build_id: None  # run synchronously below, in a fixed order

        def project(name, template, spec, description=""):
            return httpx.post(f"{url}/api/projects", json={"name": name, "template": template,
                                                           "description": description, "spec": spec}).json()

        def build(project_id, version, fail=False):
            backend.fail_on = "build" if fail else None
            started = httpx.post(f"{url}/api/projects/{project_id}/builds", json={"version": version}).json()
            service.run(started["id"])
            return started

        net = project("net-tools", "Network Tools", {"base": {"distribution": "Ubuntu", "version": "24.04"}},
                      "Packet capture and connectivity troubleshooting")
        build(net["id"], "1.0.0")
        build(net["id"], "1.1.0")
        admin = project("ops-toolbox", "Linux Admin", {"base": {"distribution": "AlmaLinux", "version": "10"}},
                        "Inspection and troubleshooting toolbox for on-call")
        build(admin["id"], "2.3.0", fail=True)
        kube = project("kube-clients", "Kubernetes", {"base": {"distribution": "Debian", "version": "13"}},
                       "kubectl and helm for the platform team")
        build(kube["id"], "0.9.0")
        project("alpine-base", "Minimal", {"base": {"distribution": "Alpine", "version": "3.22"}},
                "Minimal Alpine with curl")
        httpx.post(f"{url}/api/projects/import", json={
            "name": "legacy-app", "description": "Imported Containerfile",
            "containerfile": "FROM docker.io/library/debian:13\nRUN echo legacy\n"})
        # One build left queued, so "running" work exists on the server.
        httpx.post(f"{url}/api/projects/{kube['id']}/builds", json={"version": "1.0.0"})
    return url, server


async def capture(url: str | None, out: Path, size: tuple[int, int], views: list[str], prefix: str) -> None:
    from layersmith_client.api import ApiClient
    from layersmith_client.config import ClientConfig
    from layersmith_client.tui.app import LayerSmithApp

    shown = ClientConfig(server=DISPLAY_SERVER, timeout=3)

    def factory(cfg):
        real = ClientConfig(**{**cfg.__dict__, "server": url or "http://127.0.0.1:9"})
        client = ApiClient(real)
        client.server = cfg.server  # what the user would see
        return client

    app = LayerSmithApp(shown, client_factory=factory)
    async with app.run_test(size=size) as pilot:
        async def settle(rounds=8):
            for _ in range(rounds):
                await pilot.pause(0.15)
                await app.workers.wait_for_complete()

        await settle()
        for view in views:
            if view != "start":
                app.show_view(view)
                await settle()
                if view in ("projects", "builds", "dashboard"):
                    app.current_view().focus_main()
                    await pilot.press("down")
                    await settle()
            name = f"{prefix}{view}-{size[0]}x{size[1]}.svg"
            app.save_screenshot(filename=name, path=str(out))
        if url and not prefix:
            from layersmith_client.tui.detail import BuildScreen
            from layersmith_client.tui.widgets import ConfirmScreen
            builds = app.client.get("/builds")
            ready = next(b for b in builds if b["status"] == "ready")
            app.push_screen(BuildScreen(ready["id"], tab="log"))
            await settle()
            app.save_screenshot(filename=f"build-page-{size[0]}x{size[1]}.svg", path=str(out))
            app.pop_screen()
            app.show_view("projects")
            await settle()
            selected = app.query_one("#projects").selected()
            app.push_screen(ConfirmScreen(f"Delete project {selected['name']}? Built images and archives are kept.",
                                          "Delete"))
            await settle()
            app.save_screenshot(filename=f"dialog-{size[0]}x{size[1]}.svg", path=str(out))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output")
    parser.add_argument("--sizes", default="160x48,120x36,100x30,80x24")
    parser.add_argument("--views", default="projects,dashboard,builds,create,training,scans,exports,settings")
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    sizes = [tuple(int(n) for n in item.split("x")) for item in args.sizes.split(",")]
    with tempfile.TemporaryDirectory() as full, tempfile.TemporaryDirectory() as empty:
        url, _ = start_server(Path(full), populate=True)
        empty_url, _ = start_server(Path(empty) / "data", populate=False)
        for size in sizes:
            asyncio.run(capture(url, out, size, args.views.split(","), ""))
        asyncio.run(capture(empty_url, out, sizes[0], ["projects", "dashboard"], "empty-"))
        asyncio.run(capture(None, out, sizes[0], ["start"], "disconnected-"))
        asyncio.run(capture(None, out, (80, 24), ["start"], "disconnected-"))
    print(f"screenshots in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
