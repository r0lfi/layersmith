"""The command line: parsing, help, exit codes, stdout/stderr, prompts."""

import argparse
import json
import subprocess
import sys

import pytest

from layersmith_client import cli
from layersmith_client.api import EXIT_UNREACHABLE, EXIT_USAGE


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def all_commands(parser=None, prefix=()):
    parser = parser or cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield (*prefix, name)
                yield from all_commands(sub, (*prefix, name))


def test_every_command_has_help(capsys):
    commands = list(all_commands())
    assert ("projects", "create") in commands and ("training", "gpu-report") in commands
    for command in commands:
        with pytest.raises(SystemExit) as exited:
            cli.main([*command, "--help"])
        assert exited.value.code == 0
        assert "usage: layersmith" in capsys.readouterr().out


def test_version_and_help_need_no_server(capsys):
    code, out, _ = run(["version"], capsys)
    assert code == 0 and out.startswith("layersmith-client ")
    code, out, _ = run(["--json", "version"], capsys)
    assert json.loads(out)["client"]


def test_starting_the_client_loads_no_server_code():
    """The client must not import the server, its database or its worker."""
    script = ("import sys; from layersmith_client import cli; cli.main(['version']); "
              "bad = [m for m in ('layersmith', 'layersmith.api', 'fastapi', 'sqlalchemy', 'uvicorn', "
              "'textual') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "[]"  # not even Textual, until the TUI is opened


def test_no_terminal_means_no_tui_and_no_hang(capsys):
    code, out, err = run([], capsys)
    assert code == EXIT_USAGE and "use a command" in err and out == ""
    code, _, err = run(["tui"], capsys)
    assert code == EXIT_USAGE and "interactive terminal" in err


def test_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["projects", "nonsense"])
    assert exited.value.code == 2


def test_unreachable_server_exits_3_with_the_message_on_stderr(capsys):
    code, out, err = run(["--server", "http://127.0.0.1:9", "--timeout", "2", "projects", "list"], capsys)
    assert code == EXIT_UNREACHABLE and out == "" and "Cannot reach" in err


def test_json_errors_are_json_on_stdout(capsys):
    code, out, err = run(["--json", "--server", "http://127.0.0.1:9", "--timeout", "2", "projects", "list"],
                         capsys)
    body = json.loads(out)
    assert code == EXIT_UNREACHABLE and body["exit_code"] == EXIT_UNREACHABLE and "\x1b" not in out


def test_config_set_show_and_unset(capsys, tmp_path):
    code, out, _ = run(["config", "set", "server", "https://layersmith.example.org/api/"], capsys)
    assert code == 0 and "https://layersmith.example.org" in out
    code, out, _ = run(["--json", "config", "show"], capsys)
    rows = {r["setting"]: r for r in json.loads(out)["settings"]}
    assert rows["server"]["value"] == "https://layersmith.example.org" and rows["server"]["source"] == "config file"
    code, out, _ = run(["--json", "--server", "http://127.0.0.1:1", "config", "show"], capsys)
    assert {r["setting"]: r for r in json.loads(out)["settings"]}["server"]["source"] == "flag"
    code, _, _ = run(["config", "unset", "server"], capsys)
    code, out, _ = run(["--json", "config", "show"], capsys)
    assert {r["setting"]: r for r in json.loads(out)["settings"]}["server"]["source"] == "default"


def test_config_rejects_credentials_in_the_url(capsys):
    code, _, err = run(["config", "set", "server", "https://u:p@layersmith.example.org"], capsys)
    assert code == 4 and "credentials" in err


def test_destructive_actions_need_confirmation_without_a_terminal(capsys, monkeypatch):
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def delete(self, path):
            calls.append(path)

    monkeypatch.setattr(cli, "make_client", lambda args, out: Client())
    monkeypatch.setattr(cli.ops, "resolve_project", lambda client, ref: {"id": "p1", "name": ref})
    code, _, err = run(["projects", "delete", "demo"], capsys)
    assert code == EXIT_USAGE and "--yes" in err and calls == []
    code, _, _ = run(["--json", "projects", "delete", "demo"], capsys)
    assert code == EXIT_USAGE and calls == []  # JSON mode never prompts
    code, out, _ = run(["projects", "delete", "demo", "--yes"], capsys)
    assert code == 0 and calls == ["/projects/p1"]


def test_ctrl_c_while_waiting_explains_that_the_job_continues(capsys, monkeypatch):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "make_client", lambda args, out: Client())
    monkeypatch.setattr(cli.ops, "resolve_build_id", lambda client, ref: ref)
    monkeypatch.setattr(cli.ops, "wait_for_build", interrupted)
    code, _, err = run(["builds", "wait", "b-123"], capsys)
    assert code == 130 and "continues on the server" in err and "builds wait b-123" in err
