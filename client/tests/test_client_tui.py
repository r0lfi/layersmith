"""The TUI, headless through Textual's Pilot, against the real API."""

import asyncio

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import Button, DataTable, Input, OptionList, Static  # noqa: E402

from layersmith_client.api import Unreachable  # noqa: E402
from layersmith_client.config import ClientConfig  # noqa: E402
from layersmith_client.tui.app import LayerSmithApp  # noqa: E402


def drive(cfg, scenario, size=(140, 40)):
    async def main():
        app = LayerSmithApp(cfg)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await scenario(app, pilot)
    asyncio.run(main())


async def settle(pilot, rounds=6):
    for _ in range(rounds):
        await pilot.pause(0.1)
        await pilot.app.workers.wait_for_complete()
    await pilot.pause(0.1)


def text_of(widget) -> str:
    """What a widget shows, as plain text (no colour, no escapes)."""
    from rich.console import Console
    console = Console(width=200, record=True, color_system=None, file=open("/dev/null", "w"))
    content = widget.content
    console.print(content if not isinstance(content, str) else content)
    return console.export_text()


def make_project(server, **extra):
    return httpx.post(f"{server.url}/api/projects", json={
        "name": "demo", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}, **extra}).json()


def test_header_shows_the_server_and_navigation_works_by_keyboard(server):
    make_project(server)

    async def scenario(app, pilot):
        header = text_of(app.query_one("#connection-state", Static))
        assert server.url in header and "Connected" in header
        nav = app.query_one("#nav", OptionList)
        nav.focus()
        await pilot.press("down")
        await settle(pilot)
        # Moving in the menu only moves the cursor; Enter opens the page.
        assert app.query_one("#content").current == "dashboard"
        await pilot.press("enter")
        await settle(pilot)
        assert app.query_one("#content").current == "projects" and app.active_view == "projects"
        table = app.query_one("#project-table", DataTable)
        assert table.has_focus and table.row_count == 1
        await pilot.press("f2")
        assert nav.has_focus
        await pilot.press("tab")
        assert not nav.has_focus
    drive(server.config(), scenario)


def test_an_unreachable_server_opens_the_connection_page():
    async def scenario(app, pilot):
        assert app.connection == "DISCONNECTED"
        assert app.query_one("#content").current == "connection"
        state = text_of(app.query_one("#conn-state", Static))
        assert "Disconnected" in state and "could not be reached" in state
        assert "127.0.0.1:9" in text_of(app.query_one("#connection-state", Static))
    drive(ClientConfig(server="http://127.0.0.1:9", timeout=2), scenario)


def test_changing_the_server_on_the_connection_page_reconnects(server):
    async def scenario(app, pilot):
        app.show_view("connection")
        await settle(pilot)
        field = app.query_one("#server-url", Input)
        field.value = server.url
        field.focus()
        await pilot.press("enter")
        await settle(pilot)
        assert app.connection == "CONNECTED" and app.cfg.server == server.url
    drive(ClientConfig(server="http://127.0.0.1:9", timeout=2), scenario)


def test_too_small_terminal_says_so_and_recovers_on_resize(server):
    async def scenario(app, pilot):
        assert app.has_class("too-small")
        assert app.query_one("#too-small", Static).display
        assert "80x24" in text_of(app.query_one("#too-small", Static))
        await pilot.resize_terminal(100, 30)
        await settle(pilot)
        assert not app.has_class("too-small") and app.query_one("#body").display
        assert app.has_class("medium")  # details and log share one pane below 140x40
    drive(server.config(), scenario, size=(70, 20))


def test_80x24_is_usable(server):
    make_project(server)

    async def scenario(app, pilot):
        assert not app.has_class("too-small")
        app.show_view("projects")
        await settle(pilot)
        assert app.query_one("#project-table", DataTable).row_count == 1
    drive(server.config(), scenario, size=(80, 24))


def test_typing_q_into_a_field_does_not_quit(server):
    async def scenario(app, pilot):
        app.show_view("connection")
        await settle(pilot)
        field = app.query_one("#server-url", Input)
        field.value = ""
        field.focus()
        await pilot.press("q", "u", "i", "t")
        assert field.value == "quit" and app.is_running
    drive(server.config(), scenario)


def test_wizard_validates_keeps_values_and_creates_through_the_api(server):
    async def scenario(app, pilot):
        app.show_view("create")
        await settle(pilot)
        wizard = app.query_one("#create")
        # Alpine as the base, via the server's own distribution list.
        wizard.go(1)
        distributions = app.query_one("#distributions", OptionList)
        distributions.highlighted = [o.id for o in distributions._options].index("Alpine")
        await settle(pilot)
        for step in range(2, 8):
            wizard.go(step)
            await settle(pilot)
        app.query_one("#extra-packages", Input).value = "ncdu"
        app.query_one("#image-name", Input).value = "Not Valid"
        wizard.go(8)
        await settle(pilot)
        assert wizard.step == 7
        # The problem is shown at the field it belongs to.
        assert "Lowercase letters" in text_of(app.query_one("#image-name-error", Static))
        assert app.query_one("#image-name", Input).has_class("-invalid")
        assert not app.query_one("#image-version", Input).has_class("-invalid")

        # Back and forth keeps what was entered.
        wizard.go(4)
        await settle(pilot)
        assert app.query_one("#extra-packages", Input).value == "ncdu"
        wizard.go(7)
        app.query_one("#image-name", Input).value = "from-tui"
        wizard.go(8)
        await settle(pilot)
        assert wizard.step == 8
        containerfile = text_of(app.query_one("#review-containerfile", Static))
        assert "FROM docker.io/library/alpine:3.22" in containerfile and "ncdu" in containerfile

        app.query_one("#create-only", Button).press()
        await settle(pilot)
        names = [p["name"] for p in httpx.get(f"{server.url}/api/projects").json()]
        assert names == ["from-tui"]
    drive(server.config(), scenario)


def test_build_page_follows_the_log_and_shows_status(server):
    project = make_project(server)
    build = httpx.post(f"{server.url}/api/projects/{project['id']}/builds", json={}).json()

    async def scenario(app, pilot):
        from layersmith_client.tui.detail import BuildScreen
        app.push_screen(BuildScreen(build["id"], tab="log"))
        await settle(pilot, rounds=12)
        screen = app.screen
        assert screen.build["status"] == "ready"
        log = screen.query_one("#log-view-panel")
        assert any("STEP 1/2" in line for line in log.plain)
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, BuildScreen)
    drive(server.config(), scenario)


def test_lost_connection_is_shown_as_lost(server):
    async def scenario(app, pilot):
        def broken(*args, **kwargs):
            raise Unreachable("Cannot reach the LayerSmith server")
        app.client.get = broken
        app.show_view("builds")
        await settle(pilot)
        assert app.connection == "DISCONNECTED"
        assert "Disconnected" in text_of(app.query_one("#connection-state", Static))
    drive(server.config(), scenario)


def test_untrusted_text_is_shown_literally(server):
    make_project(server, description="[bold red]not markup[/] \x1b]0;title\x07\x1b[2J end")

    async def scenario(app, pilot):
        app.show_view("projects")
        await settle(pilot)
        app.query_one("#project-table", DataTable).move_cursor(row=0)
        await settle(pilot)
        output = text_of(app.query_one("#project-facts", Static))
        assert "[bold red]not markup[/]" in output and "\x1b" not in output and "end" in output
    drive(server.config(), scenario)


def test_training_wizard_lists_the_servers_profiles(server):
    async def scenario(app, pilot):
        app.show_view("training")
        await settle(pilot)
        profiles = app.query_one("#profiles", OptionList)
        ids = [option.id for option in profiles._options]
        assert {"hf-finetune", "pytorch-advanced", "llama-factory", "dataset-prep"} <= set(ids)
        wizard = app.query_one("#training")
        assert wizard.resolved and wizard.resolved["ok"]
    drive(server.config(), scenario)


def test_quitting_leaves_a_running_build_running(server):
    project = make_project(server)
    service = server.state["build_service"]
    service.enqueue = lambda build_id: None  # keep it queued: "running" on the server
    build = httpx.post(f"{server.url}/api/projects/{project['id']}/builds", json={}).json()

    async def scenario(app, pilot):
        app.show_view("builds")
        await settle(pilot)
        await pilot.press("ctrl+q")
    drive(server.config(), scenario)
    assert httpx.get(f"{server.url}/api/builds/{build['id']}").json()["status"] == "queued"


# ------------------------------------------------------------- design

def test_every_surface_uses_the_palette(server):
    """No stock widget may bring its own background (the old royal-blue bars)."""
    from textual.color import Color
    from layersmith_client.tui.theme import PALETTE
    make_project(server)
    allowed = {Color.parse(value).hex for value in PALETTE.values()}

    async def scenario(app, pilot):
        for view in ("dashboard", "projects", "builds", "create", "training", "settings"):
            app.show_view(view)
            await settle(pilot)
            for widget in app.screen.query("*"):
                if not widget.display:
                    continue
                background = widget.styles.background
                if background.a == 0:
                    continue
                assert background.hex in allowed or background.with_alpha(1).hex in allowed, \
                    f"{view}: {widget!r} has background {background.hex}"
        footer = app.query_one("Footer")
        assert footer.styles.background.hex == Color.parse(PALETTE["background"]).hex
    drive(server.config(), scenario)


def test_selected_project_drives_details_and_log(server):
    first = make_project(server)
    second = httpx.post(f"{server.url}/api/projects", json={
        "name": "other", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    service = server.state["build_service"]
    started = httpx.post(f"{server.url}/api/projects/{first['id']}/builds", json={"version": "1.2.3"}).json()
    import time
    for _ in range(100):
        if httpx.get(f"{server.url}/api/builds/{started['id']}").json()["status"] == "ready":
            break
        time.sleep(0.05)
    del service, second

    async def scenario(app, pilot):
        app.show_view("projects")
        await settle(pilot)
        table = app.query_one("#project-table", DataTable)
        log = app.query_one("#project-log")
        table.focus()
        table.move_cursor(row=[str(k.value) for k in table.rows].index(first["id"]))
        await settle(pilot, rounds=10)
        assert "demo" in text_of(app.query_one("#project-facts", Static))
        assert "demo #1 1.2.3" in str(log.border_title) and any("STEP 1/2" in line for line in log.plain)
        table.move_cursor(row=[str(k.value) for k in table.rows].index(next(
            str(k.value) for k in table.rows if str(k.value) != first["id"])))
        await settle(pilot)
        # The other project has no builds: its log is not the previous project's.
        assert log.build is None and not any("STEP" in line for line in log.plain)
    drive(server.config(), scenario)


def test_scrolling_back_pauses_following(server):
    project = make_project(server)
    build = httpx.post(f"{server.url}/api/projects/{project['id']}/builds", json={}).json()

    async def scenario(app, pilot):
        from layersmith_client.tui.widgets import LogPanel
        from layersmith_client.tui.detail import BuildScreen
        app.push_screen(BuildScreen(build["id"], tab="log"))
        await settle(pilot, rounds=10)
        panel = app.screen.query_one(LogPanel)
        for number in range(200):
            panel.view.write(f"line {number}")
        await pilot.pause(0.1)
        panel.view.scroll_home(animate=False)
        await pilot.pause(0.1)
        panel._lines((panel.follower, ["one more line"]))
        assert panel.following is False and "paused" in str(panel.border_title)
        panel.action_follow()
        assert panel.following is True and "following" in str(panel.border_title)
    drive(server.config(), scenario)


@pytest.mark.parametrize("size, mode", [((160, 48), "wide"), ((120, 36), "medium"), ((100, 30), "medium"),
                                        ((80, 24), "compact")])
def test_layout_mode_per_terminal_size(server, size, mode):
    make_project(server)

    async def scenario(app, pilot):
        assert app.layout_mode == mode
        app.show_view("projects")
        await settle(pilot)
        detail, log = app.query_one("#project-detail"), app.query_one("#project-log")
        if mode == "wide":
            assert detail.display and log.display and detail.region.width >= 38
        else:
            assert detail.display and not log.display
            app.query_one("#projects").action_toggle_pane()
            await settle(pilot)
            assert log.display and not detail.display
        # Every project action stays reachable at every size.
        bindings = {binding.action for binding in app.query_one("#projects").BINDINGS}
        assert {"new", "build", "search", "import_file", "clone", "export_spec", "delete"} <= bindings
    drive(server.config(), scenario, size=size)


def test_the_stylesheet_ships_with_the_package():
    from importlib.resources import files
    assert files("layersmith_client.tui").joinpath("layersmith.tcss").is_file()
