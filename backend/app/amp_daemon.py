"""Local service control for the packaged Server on macOS, Linux and Docker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
from contextlib import closing, suppress
from pathlib import Path

from app.server import (
    DEFAULT_STATUS_FILE,
    ServerProcessError,
    apply_server_defaults,
    installation_lock,
    installation_lock_path,
)


def run_directory() -> Path:
    return Path(os.environ["STORAGE_ROOT"]).resolve() / "run"


def socket_path() -> Path:
    """Each instance has a private short socket path derived from its canonical database."""
    identity = str(installation_lock_path(os.environ["DATABASE_URL"]).resolve())
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:32]
    return Path("/tmp") / f"amp-{os.geteuid()}-{digest}" / "control.sock"


def prepare_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    entry = path.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid() or entry.st_mode & 0o077:
        raise ServerProcessError("AMP_CONTROL_DIRECTORY_NOT_PRIVATE")


def control(command: str) -> dict | None:
    """None means no controller is listening. Other failures must not start a second one."""
    path = socket_path()
    if not path.parent.exists():
        return None
    prepare_directory(path.parent)
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as client:
        client.settimeout(float(os.environ.get("SERVER_STOP_TIMEOUT_SECONDS", "30")) + 180)
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        client.sendall(command.encode("ascii") + b"\n")
        with client.makefile("rb") as response:
            data = json.loads(response.readline(65536))
        if "error" in data:
            raise ServerProcessError(data["error"])
        return data


class Controller:
    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.quitting = False
        self.status_file = Path(os.environ.get("SERVER_STATUS_FILE", str(DEFAULT_STATUS_FILE)))

    def processes(self) -> dict:
        if self.child is None or self.child.poll() is not None:
            return {"server": "stopped", "components": {}}
        try:
            with self.status_file.open("rb") as stream:
                status = json.loads(stream.read(65536))
            if status["supervisor_pid"] == self.child.pid:
                return {
                    "server": status["phase"], "pid": self.child.pid,
                    "components": status["components"],
                }
        except (OSError, ValueError, KeyError):
            pass
        return {"server": "starting", "pid": self.child.pid, "components": {}}

    def start(self) -> dict:
        if self.child is None or self.child.poll() is not None:
            self.child = subprocess.Popen([sys.executable, "-m", "app.server"])
        return self.processes()

    def stop(self) -> dict:
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            # Allow the Server manager to drain, then terminate its own process groups.
            timeout = float(os.environ.get("SERVER_STOP_TIMEOUT_SECONDS", "30")) + 6
            try:
                self.child.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise ServerProcessError("AMP_SERVER_STOP_INCOMPLETE") from error
        self.child = None
        return self.processes()

    def request_exit(self, *_args) -> None:
        self.quitting = True

    def serve(self, *, detached: bool) -> int:
        path = socket_path()
        prepare_directory(path.parent)
        prepare_directory(run_directory())
        with installation_lock(path.parent / "controller.lock"):
            if path.exists() and not stat.S_ISSOCK(path.lstat().st_mode):
                raise ServerProcessError("AMP_CONTROL_SOCKET_INVALID")
            path.unlink(missing_ok=True)
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(8)
                listener.settimeout(0.3)
                try:
                    # Docker starts the application by default. A detached host controller waits
                    # for the initiating amp start, so that errors reach its terminal.
                    if not detached:
                        self.start()
                    while not self.quitting:
                        if self.child is not None and self.child.poll() is not None:
                            return 1  # retain the container restart policy for unexpected exits
                        try:
                            conn, _ = listener.accept()
                        except TimeoutError:
                            continue
                        with conn:
                            conn.settimeout(2)
                            try:
                                with conn.makefile("rb") as stream:
                                    command = stream.readline(32).decode("ascii").strip()
                                report = self.dispatch(command)
                            except ServerProcessError as error:
                                report = {"error": str(error)}
                            except (OSError, UnicodeError):
                                continue
                            retiring = detached and self.child is None and command == "stop"
                            # Stop accepting connections before acknowledging shutdown. A
                            # following host start will wait for this controller's lock.
                            if retiring:
                                listener.close()
                            with suppress(OSError):
                                conn.sendall(json.dumps(report).encode("utf-8") + b"\n")
                            if retiring:
                                return 0
                    return 0
                finally:
                    path.unlink(missing_ok=True)
                    self.stop()

    def dispatch(self, command: str) -> dict:
        if command == "status":
            return self.processes()
        if command == "stop":
            return self.stop()
        if command == "restart":
            self.stop()
            return self.start()
        if command == "start":
            return self.start()
        raise ServerProcessError("AMP_CONTROL_COMMAND_UNKNOWN")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detached", action="store_true")
    args = parser.parse_args()
    apply_server_defaults(os.environ)
    controller = Controller()
    signal.signal(signal.SIGTERM, controller.request_exit)
    signal.signal(signal.SIGINT, controller.request_exit)
    try:
        return controller.serve(detached=args.detached)
    except ServerProcessError as error:
        print(f"amp: {error}", file=sys.stderr)
    except (OSError, ValueError):
        print("amp: controller configuration or storage unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
