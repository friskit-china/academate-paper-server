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

from app.server import SERVER_DEFAULTS, apply_server_defaults
from app.server_https import any_host_enabled, authority, https_origins

KEYS = ("INTERNAL_SERVICE_TOKEN", "CREDENTIAL_INSTALLATION_ID", "CREDENTIAL_MASTER_KEY_B64")
MODULES = {
    "serve": "app.amp_daemon",
    "health": "app.server_health",
    "amp": "app.amp",
    "setup": "app.setup_cli",
}


def _select_private_ports(environment: MutableMapping[str, str], reserved: set[int]) -> None:
    """Allow operators to publish HTTPS on the container's usual private ports."""
    web = environment.get("SERVER_WEB_PORT")
    api = environment.get("SERVER_API_PORT") or environment.get("APP_PORT")
    used = {*reserved, *(int(value) for value in (web, api) if value)}
    for key, value, default in (("SERVER_WEB_PORT", web, 3000), ("SERVER_API_PORT", api, 8000)):
        if not value:
            port = default
            if port in used:
                port = next(port for port in range(18000, 65536) if port not in used)
                environment[key] = str(port)
            used.add(port)


def apply_container_network(environment: MutableMapping[str, str], *, initialize: bool) -> None:
    root = Path(environment.get("STORAGE_ROOT", SERVER_DEFAULTS["STORAGE_ROOT"]))
    # Store the default with the volume, before initializing identity or database.
    # In particular, a later "docker exec ... setup" starts with the image's plain
    # environment and needs to issue a link for the same origin as the supervisor.
    marker = root / ".amp-default-https"
    if initialize and not marker.exists() and not (root / ".amp-instance.json").exists():
        database = root / "database/workspace.sqlite3"
        url = make_url(environment.get("DATABASE_URL", f"sqlite:///{database}"))
        if (not database.exists() and url.get_backend_name() == "sqlite"
                and url.database and not Path(url.database).exists()):
            marker.touch(mode=0o600, exist_ok=True)
    tls_inputs = any(environment.get(name) for name in (
        "SERVER_HTTPS_MODE", "SERVER_HTTPS_CERT_FILE", "SERVER_HTTPS_KEY_FILE",
    ))
    web = environment.get("WEB_PUBLIC_ORIGIN")
    api = environment.get("SERVER_HTTPS_API_ORIGIN")
    use_default_tls = (
        not web and marker.is_file()
        and environment.get("SERVER_WEB_ENABLED", "true").lower() == "true"
    )
    aliases = any(environment.get(key) for key in ("SERVER_HTTPS_WEB_ALIASES",
                                                    "SERVER_HTTPS_API_ALIASES"))
    if use_default_tls or tls_inputs or api or aliases or any_host_enabled(environment):
        web = web or "https://127.0.0.1:8443"
        api = api or "https://127.0.0.1:9443"
    else:
        web = web or "http://127.0.0.1:3000"
    environment["WEB_PUBLIC_ORIGIN"] = web
    if api:
        environment["SERVER_HTTPS_API_ORIGIN"] = api
        web_origins, api_origins = https_origins(environment)
        reserved = {authority(origin)[1] for origin in web_origins + api_origins}
        _select_private_ports(environment, reserved)
        # A pre-0.1.2 installation without supplied PEMs used Caddy's automatic
        # issuer. Keep its trusted certificates on recreation with the same origins.
        if not marker.is_file() and not tls_inputs:
            config = root / "https" / "Caddyfile"
            if config.is_file():
                previous = config.read_text().splitlines()
                sites = {
                    origin for line in previous
                    if line.startswith("https://") and line.endswith(" {")
                    for origin in line[:-2].split(", ")
                }
                if (sites.intersection((web.rstrip("/"), web_origins[0]))
                        and sites.intersection((api.rstrip("/"), api_origins[0]))
                        and not any(line.lstrip().startswith("tls ") for line in previous)):
                    environment["SERVER_HTTPS_MODE"] = "acme"
    if not environment.get("CORS_ORIGINS"):
        environment["CORS_ORIGINS"] = web


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
    try:
        apply_container_network(os.environ, initialize=action == "serve")
        apply_server_defaults(os.environ)
        load_installation(os.environ, initialize=action == "serve")
    except (OSError, ValueError) as error:
        if isinstance(error, (OSError, json.JSONDecodeError)):
            print("Cannot read or create the private instance configuration", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1
    if (action == "setup" and "prepare-container-setup" in sys.argv[2:]
            and os.environ.get("SERVER_HTTPS_API_ORIGIN")
            and os.environ.get("SERVER_HTTPS_MODE", "") in ("", "internal")
            and not os.environ.get("SERVER_HTTPS_CERT_FILE")):
        ca = Path(os.environ["STORAGE_ROOT"]) / "https/caddy/pki/authorities/local/root.crt"
        print(f"HTTPS uses this instance's local CA. Before opening the setup URL, "
              f"copy {ca} from the container and trust it on your device.", file=sys.stderr)
    os.execv(sys.executable, [sys.executable, "-m", MODULES[action], *sys.argv[2:]])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
