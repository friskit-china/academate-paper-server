#!/usr/bin/env python3
"""Enable or disable the personal Server's TLS listener in an existing direct installation."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
from pathlib import Path

VERSION = "2.11.4"
# Official caddyserver/caddy release checksums; these inputs also cover the Docker image version.
PLATFORMS = {
    ("Darwin", "arm64"): ("mac_arm64", "9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7"),
    ("Darwin", "x86_64"): ("mac_amd64", "34bc9e5cceee8d67844ef51da624f5b79e8d070f27236e050c3f0066a2dba534"),
    ("Linux", "aarch64"): ("linux_arm64", "52d42ae12b3462097e9868da6dfed3c9648ae12edd3b3638102312af84cb6904"),
    ("Linux", "x86_64"): ("linux_amd64", "527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9"),
}


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
    with tempfile.TemporaryDirectory(dir=root / "bin") as tmp, tarfile.open(archive) as packed:
        candidate = Path(tmp) / "caddy"
        member = packed.getmember("caddy")
        if not member.isfile():
            raise ValueError("Invalid Caddy archive")
        candidate.write_bytes(packed.extractfile(member).read())
        candidate.chmod(0o700)
        candidate.replace(binary)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Direct installation directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--web-origin", help="Public HTTPS origin of the bundled Web")
    mode.add_argument("--http-loopback", action="store_true", help="Restore local HTTP")
    parser.add_argument("--api-origin", help="Public HTTPS origin for native apps")
    parser.add_argument("--cert", type=Path, help="Full-chain PEM, accessible to the service account")
    parser.add_argument("--key", type=Path, help="Matching private key PEM")
    args = parser.parse_args()
    root = args.root.resolve()
    config = root / "instance.json"
    if not (root / ".academate-personal-server").is_file() or config.is_symlink():
        parser.error("Choose the root of a direct installation")
    settings = json.loads(config.read_text())
    if args.http_loopback:
        if args.api_origin or args.cert or args.key:
            parser.error("--http-loopback takes no TLS settings")
        for key in ("SERVER_HTTPS_API_ORIGIN", "SERVER_HTTPS_CERT_FILE", "SERVER_HTTPS_KEY_FILE",
                    "SERVER_HTTPS_CADDY"):
            settings.pop(key, None)
        settings.update(WEB_PUBLIC_ORIGIN=f"http://127.0.0.1:{settings['SERVER_WEB_PORT']}",
                        WEB_SESSION_COOKIE_SECURE="false", RUNTIME_NETWORK_SCOPE="LOOPBACK")
    else:
        if not args.api_origin or bool(args.cert) != bool(args.key):
            parser.error("Set both origins and either both PEM files or neither")
        import sys
        sys.path.insert(0, str(root / "current" / "backend"))
        from app.server_https import configure_proxy

        settings.update(WEB_PUBLIC_ORIGIN=args.web_origin, SERVER_HTTPS_API_ORIGIN=args.api_origin,
                        RUNTIME_NETWORK_SCOPE="PUBLIC", WEB_SESSION_COOKIE_SECURE="true",
                        WEB_ALLOW_INSECURE_HTTP="false", WEB_CLIENT_ORIGINS="")
        settings["SERVER_HTTPS_CERT_FILE"] = str(args.cert.resolve(strict=True)) if args.cert else ""
        settings["SERVER_HTTPS_KEY_FILE"] = str(args.key.resolve(strict=True)) if args.key else ""
        binary = install_proxy(root)
        settings["SERVER_HTTPS_CADDY"] = str(binary)
        # Validate syntax, host routing and provided credentials before replacing instance config.
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            check = {**settings, "PATH": os.defpath, "STORAGE_ROOT": tmp}
            command, env = configure_proxy(check, web_port=int(settings["SERVER_WEB_PORT"]),
                                           api_port=int(settings["SERVER_API_PORT"]))
            subprocess.run((command[0], "validate", *command[2:]), env=env, check=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False, encoding="utf-8") as temp:
        json.dump(settings, temp, indent=2)
        temp.write("\n")
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, config)
    print("Configuration saved. Run amp stop, then amp start to apply it.")


if __name__ == "__main__":
    main()
