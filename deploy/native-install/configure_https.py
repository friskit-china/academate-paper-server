#!/usr/bin/env python3
"""Enable or disable the personal Server's TLS listener in an existing direct installation."""

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tarfile
import tempfile
from ipaddress import ip_address
from pathlib import Path

VERSION = "2.11.4"
# Official caddyserver/caddy release checksums; these inputs also cover the Docker image version.
PLATFORMS = {
    ("Darwin", "arm64"): ("mac_arm64", "9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7"),
    ("Darwin", "x86_64"): ("mac_amd64", "34bc9e5cceee8d67844ef51da624f5b79e8d070f27236e050c3f0066a2dba534"),
    ("Linux", "aarch64"): ("linux_arm64", "52d42ae12b3462097e9868da6dfed3c9648ae12edd3b3638102312af84cb6904"),
    ("Linux", "x86_64"): ("linux_amd64", "527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9"),
}

ROOT_CERT = Path("https/caddy/pki/authorities/local/root.crt")
TRANSPORT_KEYS = (
    "WEB_PUBLIC_ORIGIN", "SERVER_HTTPS_API_ORIGIN", "SERVER_HTTPS_CERT_FILE",
    "SERVER_HTTPS_KEY_FILE", "SERVER_HTTPS_MODE", "SERVER_HTTPS_BIND_HOST",
    "SERVER_WEB_PORT", "SERVER_API_PORT", "APP_PORT", "WEB_SESSION_COOKIE_SECURE",
    "WEB_ALLOW_INSECURE_HTTP", "WEB_CLIENT_ORIGINS", "RUNTIME_NETWORK_SCOPE",
    "SERVER_HTTP_LOOPBACK_WEB_PORT", "SERVER_HTTP_LOOPBACK_API_PORT",
)


def install_proxy(root):
    target, digest = PLATFORMS[(platform.system(), platform.machine())]
    binary = root / "bin" / f"caddy-{VERSION}"
    name = f"caddy_{VERSION}_{target}.tar.gz"
    cache = root / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    archive = cache / name
    if not archive.exists():
        with tempfile.TemporaryDirectory(dir=cache) as tmp:
            download = Path(tmp) / name
            subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                            "--proto", "=https", "--retry", "3", "-o", str(download),
                            f"https://github.com/caddyserver/caddy/releases/download/v{VERSION}/{name}"],
                           check=True)
            if hashlib.sha256(download.read_bytes()).hexdigest() != digest:
                raise ValueError("Caddy archive checksum mismatch")
            download.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("Caddy archive checksum mismatch")
    binary.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=binary.parent) as tmp, tarfile.open(archive) as packed:
        candidate = Path(tmp) / "caddy"
        member = packed.getmember("caddy")
        if not member.isfile():
            raise ValueError("Invalid Caddy archive")
        candidate.write_bytes(packed.extractfile(member).read())
        candidate.chmod(0o700)
        candidate.replace(binary)
    return binary


def add_transport_options(parser):
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--web-origin", help="HTTPS address used by the browser")
    mode.add_argument("--http-loopback", action="store_true", help="Use local HTTP")
    parser.add_argument("--api-origin", help="HTTPS address used by native apps")
    parser.add_argument("--cert", type=Path, help="Full-chain PEM")
    parser.add_argument("--key", type=Path, help="Matching private key PEM")
    parser.add_argument("--acme", action="store_true", help="Use public ACME certificates")


def check_options(parser, args):
    if bool(args.cert) != bool(args.key):
        parser.error("Set both --cert and --key")
    if bool(args.web_origin) != bool(args.api_origin):
        parser.error("Set both --web-origin and --api-origin")
    if args.cert and (not args.cert.is_file() or not args.key.is_file()):
        parser.error("Both PEM paths must name files readable by this user")
    if args.acme and args.cert:
        parser.error("--acme cannot be combined with --cert and --key")
    if args.http_loopback and (args.api_origin or args.cert or args.acme):
        parser.error("--http-loopback takes no TLS settings")


def configure_http(settings, web_port, api_port):
    if web_port == api_port:
        raise ValueError("API and Web require different loopback ports")
    for key in list(settings):
        if key.startswith("SERVER_HTTPS_"):
            del settings[key]
    settings.update(WEB_PUBLIC_ORIGIN=f"http://127.0.0.1:{web_port}",
                    SERVER_WEB_PORT=str(web_port), SERVER_API_PORT=str(api_port), APP_PORT=str(api_port),
                    SERVER_WEB_BIND_HOST="127.0.0.1", SERVER_API_BIND_HOST="127.0.0.1",
                    WEB_SESSION_COOKIE_SECURE="false", WEB_ALLOW_INSECURE_HTTP="false",
                    WEB_CLIENT_ORIGINS="", RUNTIME_NETWORK_SCOPE="LOOPBACK")


def private_ports(reserved, preferred):
    """Allocate and persist private ports without colliding with the HTTPS listeners."""
    occupied = set(reserved)
    ports = []
    for value in preferred:
        port = int(value) if value else None
        while not port or port in occupied:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        ports.append(port)
        occupied.add(port)
    return ports


def configure_tls(settings, web, api, *, cert=None, key=None, acme=False):
    from app.server_https import authority

    web_host, web_tls = authority(web)
    api_host, api_tls = authority(api)
    if (web_host, web_tls) == (api_host, api_tls):
        raise ValueError("Web and API require distinct HTTPS origins")
    bind = set()
    for host in (web_host, api_host):
        if host == "localhost":
            bind.add("127.0.0.1")
        else:
            try:
                address = ip_address(host)
            except ValueError:
                continue
            if address.is_loopback:
                bind.add(str(address))
    if bind and (len(bind) > 1 or not {web_host, api_host}.issubset(bind | {"localhost"})):
        raise ValueError("Use the same loopback address on both origins, or two LAN/public origins")
    if acme and bind:
        raise ValueError("Use the local CA for loopback addresses")
    if bind:
        settings["SERVER_HTTPS_BIND_HOST"] = bind.pop()
    else:
        settings.pop("SERVER_HTTPS_BIND_HOST", None)
    if settings.get("WEB_PUBLIC_ORIGIN", "").startswith("http://"):
        settings["SERVER_HTTP_LOOPBACK_WEB_PORT"] = settings["SERVER_WEB_PORT"]
        settings["SERVER_HTTP_LOOPBACK_API_PORT"] = settings["SERVER_API_PORT"]
    web_port, api_port = private_ports(
        {web_tls, api_tls}, (settings.get("SERVER_WEB_PORT"), settings.get("SERVER_API_PORT"))
    )
    settings.update(
        WEB_PUBLIC_ORIGIN=web.rstrip("/"), SERVER_HTTPS_API_ORIGIN=api.rstrip("/"),
        RUNTIME_NETWORK_SCOPE="PUBLIC", WEB_SESSION_COOKIE_SECURE="true",
        SERVER_WEB_BIND_HOST="127.0.0.1", SERVER_API_BIND_HOST="127.0.0.1",
        SERVER_WEB_PORT=str(web_port), SERVER_API_PORT=str(api_port), APP_PORT=str(api_port),
        WEB_ALLOW_INSECURE_HTTP="false", WEB_CLIENT_ORIGINS="",
        # Retain symlink paths so an operator can atomically replace a renewed PEM.
        SERVER_HTTPS_CERT_FILE=str(cert.absolute()) if cert else "",
        SERVER_HTTPS_KEY_FILE=str(key.absolute()) if key else "",
        SERVER_HTTPS_MODE="provided" if cert else ("acme" if acme else "internal"),
    )


def install_tls_proxy(root, settings):
    from app.server_https import configure_proxy

    settings["SERVER_HTTPS_CADDY"] = str(install_proxy(root))
    # Validate with disposable storage so a failed reconfiguration cannot alter the live PKI.
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        check = {**settings, "PATH": os.defpath, "STORAGE_ROOT": tmp}
        command, env = configure_proxy(check, web_port=int(settings["SERVER_WEB_PORT"]),
                                       api_port=int(settings["SERVER_API_PORT"]))
        subprocess.run((command[0], "validate", *command[2:]), env=env, check=True)


def show_access(settings):
    web = settings["WEB_PUBLIC_ORIGIN"]
    api = settings.get("SERVER_HTTPS_API_ORIGIN") or f"http://127.0.0.1:{settings['SERVER_API_PORT']}"
    print(f"Web: {web}   API: {api}", flush=True)
    if settings.get("SERVER_HTTPS_MODE") == "internal":
        cert = Path(settings["STORAGE_ROOT"]) / ROOT_CERT
        print(f"首次启动后，请在客户端的系统或浏览器中将此根证书导入为受信任的证书颁发机构：{cert}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Direct installation directory")
    add_transport_options(parser)
    args = parser.parse_args()
    check_options(parser, args)
    root = args.root.resolve()
    config = root / "instance.json"
    if not (root / ".academate-personal-server").is_file() or config.is_symlink():
        parser.error("Choose the root of a direct installation")
    settings = json.loads(config.read_text())
    if args.http_loopback:
        if settings.get("SERVER_HTTPS_API_ORIGIN"):
            web_port = int(settings.get("SERVER_HTTP_LOOPBACK_WEB_PORT", "3000"))
            api_port = int(settings.get("SERVER_HTTP_LOOPBACK_API_PORT", "8000"))
        else:
            web_port, api_port = int(settings["SERVER_WEB_PORT"]), int(settings["SERVER_API_PORT"])
        configure_http(settings, web_port, api_port)
    else:
        sys.path.insert(0, str(root / "current" / "backend"))
        existing = settings.get("SERVER_HTTPS_API_ORIGIN")
        web = args.web_origin or (settings["WEB_PUBLIC_ORIGIN"] if existing else "https://127.0.0.1:8443")
        api = args.api_origin or existing or "https://127.0.0.1:9443"
        configure_tls(settings, web, api, cert=args.cert, key=args.key, acme=args.acme)
        install_tls_proxy(root, settings)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False, encoding="utf-8") as temp:
        json.dump(settings, temp, indent=2)
        temp.write("\n")
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, config)
    print("配置已保存。请运行 amp stop，再运行 amp start 应用设置。")
    show_access(settings)


if __name__ == "__main__":
    main()
