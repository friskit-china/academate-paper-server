"""Run the API, queue worker, setup verifier and bundled Web as one service."""
from __future__ import annotations

import ctypes
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import FrameType

from sqlalchemy.engine import make_url

DEFAULT_STATUS_FILE = Path("/tmp/academate-server-status.json")
SERVER_DEFAULTS = {
    "RUNTIME_EDITION": "SERVER",
    "IDENTITY_MODE": "AUTHENTICATED",
    "DATABASE_PROFILE": "SQLITE_LOCAL",
    "STORAGE_PROFILE": "LOCAL_FILES",
    "TASK_QUEUE_PROFILE": "SQLITE_DB",
    "SECRET_STORE_PROFILE": "ENV_OR_KMS",
    "OS_SECRET_STORE_MODE": "unavailable",
    "RUNTIME_NETWORK_SCOPE": "PRIVATE_COMPOSE",
    "DATABASE_URL": "sqlite:////data/database/workspace.sqlite3",
    "STORAGE_ROOT": "/data",
    "APP_HOST": "0.0.0.0",
    "TOOLCHAIN_MODE": "compose-path",
    "TOOLCHAIN_MANIFEST_PATH": "/opt/texlive/runtime-profile",
    "SINGLE_USER_AUTO_LOGIN_ENABLED": "false",
    # Proof issuance is an explicit operator action, never an automatic log secret.
    "AUTO_ISSUE_SETUP_PROOF": "false",
}


class ServerProcessError(RuntimeError):
    """Bounded diagnostic code only; never include environment or command text."""


def apply_server_defaults(environment: dict[str, str]) -> None:
    for key, value in SERVER_DEFAULTS.items():
        environment.setdefault(key, value)
    if environment.get("SERVER_HTTPS_API_ORIGIN"):
        if any(environment.get(key) for key in ("SERVER_HTTPS_WEB_ALIASES",
                                                "SERVER_HTTPS_API_ALIASES")):
            from app.server_https import https_origins

            web, _ = https_origins(environment)
            for setting, required in (("WEB_CLIENT_ORIGINS", web[1:]), ("CORS_ORIGINS", web)):
                existing = [v.strip() for v in environment.get(setting, "").split(",") if v.strip()]
                environment[setting] = ",".join(dict.fromkeys([*existing, *required]))
        environment.update({
            "WEB_SESSION_COOKIE_SECURE": "true", "WEB_ALLOW_INSECURE_HTTP": "false",
            "RUNTIME_NETWORK_SCOPE": "PUBLIC", "SERVER_API_BIND_HOST": "127.0.0.1",
            "SERVER_WEB_BIND_HOST": "127.0.0.1",
        })


def installation_lock_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ServerProcessError("SERVER_DATABASE_PROFILE_INVALID")
    database = Path(url.database)
    if not database.is_absolute():
        raise ServerProcessError("SERVER_DATABASE_PATH_INVALID")
    return database.parent / ".academate-server.lock"


@contextmanager
def installation_lock(path: Path) -> Iterator[None]:
    """Keep the inode and exclusive flock for the entire container lifetime.

    Never unlink the file: unlinking lets another instance lock a different inode.
    Children must not inherit the lock, including when the supervisor is killed.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ServerProcessError("SERVER_INSTALLATION_IN_USE") from error
        yield
    finally:
        os.close(fd)


def become_child_subreaper() -> None:
    """Adopt/reap orphan render subprocesses even when running behind docker --init."""
    if sys.platform == "darwin":
        # launchd adopts orphaned processes. Child groups are still drained on shutdown.
        return
    if sys.platform != "linux":
        raise ServerProcessError("SERVER_REQUIRES_LINUX")
    # prctl(PR_SET_CHILD_SUBREAPER, 1), supported by the Linux image's libc/kernel.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise ServerProcessError("SERVER_SUBREAPER_UNAVAILABLE")


@dataclass(frozen=True)
class Component:
    name: str
    command: Sequence[str]
    required: bool = True
    environment: Mapping[str, str] | None = None


@dataclass
class RunningComponent:
    component: Component
    process: subprocess.Popen[bytes]
    started_at: float


class ServerProcessManager:
    def __init__(
        self,
        *,
        migration_command: Sequence[str],
        components: Sequence[Component],
        status_file: Path = DEFAULT_STATUS_FILE,
        stop_timeout_seconds: float = 30.0,
        poll_seconds: float = 0.2,
        stop_event: Event | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not math.isfinite(stop_timeout_seconds) or not 0 <= stop_timeout_seconds <= 300:
            raise ValueError("invalid server stop timeout")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("invalid server poll interval")
        names = [component.name for component in components]
        if len(set(names)) != len(names) or "migration" in names:
            raise ValueError("invalid server component names")
        self.migration_command = migration_command
        self.components = components
        self.status_file = status_file
        self.stop_timeout_seconds = stop_timeout_seconds
        self.poll_seconds = poll_seconds
        self.stop_event = stop_event or Event()
        self.environment = dict(environment) if environment is not None else None
        self.running: dict[str, RunningComponent] = {}
        self.phase = "starting"

    def request_stop(self, _signum: int = 0, _frame: FrameType | None = None) -> None:
        self.stop_event.set()

    def _spawn(self, component: Component) -> RunningComponent:
        process = subprocess.Popen(
            component.command,
            env=component.environment if component.environment is not None else self.environment,
            start_new_session=True,
            close_fds=True,
        )
        child = RunningComponent(component, process, time.time())
        self.running[component.name] = child
        return child

    def _reap(self) -> None:
        # poll() first so Popen records known children.  The subsequent waitpid also
        # collects adopted grandchildren and any known child that exited after poll.
        for child in self.running.values():
            child.process.poll()
        by_pid = {child.process.pid: child.process for child in self.running.values()}
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if pid in by_pid:
                by_pid[pid].returncode = os.waitstatus_to_exitcode(status)

    def _write_status(self) -> None:
        status = {
            "version": 1,
            "supervisor_pid": os.getpid(),
            "observed_at": time.time(),
            "phase": self.phase,
            "components": {
                name: {
                    "pid": child.process.pid,
                    "started_at": child.started_at,
                    "returncode": child.process.returncode,
                    "required": child.component.required,
                }
                for name, child in self.running.items()
                if name != "migration"
            },
        }
        self.status_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.status_file.with_name(f".{self.status_file.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(status, output)
        os.replace(temporary, self.status_file)

    @staticmethod
    def _signal_group(child: RunningComponent, signum: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(child.process.pid, signum)

    @staticmethod
    def _group_exists(child: RunningComponent) -> bool:
        try:
            os.killpg(child.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Darwin can return EPERM between signal delivery and reaping. The group
            # still exists; the next poll distinguishes exit from a live descendant.
            return True
        return True

    def _shutdown(self) -> None:
        self.phase = "stopping"
        # Let the worker stop claims and use its normal bounded drain, rather than
        # terminating an active TeX child immediately with a process-group signal.
        for child in self.running.values():
            if child.process.poll() is None:
                with suppress(ProcessLookupError):
                    child.process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + self.stop_timeout_seconds
        while True:
            self._reap()
            for child in self.running.values():
                if child.process.returncode is not None:
                    self._signal_group(child, signal.SIGTERM)
            if not any(self._group_exists(child) for child in self.running.values()):
                break
            if time.monotonic() >= deadline:
                for child in self.running.values():
                    self._signal_group(child, signal.SIGKILL)
                break
            self._write_status()
            time.sleep(self.poll_seconds)
        # SIGKILL is asynchronous. Reap direct and adopted children within a bounded
        # final window; Docker removes any uninterruptible process with the container.
        kill_deadline = time.monotonic() + 2
        while time.monotonic() < kill_deadline:
            self._reap()
            if not any(self._group_exists(child) for child in self.running.values()):
                break
            time.sleep(self.poll_seconds)
        self._write_status()

    def run(self) -> int:
        try:
            self.phase = "migrating"
            migration = self._spawn(Component("migration", self.migration_command))
            while migration.process.poll() is None:
                self._write_status()
                if self.stop_event.wait(self.poll_seconds):
                    return 0
                self._reap()
            if migration.process.returncode != 0:
                raise ServerProcessError("SERVER_MIGRATION_FAILED")
            if self.stop_event.is_set():
                return 0
            self.phase = "starting"
            for component in self.components:
                if self.stop_event.is_set():
                    return 0
                self._spawn(component)
            self.phase = "running"
            while not self.stop_event.is_set():
                self._reap()
                for child in self.running.values():
                    if child.component.name == "migration":
                        continue
                    code = child.process.returncode
                    if code is not None and (child.component.required or code != 0):
                        raise ServerProcessError("SERVER_COMPONENT_EXITED")
                self._write_status()
                self.stop_event.wait(self.poll_seconds)
            return 0
        finally:
            self._shutdown()



def web_environment(
    environment: Mapping[str, str], *, api_port: int, web_port: int,
) -> dict[str, str]:
    """Next receives only its transport token, never credential master/provider keys."""
    allowed = (
        "PATH", "HOME", "INTERNAL_SERVICE_TOKEN", "WEB_PUBLIC_ORIGIN",
        "WEB_CLIENT_ORIGINS", "WEB_ALLOW_INSECURE_HTTP",
    )
    result = {key: environment[key] for key in allowed if key in environment}
    result.update({
        "NODE_ENV": "production",
        "NEXT_TELEMETRY_DISABLED": "1",
        "PORT": str(web_port),
        "HOSTNAME": environment.get("SERVER_WEB_BIND_HOST", "0.0.0.0"),
        "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}",
    })
    return result

def main() -> int:
    try:
        apply_server_defaults(os.environ)
        from app.config import get_settings
        from app.domain.runtime import IdentityMode, RuntimeEdition

        https_enabled = bool(os.environ.get("SERVER_HTTPS_API_ORIGIN"))
        get_settings.cache_clear()
        settings = get_settings()
        profile = settings.validate_runtime_configuration()
        if settings.internal_service_token is None:
            raise ServerProcessError("SERVER_INTERNAL_TRANSPORT_TOKEN_REQUIRED")
        if (
            profile.edition is not RuntimeEdition.SERVER
            or profile.identity_mode is not IdentityMode.AUTHENTICATED
        ):
            raise ServerProcessError("SERVER_PROFILE_INVALID")
        port = int(os.environ.get("SERVER_API_PORT", str(settings.app_port)))
        if not 1 <= port <= 65535:
            raise ValueError("invalid API port")
        timeout = float(os.environ.get("SERVER_STOP_TIMEOUT_SECONDS", "30"))
        web_enabled_value = os.environ.get("SERVER_WEB_ENABLED", "false").lower()
        if web_enabled_value not in {"true", "false"}:
            raise ValueError("invalid web enable flag")
        web_enabled = web_enabled_value == "true"
        if https_enabled and not web_enabled:
            raise ValueError("Bundled HTTPS requires the Web component")
        api_bind_host = os.environ.get(
            "SERVER_API_BIND_HOST", "127.0.0.1" if web_enabled else settings.app_host,
        )
        components = [
            Component("api", (
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", api_bind_host, "--port", str(port),
            )),
            Component("worker", (sys.executable, "-m", "app.workers.main")),
            Component(
                "verifier", (sys.executable, "-m", "app.container_setup"), required=False,
            ),
        ]
        if web_enabled:
            web_port = int(os.environ.get("SERVER_WEB_PORT", "3000"))
            if not 1 <= web_port <= 65535 or web_port == port:
                raise ValueError("invalid web port")
            web_root = Path(os.environ.get("SERVER_WEB_ROOT", "/opt/academate-web"))
            web_entry = Path(os.environ.get("SERVER_WEB_ENTRY", str(web_root / "server.js")))
            if not web_entry.is_absolute():
                raise ValueError("invalid Web entry path")
            components.append(Component(
                "web", ("node", str(web_entry)),
                environment=web_environment(os.environ, api_port=port, web_port=web_port),
            ))
            if https_enabled:
                from app.server_https import configure_proxy

                command, proxy_environment = configure_proxy(
                    os.environ, web_port=web_port, api_port=port,
                )
                components.append(Component("https", command, environment=proxy_environment))
        manager = ServerProcessManager(
            migration_command=(sys.executable, "-m", "alembic", "upgrade", "head"),
            components=components,
            status_file=Path(os.environ.get("SERVER_STATUS_FILE", str(DEFAULT_STATUS_FILE))),
            stop_timeout_seconds=timeout,
        )
        signal.signal(signal.SIGTERM, manager.request_stop)
        signal.signal(signal.SIGINT, manager.request_stop)
        become_child_subreaper()
        with installation_lock(installation_lock_path(settings.database_url)):
            return manager.run()
    except ServerProcessError as error:
        print(f"server failure={error}", file=sys.stderr)
    except (ValueError, OSError):
        print("server failure=SERVER_CONFIGURATION_OR_STORAGE_INVALID", file=sys.stderr)
    except Exception:
        print("server failure=SERVER_STARTUP_UNAVAILABLE", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
