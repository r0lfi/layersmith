"""HTTP client for the LayerSmith API, shared by the CLI and the TUI.

Rules this module enforces for both interfaces:

* Reads are retried a few times on transient failures. Mutations are never
  retried: a POST that starts a build may have succeeded even though its
  answer was lost, and sending it again could start a second build. When the
  outcome is unknown, OutcomeUnknown says so and tells the user how to check.
* Redirects are not followed. A redirect to another host must not receive
  the credentials, and a redirect to a login page is not an API answer.
* An answer that is not JSON (an HTML login page, a proxy error page) is an
  error, never a success.
* TLS verification is always on. An internal CA is supported through a CA
  bundle; there is deliberately no switch that turns verification off.
* Downloads stream to a temporary file next to the destination, are checked
  against the checksum the server recorded, and only then take the final name.
"""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

import httpx

from layersmith_client import __version__
from layersmith_client.config import ClientConfig, ConfigError, read_password
from layersmith_client.text import clean

# ----------------------------------------------------------------- errors
#
# Exit codes are part of the CLI contract (see docs/client.md).

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_REJECTED = 4
EXIT_NOT_FOUND = 5
EXIT_AUTH = 6
EXIT_UNKNOWN_OUTCOME = 7
EXIT_FILE = 8
EXIT_JOB_FAILED = 9
EXIT_UNSUPPORTED = 10
EXIT_INTERRUPTED = 130


class ClientError(Exception):
    """An error with a message for the user, a hint and an exit code."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None, status: int | None = None):
        super().__init__(clean(message))
        self.hint = clean(hint) if hint else None
        self.status = status


class Unreachable(ClientError):
    exit_code = EXIT_UNREACHABLE


class Rejected(ClientError):
    """The server understood the request and refused it (validation, conflict, size)."""

    exit_code = EXIT_REJECTED


class NotFound(ClientError):
    exit_code = EXIT_NOT_FOUND


class AuthenticationError(ClientError):
    exit_code = EXIT_AUTH


class OutcomeUnknown(ClientError):
    """A mutation was sent and no answer arrived: it may or may not have happened."""

    exit_code = EXIT_UNKNOWN_OUTCOME


class FileProblem(ClientError):
    exit_code = EXIT_FILE


class JobFailed(ClientError):
    exit_code = EXIT_JOB_FAILED


class Unsupported(ClientError):
    exit_code = EXIT_UNSUPPORTED


class ServerError(ClientError):
    exit_code = EXIT_ERROR


#: Statuses worth retrying a read on: the proxy or server is restarting.
RETRY_STATUSES = (502, 503, 504)
READ_ATTEMPTS = 3
#: Exceptions raised before a request reached the server.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
#: Exceptions after which a read can simply be repeated.
TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError,
             httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout)

SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
CHUNK = 1024 * 1024


def safe_filename(candidate: str | None, fallback: str) -> str:
    """A server-suggested file name, or the fallback if it is not plainly safe.

    Only the last path component is ever considered, and it must be a plain
    name: no separators, no leading dot, nothing a shell or a file manager
    could misread.
    """
    if candidate:
        name = candidate.replace("\\", "/").rsplit("/", 1)[-1]
        if SAFE_FILENAME_RE.match(name) and name not in (".", ".."):
            return name
    return fallback


def _content_disposition_filename(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header)
    return match.group(1) if match else None


def _tls_hint(message: str) -> str | None:
    lowered = message.lower()
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return ("The server's TLS certificate could not be verified. If it is signed by an internal CA, "
                "point the client at that CA: layersmith config set ca_cert /path/to/ca.pem "
                "(or --ca-cert). Verification is never switched off.")
    return None


class ApiClient:
    """One LayerSmith server. Thread-safe for the TUI's worker threads."""

    def __init__(self, cfg: ClientConfig, transport: httpx.BaseTransport | None = None,
                 password_prompt: Callable[[], str | None] | None = None):
        self.cfg = cfg
        self.server = cfg.server
        self._features: list[str] | None = None
        auth = None
        if cfg.username:
            try:
                password = read_password(cfg)
            except ConfigError as exc:
                raise ClientError(str(exc))
            if password is None and password_prompt is not None:
                password = password_prompt()
            if password is None:
                raise AuthenticationError(
                    f"A user name is configured ({cfg.username}) but no password was given",
                    hint="Set LAYERSMITH_PASSWORD, or `layersmith config set password_file <file>`.")
            auth = httpx.BasicAuth(cfg.username, password)
        verify: object = True
        if cfg.ca_cert:
            if not Path(cfg.ca_cert).is_file():
                raise ClientError(f"CA bundle not found: {cfg.ca_cert}")
            verify = ssl.create_default_context(cafile=cfg.ca_cert)
        if cfg.client_cert:
            if isinstance(verify, ssl.SSLContext):
                context = verify
            else:
                context = ssl.create_default_context()
            try:
                context.load_cert_chain(cfg.client_cert, cfg.client_key or None)
            except (OSError, ssl.SSLError) as exc:
                raise ClientError(f"Cannot load client certificate {cfg.client_cert}: {exc}")
            verify = context
        kwargs = {"transport": transport} if transport is not None else {}
        self._http = httpx.Client(
            base_url=self.server, auth=auth, verify=verify,
            timeout=httpx.Timeout(cfg.timeout, connect=min(cfg.timeout, 10.0)),
            follow_redirects=False,
            headers={"User-Agent": f"layersmith-client/{__version__}", "Accept": "application/json"},
            # The proxy settings of the environment would send internal
            # traffic through an outbound proxy; honour them, as curl does.
            trust_env=True,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ core

    def request(self, method: str, path: str, *, json: object = None, params: dict | None = None,
                files: dict | None = None, timeout: float | None = None, expect_json: bool = True):
        """Send one request and return the decoded JSON body (None for 204)."""
        url = "/api" + path
        idempotent = method in ("GET", "HEAD")
        attempts = READ_ATTEMPTS if idempotent else 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            last = attempt == attempts - 1
            try:
                response = self._http.request(method, url, json=json, params=params, files=files,
                                              timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
            except TRANSIENT as exc:
                if idempotent and not last:
                    continue
                raise self._transport_error(exc, method, path)
            except httpx.HTTPError as exc:
                raise self._transport_error(exc, method, path)
            if idempotent and response.status_code in RETRY_STATUSES and not last:
                continue
            return self._decode(response, expect_json)
        raise AssertionError("unreachable")

    def _transport_error(self, exc: Exception, method: str, path: str) -> ClientError:
        message = str(exc) or exc.__class__.__name__
        hint = _tls_hint(message)
        if isinstance(exc, NOT_SENT) or method in ("GET", "HEAD"):
            if hint:
                return Unreachable(f"TLS verification failed for {self.server}: {message}", hint=hint)
            return Unreachable(
                f"Cannot reach the LayerSmith server at {self.server}: {message}",
                hint="Check the address (layersmith config show), that the server is running, and that this "
                     "machine may connect to it. Nothing was changed on the server.")
        return OutcomeUnknown(
            f"The server did not answer {method} {path} ({message}). The request was sent, so it may "
            "have been carried out.",
            hint="It was not repeated automatically, to avoid doing it twice. Check the current state "
                 "first (for builds: layersmith builds list) before trying again.")

    def _decode(self, response: httpx.Response, expect_json: bool):
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise AuthenticationError(
                f"The server answered with a redirect ({status}) to {clean(location) or 'another page'}",
                hint="A redirect usually means a login page in front of LayerSmith, or http:// where the server "
                     "expects https://. Redirects are not followed, so credentials never reach another host; "
                     "configure the final address with `layersmith config set server <url>`.",
                status=status)
        content_type = response.headers.get("content-type", "")
        is_json = "json" in content_type
        if status in (401, 403):
            scheme = response.headers.get("www-authenticate", "").split(" ", 1)[0]
            raise AuthenticationError(
                f"The server refused access ({status}{', ' + clean(scheme) if scheme else ''})",
                hint="LayerSmith itself has no logins; this comes from a proxy in front of it. The client "
                     "supports HTTP Basic (username + LAYERSMITH_PASSWORD or password_file) and client "
                     "certificates (client_cert/client_key).",
                status=status)
        if not is_json and status != 204 and (expect_json or status >= 400):
            if "html" in content_type:
                raise AuthenticationError(
                    f"The server sent a web page (HTTP {status}) instead of an API answer",
                    hint="This is usually a login page of a proxy, or an address that is not a LayerSmith "
                         "server. Check `layersmith config show`.",
                    status=status)
            if status >= 400:
                raise self._status_error(status, f"HTTP {status} {response.reason_phrase}")
            raise ServerError(f"Unexpected {content_type or 'untyped'} answer from the server (HTTP {status})",
                              status=status)
        if status >= 400:
            detail = f"HTTP {status}"
            try:
                body = response.json()
                if isinstance(body.get("detail"), str):
                    detail = body["detail"]
                elif isinstance(body.get("detail"), list):
                    detail = "; ".join(
                        f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
                        if isinstance(item, dict) else str(item) for item in body["detail"])
            except ValueError:
                pass
            raise self._status_error(status, detail)
        if status == 204 or not expect_json:
            return None if status == 204 else response
        try:
            return response.json()
        except ValueError:
            raise ServerError(f"The server sent malformed JSON (HTTP {status})", status=status)

    @staticmethod
    def _status_error(status: int, detail: str) -> ClientError:
        if status == 404:
            return NotFound(detail, status=status)
        if status == 405:
            return Unsupported(f"The server does not support this operation ({detail})",
                               hint="The server is probably older than this client; see `layersmith doctor`.",
                               status=status)
        if 400 <= status < 500:
            return Rejected(detail, status=status)
        return ServerError(f"Server error: {detail}", status=status)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: object = None, **kwargs):
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: object = None, **kwargs):
        return self.request("PUT", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------- features

    def health(self) -> dict:
        body = self.get("/health")
        if not isinstance(body, dict) or body.get("app") != "LayerSmith":
            raise ServerError(f"{self.server} answered, but it does not identify itself as LayerSmith")
        self._features = list(body.get("api_features") or [])
        return body

    def features(self) -> list[str]:
        if self._features is None:
            self.health()
        return self._features or []

    def has_feature(self, name: str) -> bool:
        return name in self.features()

    # ------------------------------------------------------- transfers

    def download(self, path: str, destination: Path, *, expected_sha256: str | None, overwrite: bool = False,
                 fallback_name: str = "download", progress: Callable[[int, int | None], None] | None = None,
                 timeout: float | None = None) -> dict:
        """Stream a server file to a local path, verifying it before it gets its name.

        `destination` may be a directory, in which case the server's file
        name is used if it is plainly safe. An existing file is never
        replaced unless `overwrite` is set, and a partial download never
        takes the destination's name.
        """
        destination = Path(destination).expanduser()
        url = "/api" + path
        try:
            stream = self._http.stream("GET", url, timeout=httpx.Timeout(timeout or self.cfg.timeout,
                                                                        connect=min(self.cfg.timeout, 10.0)))
            response = stream.__enter__()
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, "GET", path)
        temporary: Path | None = None
        try:
            if response.status_code != 200 or "json" in response.headers.get("content-type", "") \
                    or "html" in response.headers.get("content-type", ""):
                response.read()
                if response.status_code == 404:
                    detail = "File is no longer available on the server"
                    try:
                        detail = response.json().get("detail") or detail
                    except ValueError:
                        pass
                    raise FileProblem(detail, hint="It may have been removed after its status was read; "
                                                   "rebuild, or recreate the bundle.", status=404)
                self._decode(response, expect_json=True)
                raise ServerError("The server sent JSON where a file was expected")
            if destination.is_dir() or str(destination).endswith(os.sep):
                name = safe_filename(_content_disposition_filename(response.headers.get("content-disposition")),
                                     fallback_name)
                destination = destination / name
            if destination.exists() and not overwrite:
                raise FileProblem(f"{destination} already exists",
                                  hint="Choose another --output, or pass --force to replace it.")
            if not destination.parent.is_dir():
                raise FileProblem(f"Directory {destination.parent} does not exist")
            total = response.headers.get("content-length")
            total_bytes = int(total) if total and total.isdigit() else None
            handle = tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.",
                                                 suffix=".partial", delete=False)
            temporary = Path(handle.name)
            digest = hashlib.sha256()
            received = 0
            with handle:
                for chunk in response.iter_bytes(CHUNK):
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total_bytes)
            if total_bytes is not None and received != total_bytes:
                raise FileProblem(f"Download incomplete: {received} of {total_bytes} bytes",
                                  hint="Nothing was written to the destination. Run the command again.")
            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256.lower():
                raise FileProblem(f"Checksum mismatch: expected sha256 {expected_sha256}, received {actual}",
                                  hint="The file was discarded. It may have changed on the server while "
                                       "downloading; check the build and try again.")
            os.chmod(temporary, 0o644)
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)  # fails if the name was taken meanwhile
                except FileExistsError:
                    raise FileProblem(f"{destination} already exists", hint="Pass --force to replace it.")
                except OSError:
                    if destination.exists():
                        raise FileProblem(f"{destination} already exists", hint="Pass --force to replace it.")
                    os.replace(temporary, destination)
                temporary.unlink(missing_ok=True)
            temporary = None
            return {"path": str(destination.resolve()), "size": received, "sha256": actual,
                    "expected_sha256": expected_sha256, "verified": bool(expected_sha256)}
        except httpx.HTTPError as exc:
            raise FileProblem(f"Download interrupted: {exc}",
                              hint="Nothing was written to the destination. Run the command again.")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            stream.__exit__(None, None, None)

    def upload(self, local: Path, progress: Callable[[int, int], None] | None = None) -> dict:
        """Upload one local file. Uploads are content addressed, so repeating one is harmless."""
        local = Path(local).expanduser()
        if not local.is_file():
            raise FileProblem(f"Not a regular file: {local}")
        size = local.stat().st_size
        digest = hashlib.sha256()
        with open(local, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
        expected = digest.hexdigest()

        with open(local, "rb") as handle:
            body = _ProgressReader(handle, size, progress)
            result = self.request("POST", "/uploads",
                                  files={"file": (local.name, body, "application/octet-stream")},
                                  timeout=max(self.cfg.timeout, 600.0))
        if not isinstance(result, dict) or result.get("sha256") != expected:
            raise FileProblem(f"Upload of {local} arrived with a different checksum",
                              hint="Nothing refers to it yet; try again.")
        return {**result, "local_path": str(local)}


class _ProgressReader:
    """A file wrapper that reports how much has been read, for upload progress."""

    def __init__(self, handle, size: int, progress):
        self._handle, self._size, self._progress, self._read = handle, size, progress, 0

    def read(self, amount: int = -1) -> bytes:
        data = self._handle.read(amount)
        self._read += len(data)
        if self._progress and data:
            self._progress(self._read, self._size)
        return data

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.read(CHUNK):
            yield chunk

    def seek(self, *args):
        return self._handle.seek(*args)

    def tell(self):
        return self._handle.tell()

    @property
    def name(self):
        return self._handle.name
