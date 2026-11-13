"""Load an installation's private configuration before starting a container component."""

import base64
import json
import os
import secrets
import stat
import sys
import tempfile
import uuid
from collections.abc import MutableMapping
from contextlib import suppress
from pathlib import Path

from sqlalchemy.engine import make_url

from app.server import apply_server_defaults

KEYS = ("INTERNAL_SERVICE_TOKEN", "CREDENTIAL_INSTALLATION_ID", "CREDENTIAL_MASTER_KEY_B64")
MODULES = {
    "serve": "app.amp_daemon",
    "health": "app.server_health",
    "amp": "app.amp",
    "setup": "app.setup_cli",
}


def load_installation(environment: MutableMapping[str, str], *, initialize: bool) -> None:
    # M7 Compose installations already supply private deployment values. A caller-supplied
    # token selects that profile even when the optional model credential store is unused.
    root = Path(environment["STORAGE_ROOT"])
    config = root / ".amp-instance.json"
    if not config.exists():
        if environment.get(KEYS[0]):
            return
        if any(environment.get(key) for key in KEYS[1:]):
            raise ValueError("Set the matching transport token for an externally managed instance")
        if not initialize:
            raise ValueError("Start the container to initialize the installation")
        standard_db = root / "database/workspace.sqlite3"
        url = make_url(environment.get("DATABASE_URL", f"sqlite:///{standard_db}"))
        if (
            url.get_backend_name() != "sqlite" or not url.database
            or not Path(url.database).is_absolute()
        ):
            raise ValueError("Expected an absolute SQLite database path")
        if standard_db.exists() or Path(url.database).exists():
            raise ValueError("Restore the matching instance configuration for this database")
        values = {
            KEYS[0]: secrets.token_urlsafe(48),
            KEYS[1]: str(uuid.uuid4()),
            KEYS[2]: base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        }
        # Publish a complete 0600 file exactly once, even if two starters use the same volume.
        with tempfile.NamedTemporaryFile(dir=root, mode="w", prefix=".amp-new-") as temp:
            json.dump(values, temp)
            temp.flush()
            os.fsync(temp.fileno())
            with suppress(FileExistsError):
                os.link(temp.name, config)
    if initialize:
        # A filesystem crash must never retain a database that references an unpublished key.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    with os.fdopen(os.open(config, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ValueError("Instance configuration must be private and owned by the service user")
        saved = json.loads(stream.read(4097))
    if not isinstance(saved, dict) or set(saved) != set(KEYS):
        raise ValueError("Restore the installation's original private configuration")
    for key in KEYS:
        value = saved[key]
        if not isinstance(value, str) or not value or environment.get(key) not in (None, "", value):
            raise ValueError("Conflicting or invalid installation configuration")
    environment.update(saved)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in MODULES:
        print("Expected serve, health, setup or amp", file=sys.stderr)
        return 2
    action = sys.argv[1]
    apply_server_defaults(os.environ)
    try:
        load_installation(os.environ, initialize=action == "serve")
    except (OSError, ValueError) as error:
        if isinstance(error, (OSError, json.JSONDecodeError)):
            print("Cannot read or create the private instance configuration", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1
    os.execv(sys.executable, [sys.executable, "-m", MODULES[action], *sys.argv[2:]])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
