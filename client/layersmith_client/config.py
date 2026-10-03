"""Client configuration: which server to talk to, and how.

This is the client's own configuration, kept in the user's home directory.
It never describes the server's storage: a download directory here is a
directory on the machine running the client.

Precedence, highest first:

    command-line flag  >  environment variable  >  config file  >  default

The config file is JSON at $XDG_CONFIG_HOME/layersmith/client.json
(~/.config/layersmith/client.json), or wherever LAYERSMITH_CONFIG points.
It holds no secrets: a proxy password comes from the environment, from a
password file, or from a prompt.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT = 30.0

#: setting -> (environment variable, description). The only keys `config set`
#: accepts; anything else in the file is reported, not silently used.
SETTINGS = {
    "server": ("LAYERSMITH_SERVER", "Base URL of the LayerSmith server, e.g. https://layersmith.example.org"),
    "ca_cert": ("LAYERSMITH_CA_CERT", "CA bundle (PEM) used to verify the server certificate, for an internal CA"),
    "client_cert": ("LAYERSMITH_CLIENT_CERT", "Client certificate (PEM) for a proxy that requires mutual TLS"),
    "client_key": ("LAYERSMITH_CLIENT_KEY", "Private key for client_cert, if it is not in the same file"),
    "username": ("LAYERSMITH_USERNAME", "User name for a proxy that requires HTTP Basic authentication"),
    "password_file": ("LAYERSMITH_PASSWORD_FILE", "File holding the Basic authentication password (mode 0600)"),
    "timeout": ("LAYERSMITH_TIMEOUT", f"Seconds to wait for the server (default {DEFAULT_TIMEOUT:.0f})"),
    "download_dir": ("LAYERSMITH_DOWNLOAD_DIR", "Local directory the TUI offers for downloads (default: current)"),
}
PASSWORD_ENV = "LAYERSMITH_PASSWORD"
CONFIG_ENV = "LAYERSMITH_CONFIG"


class ConfigError(ValueError):
    """A configuration value that cannot be used, with a message for the user."""


def config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "layersmith" / "client.json"


def normalise_server(value: str) -> str:
    """Validate a server URL and reduce it to scheme://host[:port][/prefix].

    A trailing /api is dropped, because the client adds it: pasting the URL
    of the API instead of the server is an easy mistake to make.
    """
    text = (value or "").strip()
    if not text:
        raise ConfigError("The server URL is empty")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"The server URL must start with http:// or https://, not {text!r}")
    if not parts.hostname:
        raise ConfigError(f"The server URL has no host name: {text!r}")
    if parts.username or parts.password:
        # Credentials in a URL end up in shell history, logs and `config show`.
        raise ConfigError("Do not put credentials in the server URL; use `username` and a password file "
                          "or the LAYERSMITH_PASSWORD environment variable instead")
    if parts.query or parts.fragment:
        raise ConfigError("The server URL must not contain a query or fragment")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_file(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc.strerror}")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def save_file(data: dict, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def check_value(key: str, value: str) -> object:
    if key not in SETTINGS:
        raise ConfigError(f"Unknown setting {key!r}. Known: {', '.join(SETTINGS)}")
    if key == "server":
        return normalise_server(value)
    if key == "timeout":
        try:
            number = float(value)
        except ValueError:
            raise ConfigError("timeout must be a number of seconds")
        if not 1 <= number <= 3600:
            raise ConfigError("timeout must be between 1 and 3600 seconds")
        return number
    if key in ("ca_cert", "client_cert", "client_key", "password_file", "download_dir"):
        return str(Path(value).expanduser())
    return value


@dataclass
class ClientConfig:
    """The effective settings, and where each one came from."""

    server: str = DEFAULT_SERVER
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    username: str | None = None
    password_file: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    download_dir: str | None = None
    sources: dict = field(default_factory=dict)
    path: Path | None = None
    unknown_keys: list = field(default_factory=list)

    def describe(self) -> list[dict]:
        """Rows for `config show`: values and origins, never a password."""
        rows = []
        for key in SETTINGS:
            value = getattr(self, key)
            rows.append({"setting": key, "value": value, "source": self.sources.get(key, "default"),
                         "variable": SETTINGS[key][0]})
        rows.append({"setting": "password", "value": "(set)" if os.environ.get(PASSWORD_ENV) else None,
                     "source": "environment" if os.environ.get(PASSWORD_ENV) else "default",
                     "variable": PASSWORD_ENV})
        return rows


def resolve(flags: Optional[dict] = None, environ: Optional[Mapping[str, str]] = None, path: Path | None = None) -> ClientConfig:
    """Combine flags, environment, file and defaults, in that order of precedence."""
    flags = {key: value for key, value in (flags or {}).items() if value is not None}
    env: Mapping[str, str] = os.environ if environ is None else environ
    path = path or config_path()
    stored = load_file(path)
    result = ClientConfig(path=path)
    result.unknown_keys = sorted(key for key in stored if key not in SETTINGS)
    for key, (variable, _) in SETTINGS.items():
        if key in flags:
            raw, source = flags[key], "flag"
        elif env.get(variable):
            raw, source = env[variable], "environment"
        elif stored.get(key) not in (None, ""):
            raw, source = stored[key], "config file"
        else:
            continue
        try:
            value = check_value(key, str(raw))
        except ConfigError as exc:
            raise ConfigError(f"{exc} (from {source}{' ' + variable if source == 'environment' else ''})")
        setattr(result, key, value)
        result.sources[key] = source
    return result


def read_password(cfg: ClientConfig, environ: Optional[Mapping[str, str]] = None) -> str | None:
    """The Basic authentication password, from the environment or a private file."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    if env.get(PASSWORD_ENV):
        return env[PASSWORD_ENV]
    if not cfg.password_file:
        return None
    path = Path(cfg.password_file)
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigError(f"Cannot read password file {path}: {exc.strerror}")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"Password file {path} is readable by other users; run: chmod 600 {path}")
    return path.read_text(encoding="utf-8").strip("\r\n")
