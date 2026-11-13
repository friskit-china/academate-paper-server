"""A standard-library native v2 client, independent of Next/BFF and product runtimes.

Run in the Server image or a separate Python client container. Credentials are never accepted
through argv/environment or printed. Explicit login authorizes protected local session storage.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

if os.name == "posix":
    import fcntl

JSON_LIMIT = 2_000_000
STATE_LIMIT = 16_384
DEFAULT_DOWNLOAD_LIMIT = 512 * 1024 * 1024


class ClientError(Exception):
    """Safe diagnostic identifier only; never include request/response or credential text."""


def canonical_origin(value: str, *, allow_remote_http: bool = False) -> str:
    try:
        if any(ord(character) <= 32 for character in value) or "\\" in value:
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode().lower()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        loopback = hostname == "localhost"
        with suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        if parsed.scheme == "http" and not loopback and not allow_remote_http:
            raise ClientError("REMOTE_CONNECTION_REQUIRES_HTTPS")
        host = "[" + hostname + "]" if ":" in hostname else hostname
        default_port = 80 if parsed.scheme == "http" else 443
        authority = host + (":" + str(port) if port is not None and port != default_port else "")
        return parsed.scheme + "://" + authority
    except (ValueError, UnicodeError):
        raise ClientError("INVALID_SERVER_ORIGIN") from None


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise ClientError("INVALID_SESSION_STATE") from None


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, origin: str, *, timeout: float = 30, allow_remote_http: bool = False):
        self.origin = canonical_origin(origin, allow_remote_http=allow_remote_http)
        self.timeout = timeout
        # Never forward a user's credentials through an implicit environment-configured proxy.
        # HTTPSHandler uses certificate verification; no insecure override is offered.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, method: str, path: str, *, body=None, bearer=None, headers=None):
        if (
            not path.startswith("/api/v2/")
            or path.startswith("//")
            or any(ord(character) <= 32 for character in path)
            or "\\" in path
            or "#" in path
        ):
            raise ClientError("UNSAFE_API_PATH")
        request_headers = {"Accept": "application/json", "User-Agent": "AcadematePaper-CLI/2"}
        if headers:
            request_headers.update(headers)
        if bearer is not None:
            request_headers["Authorization"] = "Bearer " + bearer
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.origin + path, data=encoded, headers=request_headers, method=method
        )
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if 300 <= code < 400:
                raise ClientError("REDIRECT_REFUSED") from None
            raise ClientError("HTTP_" + str(code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("SERVER_CONNECTION_FAILED") from None

    def json(self, method: str, path: str, **kwargs):
        with self.open(method, path, **kwargs) as response:
            if response.status == 204:
                return None
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise ClientError("INVALID_JSON_RESPONSE")
            raw = response.read(JSON_LIMIT + 1)
            if len(raw) > JSON_LIMIT:
                raise ClientError("RESPONSE_TOO_LARGE")
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise ClientError("INVALID_JSON_RESPONSE") from None


class SessionStore:
    def __init__(self, directory: Path):
        if os.name != "posix":
            raise ClientError("PERSISTENT_CLI_SESSIONS_REQUIRE_POSIX")
        self.directory = directory.expanduser().absolute()
        old_umask = os.umask(0o077)
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ClientError("UNSAFE_SESSION_DIRECTORY")

    @staticmethod
    def _private_fd(path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(fd)
            raise ClientError("UNSAFE_SESSION_FILE")
        return fd

    @contextmanager
    def locked(self, origin: str, account: str):
        path = self.directory / (_fingerprint(origin, account) + ".lock")
        fd = self._private_fd(path, os.O_RDWR | os.O_CREAT)
        try:
            # One lock covers load, rotation and save across both threads and processes.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _path(self, origin: str, account: str, server_id: str | None = None) -> Path:
        parts = (origin, account) if server_id is None else (origin, server_id, account)
        return self.directory / (
            _fingerprint(*parts) + (".pin.json" if server_id is None else ".session.json")
        )

    def _read(self, path: Path) -> dict:
        try:
            fd = self._private_fd(path, os.O_RDONLY)
        except FileNotFoundError:
            raise ClientError("NOT_LOGGED_IN") from None
        with os.fdopen(fd, "rb") as source:
            raw = source.read(STATE_LIMIT + 1)
        if len(raw) > STATE_LIMIT:
            raise ClientError("INVALID_SESSION_STATE")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise ClientError("INVALID_SESSION_STATE") from None

    def _write(self, path: Path, value: dict) -> None:
        if path.exists() or path.is_symlink():
            fd = self._private_fd(path, os.O_RDONLY)
            os.close(fd)
        temporary = self.directory / (".session-" + uuid4().hex + ".tmp")
        try:
            fd = self._private_fd(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w") as output:
                json.dump(value, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, origin: str, server_id: str, account: str) -> dict:
        pin = self._read(self._path(origin, account))
        if (
            pin.get("origin") != origin
            or pin.get("account") != account
            or pin.get("server_id") != server_id
        ):
            raise ClientError("SERVER_ID_CHANGED_LOGIN_REQUIRED")
        state = self._read(self._path(origin, account, server_id))
        if (
            state.get("origin") != origin
            or state.get("server_id") != server_id
            or state.get("account") != account
        ):
            raise ClientError("INVALID_SESSION_STATE")
        return state

    def save(self, state: dict, *, new_login: bool = False) -> None:
        origin, server_id, account = state["origin"], state["server_id"], state["account"]
        self._write(self._path(origin, account, server_id), state)
        if new_login:
            self._write(
                self._path(origin, account),
                {
                    "origin": origin,
                    "server_id": server_id,
                    "account": account,
                },
            )

    def remove(self, state: dict) -> None:
        for path in (
            self._path(state["origin"], state["account"], state["server_id"]),
            self._path(state["origin"], state["account"]),
        ):
            fd = self._private_fd(path, os.O_RDONLY)
            os.close(fd)
            path.unlink()


def _safe_identifier(value: str) -> str:
    if not value or len(value) > 4096:
        raise ClientError("INVALID_RESOURCE_IDENTIFIER")
    return urllib.parse.quote(value, safe="")


def _query(path: str, values: dict) -> str:
    params = {key: value for key, value in values.items() if value is not None}
    return path + ("?" + urllib.parse.urlencode(params) if params else "")


class NativeClient:
    def __init__(
        self,
        origin: str,
        account: str = "",
        *,
        state_dir: Path | None = None,
        transport: Transport | None = None,
        allow_remote_http: bool = False,
        session_store=None,
        client_name: str = "AcadeMate Paper CLI",
        device_name: str = "CLI container",
    ):
        self.origin = canonical_origin(origin, allow_remote_http=allow_remote_http)
        self.account = account.strip().casefold()
        self.transport = transport or Transport(self.origin, allow_remote_http=allow_remote_http)
        if self.transport.origin != self.origin:
            raise ClientError("TRANSPORT_ORIGIN_MISMATCH")
        self.state_dir = state_dir or Path.home() / ".local/state/academatepaper-client"
        self.session_store = session_store
        self.client_name = client_name
        self.device_name = device_name
        self._known_secrets: set[str] = set()

    def discover(self) -> dict:
        info = self.transport.json("GET", "/api/v2/server/info")
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("server_id"), str)
            or not 1 <= len(info["server_id"]) <= 128
            or not isinstance(info.get("api_versions"), list)
            or "v2" not in info["api_versions"]
        ):
            raise ClientError("INCOMPATIBLE_SERVER")
        return info

    def _store(self) -> SessionStore:
        if not self.account or len(self.account) > 128:
            raise ClientError("ACCOUNT_REQUIRED")
        if self.session_store is not None:
            return self.session_store
        return SessionStore(self.state_dir)

    def _pair(self, pair: dict, server_id: str) -> dict:
        try:
            if (
                pair["server_id"] != server_id
                or pair["token_type"] != "Bearer"
                or not pair["authenticated"]
            ):
                raise ValueError
            for field in ("access_token", "refresh_token"):
                raw = pair[field]
                if not isinstance(raw, str) or not re.fullmatch(
                    r"[A-Za-z0-9._~+/-]{32,512}=*", raw
                ):
                    raise ValueError
                self._known_secrets.add(raw)
            for field in ("user_id", "session_id", "active_workspace_id"):
                if not isinstance(pair[field], str) or not 1 <= len(pair[field]) <= 128:
                    raise ValueError
            _timestamp(pair["expires_at"])
            _timestamp(pair["refresh_expires_at"])
        except (KeyError, TypeError, ValueError):
            raise ClientError("INVALID_TOKEN_RESPONSE") from None
        return {
            "origin": self.origin,
            "server_id": server_id,
            "account": self.account,
            "access_token": pair["access_token"],
            "refresh_token": pair["refresh_token"],
            "expires_at": pair["expires_at"],
            "refresh_expires_at": pair["refresh_expires_at"],
            "user_id": pair["user_id"],
            "session_id": pair["session_id"],
            "active_workspace_id": pair["active_workspace_id"],
            "refresh_pending": False,
        }

    def login(self, password: str, *, workspace_id: str | None = None) -> dict:
        self._known_secrets.add(password)
        store = self._store()
        with store.locked(self.origin, self.account):
            info = self.discover()
            body = {
                "username": self.account,
                "password": password,
                "client_name": self.client_name,
                "device_name": self.device_name,
            }
            if workspace_id:
                body["workspace_id"] = workspace_id
            pair = self.transport.json("POST", "/api/v2/auth/sessions", body=body)
            state = self._pair(pair, info["server_id"])
            store.save(state, new_login=True)
            return {
                "authenticated": True,
                "server_id": state["server_id"],
                "user_id": state["user_id"],
                "session_id": state["session_id"],
            }

    def _load(self, store: SessionStore) -> dict:
        # Never send a cached credential before verifying the pinned identity.
        info = self.discover()
        state = store.load(self.origin, info["server_id"], self.account)
        for field in ("access_token", "refresh_token"):
            value = state.get(field)
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9._~+/-]{32,512}=*", value
            ):
                raise ClientError("INVALID_SESSION_STATE")
            self._known_secrets.add(value)
        _timestamp(state.get("expires_at"))
        _timestamp(state.get("refresh_expires_at"))
        if state.get("refresh_pending") is not False:
            raise ClientError("REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED")
        return state

    def _refresh(self, store: SessionStore, state: dict) -> dict:
        if self.discover()["server_id"] != state["server_id"]:
            raise ClientError("SERVER_ID_CHANGED_LOGIN_REQUIRED")
        if _timestamp(state["refresh_expires_at"]) <= datetime.now(UTC):
            raise ClientError("SESSION_EXPIRED_LOGIN_REQUIRED")
        state["refresh_pending"] = True
        store.save(state)  # Crash/network ambiguity must not reuse a consumed refresh credential.
        pair = self.transport.json(
            "POST", "/api/v2/auth/refresh", body={"refresh_token": state["refresh_token"]}
        )
        replacement = self._pair(pair, state["server_id"])
        if (
            replacement["user_id"] != state["user_id"]
            or replacement["session_id"] != state["session_id"]
            or secrets.compare_digest(replacement["refresh_token"], state["refresh_token"])
        ):
            raise ClientError("INVALID_ROTATION_RESPONSE_LOGIN_REQUIRED")
        store.save(replacement)
        return replacement

    def refresh(self) -> dict:
        store = self._store()
        with store.locked(self.origin, self.account):
            state = self._refresh(store, self._load(store))
            return {
                "refreshed": True,
                "session_id": state["session_id"],
                "expires_at": state["expires_at"],
            }

    def _authorized(self, store, state, operation):
        if _timestamp(state["expires_at"]).timestamp() <= time.time() + 30:
            state = self._refresh(store, state)
        try:
            return operation(state["access_token"]), state
        except ClientError as error:
            if str(error) != "HTTP_401":
                raise
            state = self._refresh(store, state)
            return operation(state["access_token"]), state

    def request(self, method: str, path: str, *, body=None, idempotency_key=None):
        store = self._store()
        with store.locked(self.origin, self.account):
            state = self._load(store)
            headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
            result, _ = self._authorized(
                store,
                state,
                lambda access: self.transport.json(
                    method,
                    path,
                    body=body,
                    bearer=access,
                    headers=headers,
                ),
            )
            return result

    def logout(self) -> dict:
        store = self._store()
        with store.locked(self.origin, self.account):
            state = self._load(store)
            _, current = self._authorized(
                store,
                state,
                lambda access: self.transport.json(
                    "DELETE",
                    "/api/v2/auth/sessions/current",
                    bearer=access,
                ),
            )
            store.remove(current)
            return {"logged_out": True}

    def download(self, path: str, destination: Path, *, max_bytes=DEFAULT_DOWNLOAD_LIMIT) -> dict:
        if not 1 <= max_bytes <= 10 * 1024**3:
            raise ClientError("INVALID_DOWNLOAD_LIMIT")
        target = destination.expanduser().absolute()
        store = self._store()
        with store.locked(self.origin, self.account):
            state = self._load(store)

            def transfer(access):
                with self.transport.open("GET", path, bearer=access) as source:
                    length = source.headers.get("Content-Length")
                    if length and (
                        len(length) > 32 or not length.isdecimal() or int(length) > max_bytes
                    ):
                        raise ClientError("DOWNLOAD_TOO_LARGE")
                    fd = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    complete = False
                    total = 0
                    try:
                        with os.fdopen(fd, "wb") as output:
                            while chunk := source.read(65536):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise ClientError("DOWNLOAD_TOO_LARGE")
                                output.write(chunk)
                            if length is not None and total != int(length):
                                raise ClientError("DOWNLOAD_INCOMPLETE")
                            output.flush()
                            os.fsync(output.fileno())
                        complete = True
                        return {"saved": str(target), "bytes": total}
                    finally:
                        if not complete:
                            target.unlink(missing_ok=True)

            result, _ = self._authorized(store, state, transfer)
            return result

    def events(self, *, cursor=None, max_events=10, max_seconds=30) -> list[dict]:
        if not 1 <= max_events <= 100 or not 1 <= max_seconds <= 300:
            raise ClientError("INVALID_EVENT_LIMIT")
        store = self._store()
        with store.locked(self.origin, self.account):
            state = self._load(store)
            source, _ = self._authorized(
                store,
                state,
                lambda access: self.transport.open(
                    "GET",
                    _query("/api/v2/events", {"cursor": cursor}),
                    bearer=access,
                    headers={"Accept": "text/event-stream"},
                ),
            )
            with source:
                if source.headers.get("Content-Type", "").split(";", 1)[0] != "text/event-stream":
                    raise ClientError("INVALID_EVENT_RESPONSE")
                deadline = time.monotonic() + max_seconds
                output, data, event = [], [], "message"
                total = 0
                while len(output) < max_events and time.monotonic() < deadline:
                    line = source.readline(65537)
                    total += len(line)
                    if len(line) > 65536 or total > JSON_LIMIT:
                        raise ClientError("EVENT_RESPONSE_TOO_LARGE")
                    if not line:
                        break
                    text = line.decode("utf-8").rstrip("\r\n")
                    if text.startswith("event:"):
                        event = text[6:].strip()
                    elif text.startswith("data:"):
                        data.append(text[5:].lstrip())
                    elif not text and data:
                        payload = json.loads("\n".join(data))
                        output.append({"event": event, "data": payload})
                        data, event = [], "message"
                return output

    def safe_output(self, value):
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if any(
                        fragment in re.sub(r"[^a-z]", "", key.lower())
                        for fragment in (
                            "password",
                            "token",
                            "cookie",
                            "authorization",
                            "masterkey",
                        )
                    )
                    else self.safe_output(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self.safe_output(child) for child in value]
        if isinstance(value, str):
            for secret in self._known_secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return value


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("INVALID_ARGUMENTS_USE_HELP")


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description=__doc__)
    result.add_argument("--server", required=True, help="Server origin, e.g. https://127.0.0.1:9443")
    result.add_argument("--account", default="", help="Account name; required except for info")
    result.add_argument("--state-dir", type=Path, help="Private client state directory")
    commands = result.add_subparsers(dest="command", required=True, parser_class=SafeArgumentParser)
    commands.add_parser("info")
    login = commands.add_parser("login")
    login.add_argument("--password-stdin", action="store_true")
    login.add_argument("--workspace-id")
    commands.add_parser("refresh")
    commands.add_parser("logout")
    libraries = commands.add_parser("libraries")
    libraries.add_argument("--cursor")
    items = commands.add_parser("items")
    items.add_argument("library_id")
    items.add_argument("--cursor")
    imported = commands.add_parser("import-arxiv")
    imported.add_argument("library_id")
    imported.add_argument("arxiv_id")
    imported.add_argument("--idempotency-key")
    jobs = commands.add_parser("jobs")
    jobs.add_argument("--cursor")
    for command in ("sources", "assets"):
        listing = commands.add_parser(command)
        listing.add_argument("paper_id")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id")
    retry.add_argument("--confirm-paid", action="store_true")
    for name in ("download-asset", "download-source"):
        download = commands.add_parser(name)
        download.add_argument("resource_id", help="Asset ID or paper ID")
        if name == "download-source":
            download.add_argument(
                "source_type", choices=("ARXIV_TARBALL", "ARXIV_PDF", "EXPANDED_SOURCE")
            )
        download.add_argument("--output", required=True, type=Path)
        download.add_argument("--max-bytes", type=int, default=DEFAULT_DOWNLOAD_LIMIT)
    reading = commands.add_parser("reading-state")
    reading.add_argument("item_id")
    writing = commands.add_parser("set-reading-state")
    writing.add_argument("item_id")
    writing.add_argument("--revision", required=True, type=int)
    writing.add_argument("--progress", required=True, type=float)
    writing.add_argument("--block-id")
    changes = commands.add_parser("changes")
    changes.add_argument("--cursor")
    changes.add_argument("--reset", action="store_true")
    events = commands.add_parser("events")
    events.add_argument("--cursor")
    events.add_argument("--max-events", default=10, type=int)
    events.add_argument("--max-seconds", default=30, type=int)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        client = NativeClient(arguments.server, arguments.account, state_dir=arguments.state_dir)
        command = arguments.command
        if command == "info":
            result = client.discover()
        elif command == "login":
            if arguments.password_stdin:
                password = sys.stdin.readline(4097).rstrip("\r\n")
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    password = getpass.getpass("Password (local input only): ")
            if not password or len(password.encode()) > 4096:
                raise ClientError("INVALID_PASSWORD_INPUT")
            result = client.login(password, workspace_id=arguments.workspace_id)
        elif command in {"refresh", "logout"}:
            result = getattr(client, command)()
        elif command == "libraries":
            result = client.request(
                "GET", _query("/api/v2/libraries", {"cursor": arguments.cursor})
            )
        elif command == "items":
            result = client.request(
                "GET",
                _query(
                    "/api/v2/libraries/" + _safe_identifier(arguments.library_id) + "/items",
                    {"cursor": arguments.cursor},
                ),
            )
        elif command == "import-arxiv":
            result = client.request(
                "POST",
                "/api/v2/libraries/" + _safe_identifier(arguments.library_id) + "/imports",
                body={"source": arguments.arxiv_id},
                idempotency_key=arguments.idempotency_key or str(uuid4()),
            )
        elif command == "jobs":
            result = client.request("GET", _query("/api/v2/jobs", {"cursor": arguments.cursor}))
        elif command in {"sources", "assets"}:
            result = client.request(
                "GET", "/api/v2/papers/" + _safe_identifier(arguments.paper_id) + "/" + command
            )
        elif command in {"job", "cancel", "retry"}:
            path = "/api/v2/jobs/" + _safe_identifier(arguments.job_id)
            body = None
            if command != "job":
                path += "/" + command
                body = {"confirm_paid": arguments.confirm_paid} if command == "retry" else None
            result = client.request(
                "GET" if command == "job" else "POST",
                path,
                body=body,
                idempotency_key=str(uuid4()) if command != "job" else None,
            )
        elif command in {"download-asset", "download-source"}:
            if command == "download-asset":
                path = "/api/v2/assets/" + _safe_identifier(arguments.resource_id) + "/content"
            else:
                path = (
                    "/api/v2/papers/"
                    + _safe_identifier(arguments.resource_id)
                    + "/sources/"
                    + arguments.source_type
                    + "/download"
                )
            result = client.download(path, arguments.output, max_bytes=arguments.max_bytes)
        elif command in {"reading-state", "set-reading-state"}:
            path = (
                "/api/v2/me/library-items/" + _safe_identifier(arguments.item_id) + "/reading-state"
            )
            body = (
                None
                if command == "reading-state"
                else {
                    "revision": arguments.revision,
                    "progress": arguments.progress,
                    "block_id": arguments.block_id,
                }
            )
            result = client.request("GET" if body is None else "PUT", path, body=body)
        elif command == "changes":
            result = client.request(
                "GET",
                _query(
                    "/api/v2/changes",
                    {
                        "cursor": arguments.cursor,
                        "reset": "true" if arguments.reset else None,
                    },
                ),
            )
        else:
            result = client.events(
                cursor=arguments.cursor,
                max_events=arguments.max_events,
                max_seconds=arguments.max_seconds,
            )
        print(json.dumps(client.safe_output(result), ensure_ascii=False, indent=2))
        return 0
    except (ClientError, getpass.GetPassWarning) as error:
        print("client error=" + str(error), file=sys.stderr)
    except (OSError, ValueError, UnicodeError):
        print("client error=CLIENT_STORAGE_OR_RESPONSE_INVALID", file=sys.stderr)
    except KeyboardInterrupt:
        print("client error=CANCELLED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
