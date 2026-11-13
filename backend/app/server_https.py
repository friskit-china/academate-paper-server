"""TLS frontend for the packaged Server, with two HTTPS origins and private HTTP upstreams."""

import os
import re
import tempfile
from collections.abc import Mapping
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
    if parsed.netloc.startswith("[") and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
        raise ValueError("Invalid bracketed HTTPS address")
    return host, port


def https_origins(environment: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return canonical Web and API origins followed by their explicitly configured aliases."""
    def group(primary: str, aliases: str) -> list[str]:
        values = [environment[primary], *(environment.get(aliases) or "").split(",")]
        origins = []
        for index, value in enumerate(values):
            if index and not value.strip():
                continue
            host, port = authority(value.strip(" ") if index else value)
            name = f"[{host}]" if ":" in host else host
            origin = f"https://{name}" + (f":{port}" if port != 443 else "")
            if origin not in origins:
                origins.append(origin)
        return origins

    web = group("WEB_PUBLIC_ORIGIN", "SERVER_HTTPS_WEB_ALIASES")
    api = group("SERVER_HTTPS_API_ORIGIN", "SERVER_HTTPS_API_ALIASES")
    if set(web).intersection(api):
        raise ValueError("Web and API require distinct HTTPS origins")
    return web, api


def validate_proxy_origins(environment: Mapping[str, str], *, web_port: int, api_port: int):
    web, api = https_origins(environment)
    if any_host_enabled(environment) and listener_ports(web) & listener_ports(api):
        raise ValueError("All-host HTTPS requires separate Web and API listener ports")
    authorities = [authority(origin) for origin in web + api]
    ports = {port for _, port in authorities}
    if ports.intersection((web_port, api_port)):
        raise ValueError("Use distinct HTTPS and backend ports")
    numeric_hosts = []
    for host, _ in authorities:
        try:
            ip_address(host)
        except ValueError:
            continue
        if host not in numeric_hosts:
            numeric_hosts.append(host)
    if len(numeric_hosts) > 1 and not environment.get("SERVER_HTTPS_CERT_FILE"):
        raise ValueError("Multiple IP addresses require a certificate and key covering all IPs")
    return web, api, ports, numeric_hosts


def any_host_enabled(environment: Mapping[str, str]) -> bool:
    value = environment.get("SERVER_HTTPS_ANY_HOST", "false").lower()
    if value not in {"true", "false", ""}:
        raise ValueError("SERVER_HTTPS_ANY_HOST must be true or false")
    return value == "true"


def listener_ports(origins: list[str]) -> set[int]:
    return {authority(origin)[1] for origin in origins}


def supports_any_host(environment: Mapping[str, str]) -> bool:
    web, api = https_origins(environment)
    return listener_ports(web).isdisjoint(listener_ports(api))


def configure_proxy(environment: Mapping[str, str], *, web_port: int, api_port: int):
    web, api, ports, numeric_hosts = validate_proxy_origins(
        environment, web_port=web_port, api_port=api_port,
    )
    certificate = environment.get("SERVER_HTTPS_CERT_FILE", "")
    key = environment.get("SERVER_HTTPS_KEY_FILE", "")
    if bool(certificate) != bool(key):
        raise ValueError("Both certificate and private key are required")
    mode = environment.get("SERVER_HTTPS_MODE") or ("provided" if certificate else "internal")
    if mode not in {"internal", "provided", "acme"} or bool(certificate) != (mode == "provided"):
        raise ValueError("Use internal or acme without PEM files, or provided with both PEM files")
    if 8080 in ports.union((web_port, api_port)) and mode == "acme":
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
    elif 80 in ports:
        # Even without HTTP, Caddy refuses an HTTPS listener on its configured
        # HTTP port. Pick an unused port convention for this explicit TLS origin.
        other = next(port for port in range(8080, 65536) if port not in ports)
        config += f"    http_port {other}\n"
    # Clients connecting by IP omit SNI; with container NAT their destination IP
    # is also unavailable to Caddy. DNS clients send SNI and select their own leaf.
    all_hosts = any_host_enabled(environment)
    if numeric_hosts or certificate or all_hosts:
        default_sni = numeric_hosts[0] if numeric_hosts else authority(web[0])[0]
        config += f"    default_sni {default_sni}\n"
        if all_hosts:
            # New SNI names receive an existing certificate; never issue one on demand.
            config += f"    fallback_sni {default_sni}\n"
    if all_hosts:
        # Keep named sites so Caddy manages their certificates, then accept any Host
        # header on the same listeners. Their disjoint ports determine the upstream.
        for sites in (web, api):
            sites.extend(f"https://:{port}" for port in sorted(listener_ports(sites)))
    config += "}\n"
    bind = environment.get("SERVER_HTTPS_BIND_HOST")
    if bind and "%" in bind:
        raise ValueError("Use a numeric bind address without an interface scope")
    interface = f"    bind {ip_address(bind)}\n" if bind else ""
    config += f"""{', '.join(web)} {{
{interface}{tls}    reverse_proxy 127.0.0.1:{web_port}
}}
{', '.join(api)} {{
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
