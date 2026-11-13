"""TLS frontend for the packaged Server, with two HTTPS origins and private HTTP upstreams."""

import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit


def authority(origin: str) -> tuple[str, int]:
    parsed = urlsplit(origin)
    host = parsed.hostname
    if (
        parsed.scheme != "https" or not host or parsed.username is not None
        or parsed.password is not None or parsed.path not in ("", "/")
        or any(c in origin for c in "?#%") or parsed.netloc.endswith(":")
        or any(c.isspace() or ord(c) < 32 or c in '{}"\\' for c in origin)
    ):
        raise ValueError("HTTPS origin must contain a host and optional port")
    try:
        host = ip_address(host).compressed
    except ValueError:
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in host.split(".")):
            raise ValueError("Invalid HTTPS host") from None
    port = parsed.port if parsed.port is not None else 443
    if not 1 <= port <= 65535:
        raise ValueError("Invalid HTTPS port")
    return host, port


def configure_proxy(environment: Mapping[str, str], *, web_port: int, api_port: int):
    web = environment["WEB_PUBLIC_ORIGIN"].rstrip("/")
    api = environment["SERVER_HTTPS_API_ORIGIN"].rstrip("/")
    web_host, web_tls = authority(web)
    api_host, api_tls = authority(api)
    if (web_host, web_tls) == (api_host, api_tls) or {web_tls, api_tls} & {web_port, api_port}:
        raise ValueError("Use distinct HTTPS origins and backend ports")
    certificate = environment.get("SERVER_HTTPS_CERT_FILE", "")
    key = environment.get("SERVER_HTTPS_KEY_FILE", "")
    if bool(certificate) != bool(key):
        raise ValueError("Both certificate and private key are required")
    mode = environment.get("SERVER_HTTPS_MODE") or ("provided" if certificate else "internal")
    if mode not in {"internal", "provided", "acme"} or bool(certificate) != (mode == "provided"):
        raise ValueError("Use internal or acme without PEM files, or provided with both PEM files")
    if 8080 in (web_port, api_port, web_tls, api_tls) and mode == "acme":
        raise ValueError("Automatic HTTPS reserves 8080 for ACME HTTP challenges")
    # Caddyfile has interpolation and quoting of its own. Certificate paths are the only
    # free-form values pasted into directives, so accept only paths with literal characters.
    tls = "    tls internal\n" if mode == "internal" else ""
    if certificate:
        for path in (certificate, key):
            if not Path(path).is_absolute() or any(ord(c) < 32 or c in '{}"\\' for c in path):
                raise ValueError("Invalid TLS file path")
        tls = f'    tls "{certificate}" "{key}"\n'
    # Avoid installing a root in a host trust store. Caddy stores managed certs and ACME
    # account material under the instance data volume and reloads renewed certificates.
    config = """{
    admin off
    skip_install_trust
    auto_https disable_redirects
"""
    if mode == "acme":
        config += "    http_port 8080\n"
    elif 80 in (web_tls, api_tls):
        # Even without HTTP, Caddy refuses an HTTPS listener on its configured
        # HTTP port. Pick an unused port convention for this explicit TLS origin.
        other = 8080 if 8080 not in (web_tls, api_tls) else 8081
        config += f"    http_port {other}\n"
    # Clients connecting by IP omit SNI; with container NAT their destination IP
    # is also unavailable to Caddy. DNS clients send SNI and select their own leaf.
    numeric_hosts = set()
    for host in (web_host, api_host):
        with suppress(ValueError):
            numeric_hosts.add(str(ip_address(host)))
    if mode == "internal" and len(numeric_hosts) > 1:
        raise ValueError("Use one IP with two ports, DNS names, or a PEM covering both IPs")
    if numeric_hosts or certificate:
        default_sni = next(iter(numeric_hosts)) if len(numeric_hosts) == 1 else web_host
        config += f"    default_sni {default_sni}\n"
    config += "}\n"
    bind = environment.get("SERVER_HTTPS_BIND_HOST")
    if bind and "%" in bind:
        raise ValueError("Use a numeric bind address without an interface scope")
    interface = f"    bind {ip_address(bind)}\n" if bind else ""
    config += f"""{web} {{
{interface}{tls}    reverse_proxy 127.0.0.1:{web_port}
}}
{api} {{
{interface}{tls}    route {{
        @browserCredentials header Cookie *
        respond @browserCredentials 403
        @native path /api/v2/*
        reverse_proxy @native 127.0.0.1:{api_port} {{
            header_up -X-Academate-Internal-Service-Token
            header_up -X-Academate-Client-Transport
        }}
        respond 404
    }}
}}
"""
    root = Path(environment["STORAGE_ROOT"]) / "https"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = root / "Caddyfile"
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False, encoding="utf-8") as stream:
        stream.write(config)
        temporary = stream.name
    os.replace(temporary, config_path)
    binary = environment.get("SERVER_HTTPS_CADDY", "caddy")
    child_env = {k: environment[k] for k in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
                 if environment.get(k)}
    child_env.update({"HOME": str(root), "XDG_DATA_HOME": str(root),
                      "XDG_CONFIG_HOME": str(root / "config")})
    return (binary, "run", "--config", str(config_path), "--adapter", "caddyfile"), child_env
