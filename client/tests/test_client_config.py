import json
import os

import pytest

from layersmith_client import config


def test_precedence_flag_over_environment_over_file_over_default(tmp_path):
    path = tmp_path / "client.json"
    assert config.resolve({}, environ={}, path=path).server == config.DEFAULT_SERVER

    config.save_file({"server": "https://file.example.org"}, path)
    assert config.resolve({}, environ={}, path=path).server == "https://file.example.org"

    environ = {"LAYERSMITH_SERVER": "https://env.example.org"}
    resolved = config.resolve({}, environ=environ, path=path)
    assert resolved.server == "https://env.example.org" and resolved.sources["server"] == "environment"

    resolved = config.resolve({"server": "https://flag.example.org"}, environ=environ, path=path)
    assert resolved.server == "https://flag.example.org" and resolved.sources["server"] == "flag"


@pytest.mark.parametrize("raw, expected", [
    ("https://layersmith.example.org/", "https://layersmith.example.org"),
    ("https://layersmith.example.org/api", "https://layersmith.example.org"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("https://proxy.example.org/layersmith/api/", "https://proxy.example.org/layersmith"),
])
def test_server_url_is_normalised(raw, expected):
    assert config.normalise_server(raw) == expected


@pytest.mark.parametrize("raw", ["layersmith.example.org", "ftp://x.example.org", "https://", "",
                                 "https://user:secret@layersmith.example.org", "https://x.example.org/?a=1"])
def test_bad_server_urls_are_refused(raw):
    with pytest.raises(config.ConfigError):
        config.normalise_server(raw)


def test_credentials_in_url_are_refused_with_a_useful_message():
    with pytest.raises(config.ConfigError, match="credentials"):
        config.normalise_server("https://admin:hunter2@layersmith.example.org")


def test_config_file_is_private_and_unknown_keys_are_reported(tmp_path):
    path = tmp_path / "client.json"
    config.save_file({"server": "https://a.example.org", "colour": "pink"}, path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert config.resolve({}, environ={}, path=path).unknown_keys == ["colour"]


def test_invalid_config_file_is_an_error_not_a_default(tmp_path):
    path = tmp_path / "client.json"
    path.write_text("{not json")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.resolve({}, environ={}, path=path)


def test_password_comes_from_environment_or_a_private_file(tmp_path):
    cfg = config.ClientConfig(username="ops")
    assert config.read_password(cfg, environ={"LAYERSMITH_PASSWORD": "pw"}) == "pw"

    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    os.chmod(secret, 0o644)
    cfg.password_file = str(secret)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.read_password(cfg, environ={})
    os.chmod(secret, 0o600)
    assert config.read_password(cfg, environ={}) == "from-file"


def test_describe_never_shows_a_password(monkeypatch):
    monkeypatch.setenv("LAYERSMITH_PASSWORD", "do-not-print")
    rows = config.ClientConfig().describe()
    assert "do-not-print" not in json.dumps(rows)
    assert next(r for r in rows if r["setting"] == "password")["value"] == "(set)"


def test_default_location_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config.config_path() == tmp_path / "cfg" / "layersmith" / "client.json"
    monkeypatch.setenv("LAYERSMITH_CONFIG", str(tmp_path / "other.json"))
    assert config.config_path() == tmp_path / "other.json"
