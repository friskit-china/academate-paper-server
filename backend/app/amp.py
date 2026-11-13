"""Operate the AcadeMate Paper Server installed on this machine."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.amp_daemon import control, prepare_directory, run_directory
from app.application.runtime_verification import SchemaRevisionInvariantError
from app.config import get_settings
from app.domain.users import validated_password
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.db.client_models import ClientSessionModel
from app.infrastructure.db.models import InstallationStateModel, UserModel, WebSessionModel
from app.infrastructure.schema_revision import DatabaseSchemaRevisionInvariant
from app.infrastructure.security import ScryptPasswordHasher
from app.infrastructure.tasks import DatabaseTaskQueue
from app.infrastructure.worker_runtime import DatabaseWorkerHeartbeatStore
from app.server import ServerProcessError, apply_server_defaults, installation_lock_path
from app.server_health import _api_ready, _https_ready, _readonly_engine, _web_ready


def await_start(report: dict) -> dict:
    api_port = int(os.environ.get("SERVER_API_PORT", get_settings().app_port))
    web_port = int(os.environ.get("SERVER_WEB_PORT", "3000"))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (
            report["server"] == "running" and _api_ready(api_port)
            and ("web" not in report["components"] or _web_ready(web_port))
            and (not os.environ.get("SERVER_HTTPS_API_ORIGIN")
                 or ("https" in report["components"] and _https_ready(os.environ)))
        ):
            return report
        time.sleep(0.2)
        report = control("status")
        if report is None or report["server"] == "stopped":
            raise ServerProcessError("AMP_SERVER_START_FAILED")
    raise ServerProcessError("AMP_SERVER_START_INCOMPLETE")


def lifecycle(command: str) -> dict:
    run = run_directory()
    prepare_directory(run)
    # Serialize the complete invocation, including creation of the host controller.
    lock_fd = os.open(run / "command.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        result = control(command)
        if result is not None:
            return result if command == "stop" else await_start(result)
        if command == "stop":
            return {"server": "stopped", "components": {}}
        log_fd = os.open(run / "server.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600)
        with os.fdopen(log_fd, "ab") as log:
            controller = subprocess.Popen(
                [sys.executable, "-m", "app.amp_daemon", "--detached"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and controller.poll() is None:
            report = control("start")
            if report is not None:
                return await_start(report)
            time.sleep(0.1)
        raise ServerProcessError("AMP_CONTROLLER_START_FAILED")
    finally:
        os.close(lock_fd)


def status() -> dict:
    report = control("status") or {"server": "stopped", "components": {}}
    settings = get_settings()
    engine = _readonly_engine(settings.database_url)
    try:
        heartbeat = DatabaseWorkerHeartbeatStore(build_session_factory(engine)).latest()
    except (SQLAlchemyError, KeyError, ValueError):
        heartbeat = None
    finally:
        engine.dispose()
    if heartbeat is None:
        report["worker"] = {"heartbeat": "unavailable", "accepting_tasks": False}
        return report
    component = report["components"].get("worker", {})
    age = time.time() - heartbeat.observed_at.timestamp()
    fresh = (
        report["server"] == "running" and component.get("returncode") is None
        and component.get("started_at", float("inf")) <= heartbeat.observed_at.timestamp()
        and -5 <= age <= max(15, 3 * settings.worker_heartbeat_seconds)
    )
    report["worker"] = {
        **heartbeat.safe_summary(), "heartbeat": "fresh" if fresh else "stale",
        "accepting_tasks": fresh and heartbeat.accepting_tasks,
    }
    return report


def change_password(factory, password: str) -> None:
    new_hash = ScryptPasswordHasher().hash(validated_password(password))
    now = datetime.now(UTC)
    with factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        installation = session.get(InstallationStateModel, "installation")
        if not installation or installation.edition != "SERVER" or not installation.owner_user_id:
            raise ServerProcessError("AMP_OWNER_NOT_INITIALIZED")
        owner = session.get(UserModel, installation.owner_user_id)
        if owner is None or owner.status != "ACTIVE":
            raise ServerProcessError("AMP_OWNER_UNAVAILABLE")
        owner.password_hash = new_hash
        owner.auth_generation += 1
        owner.updated_at = now
        for model in (WebSessionModel, ClientSessionModel):
            values = {"revoked_at": now}
            if model is ClientSessionModel:
                values["revocation_reason"] = "PASSWORD_CHANGED"
            session.execute(update(model).where(
                model.user_id == owner.id, model.revoked_at.is_(None),
            ).values(**values))
        session.commit()


def read_password() -> str:
    if sys.stdin.isatty():
        new = getpass.getpass("New password: ")
        repeated = getpass.getpass("Repeat new password: ")
    else:
        # Pipes allow automation without arguments, environment variables or echo.
        new = sys.stdin.readline(4098).rstrip("\r\n")
        repeated = sys.stdin.readline(4098).rstrip("\r\n")
    if new != repeated:
        raise ServerProcessError("AMP_PASSWORD_CONFIRMATION_MISMATCH")
    try:
        return validated_password(new)
    except ValueError as error:
        raise ServerProcessError("AMP_PASSWORD_INVALID (12 to 1024 characters required)") from error


def operate(command: str) -> dict:
    apply_server_defaults(os.environ)
    database_url = get_settings().database_url
    installation_lock_path(database_url)  # require an absolute, persistent SQLite database
    if command in {"start", "restart", "stop"}:
        return lifecycle(command)
    if command == "status":
        return status()
    if not Path(make_url(database_url).database).is_file():
        raise ServerProcessError("AMP_INSTALLATION_NOT_INITIALIZED")
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        DatabaseSchemaRevisionInvariant(factory).verify()
        if command == "resume":
            return {"requeued": DatabaseTaskQueue(factory).resume_unfinished()}
        change_password(factory, read_password())
        return {"password_changed": True}
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amp", description=__doc__)
    parser.add_argument(
        "command", choices=["start", "stop", "restart", "status", "resume", "passwd"],
    )
    args = parser.parse_args(argv)
    try:
        report = operate(args.command)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except ServerProcessError as error:
        print(f"amp: {error}", file=sys.stderr)
    except (OSError, ValueError, EOFError, SQLAlchemyError, SchemaRevisionInvariantError):
        print("amp: installation configuration or storage unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
