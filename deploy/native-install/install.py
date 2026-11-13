"""Complete an AcadeMate Paper private installation using its own Python."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from configure_https import (
    TRANSPORT_KEYS, add_transport_options, check_options, configure_http, configure_tls,
    install_tls_proxy, show_access,
)
from install_tex import install as install_tex

SOURCE = Path(__file__).resolve().parents[2]


def run(command, *, cwd, env):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def copy_sources(release):
    backend = release / "backend"
    backend.mkdir(exist_ok=True)
    for directory in ("app", "alembic"):
        shutil.copytree(
            SOURCE / "backend" / directory,
            backend / directory,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "._*", ".DS_Store"),
        )
    for filename in ("requirements.runtime.lock", "alembic.ini"):
        shutil.copy2(SOURCE / "backend" / filename, backend / filename)


def install_web(release, cache, env):
    frontend = release / "frontend"
    frontend.mkdir(exist_ok=True)
    # Build from source files only. Local config, credentials and host build outputs
    # are not inputs to a distribution.
    for name in ("app", "public", "scripts"):
        shutil.copytree(
            SOURCE / "frontend" / name,
            frontend / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("._*", ".DS_Store"),
        )
    for name in (
        "package.json",
        "pnpm-lock.yaml",
        "next-env.d.ts",
        "next.config.ts",
        "proxy.ts",
        "tsconfig.json",
    ):
        shutil.copy2(SOURCE / "frontend" / name, frontend / name)
    package_manager = json.loads((frontend / "package.json").read_text())[
        "packageManager"
    ]
    build_env = dict(
        env, COREPACK_HOME=str(cache / "corepack"), NEXT_TELEMETRY_DISABLED="1"
    )
    pnpm = ["corepack", package_manager]
    run(
        [*pnpm, "install", "--frozen-lockfile", "--store-dir", cache / "pnpm"],
        cwd=frontend,
        env=build_env,
    )
    run([*pnpm, "run", "build"], cwd=frontend, env=build_env)
    web = frontend / ".next" / "standalone"
    shutil.copytree(
        frontend / ".next" / "static", web / ".next" / "static", dirs_exist_ok=True
    )
    shutil.copytree(frontend / "public", web / "public", dirs_exist_ok=True)
    run(
        ["node", frontend / "scripts" / "smoke-next-standalone.mjs", web],
        cwd=frontend,
        env=build_env,
    )
    return web


def configure_toolchain(release):
    sys.path.insert(0, str(release / "backend"))
    from app.domain.os_adapters import ToolchainCapability, ToolchainCommands
    from app.infrastructure.platform.toolchain import ManagedManifestToolchainResolver

    entries = {}
    for name in ToolchainCommands.__dataclass_fields__:
        command = release / "texlive" / "bin" / name
        if not command.exists():
            command = release / "runtime" / "bin" / name
        if sys.platform == "darwin" and name == "biber":
            command = native_biber(command, release)
        if not command.resolve().is_relative_to(release) or not os.access(
            command, os.X_OK
        ):
            raise RuntimeError(f"Missing private runtime tool: {name}")
        with command.open("rb") as executable:
            digest = hashlib.file_digest(executable, "sha256").hexdigest()
        entries[name] = {"path": str(command.relative_to(release)), "sha256": digest}
    locked = dict(
        line.split("=", 1)
        for line in (SOURCE / "backend" / "texlive-runtime.lock")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    )
    manifest = release / "toolchain.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": locked["TEXLIVE_RUNTIME_ID"],
                "version": locked["TEXLIVE_RELEASE"],
                "commands": entries,
                "capabilities": [item.value for item in ToolchainCapability],
            },
            sort_keys=True,
        )
    )
    toolchain = ManagedManifestToolchainResolver(
        managed_root=release, manifest_path=manifest
    ).resolve()
    return {
        "TOOLCHAIN_MODE": "managed-manifest",
        "TOOLCHAIN_MANAGED_ROOT": str(release),
        "TOOLCHAIN_MANIFEST_PATH": str(manifest),
        "TOOLCHAIN_FINGERPRINT": toolchain.fingerprint,
    }


def native_biber(universal, release):
    # Biber's self-extracting universal executable runs /usr/bin/lipo on startup.
    # Ship the exact pre-signed architecture slice instead, so end users do not need
    # Xcode Command Line Tools. The enclosing artifact was verified against the TeX lock.
    machines = {"arm64": 0x0100000C, "x86_64": 0x01000007}
    with universal.open("rb") as executable:
        magic, count = struct.unpack(">II", executable.read(8))
        if magic != 0xCAFEBABE or not 1 <= count <= 16:
            raise RuntimeError("Unexpected universal Biber archive")
        for _ in range(count):
            cpu, _, offset, size, _ = struct.unpack(">IIIII", executable.read(20))
            if cpu == machines[os.uname().machine]:
                if offset + size > universal.stat().st_size:
                    raise RuntimeError("Invalid Biber slice extent")
                executable.seek(offset)
                binary = executable.read(size)
                target = release / "runtime" / "bin" / "biber"
                target.write_bytes(binary)
                target.chmod(0o755)
                return target
    raise RuntimeError("Biber does not include this CPU architecture")


def private_file(path, content, mode=0o600):
    temporary = path.with_suffix(".new")
    temporary.write_text(content)
    temporary.chmod(mode)
    temporary.replace(path)


def restore_data(root, source, settings):
    source = source.resolve()
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("备份源与新实例必须位于互不包含的目录。")
    database = source / "data" / "database" / "workspace.sqlite3"
    if not database.is_file():
        raise ValueError("备份中找不到论文数据库。")
    old = json.loads((source / "instance.json").read_text())
    credentials = {
        key: old[key]
        for key in (
            "CREDENTIAL_INSTALLATION_ID",
            "CREDENTIAL_MASTER_KEY_B64",
            "INTERNAL_SERVICE_TOKEN",
        )
    }
    # The Server holds this lock throughout migrations and all product writes.
    # A backup copy can omit the lock file. Never replace or unlink a live lock.
    with (database.parent / ".academate-server.lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("源实例仍在使用，请先运行 amp stop。") from error
        data = source / "data"
        for entry in data.rglob("*"):
            if entry.is_symlink():
                raise ValueError(f"恢复源中含符号链接：{entry.relative_to(data)}")
        with tempfile.TemporaryDirectory(dir=root) as staging:
            target = Path(staging) / "data"

            def exclude(path, _names):
                if Path(path) == data:
                    return {"run"}
                if Path(path) == database.parent:
                    return {".academate-server.lock"}
                return set()

            shutil.copytree(data, target, ignore=exclude)
            (target / "run").mkdir(mode=0o700)
            # Publish credentials before data. A retry can safely repeat a failed copy;
            # an existing database is never associated with newly generated keys.
            settings.update(credentials)
            private_file(root / "instance.json", json.dumps(settings, indent=2) + "\n")
            target.rename(root / "data")


def set_install_transport(settings, source, web_port, api_port, web_origin, api_origin, cert, key,
                          acme, http_loopback):
    if source and not any((web_port, api_port, web_origin, cert, acme, http_loopback)):
        previous = json.loads((source / "instance.json").read_text())
        settings.update({k: previous[k] for k in TRANSPORT_KEYS if k in previous})
        # A copied data tree may contain operator-supplied PEMs as well as Caddy's PKI.
        for key_name in ("SERVER_HTTPS_CERT_FILE", "SERVER_HTTPS_KEY_FILE"):
            if settings.get(key_name):
                pem = Path(settings[key_name])
                previous_data = Path(previous["STORAGE_ROOT"])
                # Canonicalize the containing directory, preserving any live PEM symlink.
                original = pem.parent.resolve() / pem.name
                if original.is_relative_to(previous_data.resolve()):
                    relative = original.relative_to(previous_data.resolve())
                    settings[key_name] = str(Path(settings["STORAGE_ROOT"]) / relative)
        if settings.get("SERVER_HTTPS_API_ORIGIN") and not settings.get("SERVER_HTTPS_MODE"):
            # Older installations left Caddy in charge of ACME and local issuance.
            # Preserve their existing proxy policy and PKI when restoring a backup.
            if settings.get("SERVER_HTTPS_CERT_FILE"):
                settings["SERVER_HTTPS_MODE"] = "provided"
            else:
                settings["SERVER_HTTPS_MODE"] = "acme"
        return
    if http_loopback:
        configure_http(settings, web_port or 3000, api_port or 8000)
    else:
        web = web_origin or f"https://127.0.0.1:{web_port or 8443}"
        api = api_origin or f"https://127.0.0.1:{api_port or 9443}"
        configure_tls(settings, web, api, cert=cert, key=key, acme=acme)


def instance_settings(root, web_port, api_port, source=None, *, web_origin=None, api_origin=None,
                      cert=None, key=None, acme=False, http_loopback=False):
    transport = (source, web_port, api_port, web_origin, api_origin, cert, key, acme, http_loopback)
    path = root / "instance.json"
    data = root / "data"
    directories = ("database", "papers", "exports", "tmp", "run")
    if path.exists():
        settings = json.loads(path.read_text())
        if source:
            backup = json.loads((source / "instance.json").read_text())
            if (
                settings["CREDENTIAL_INSTALLATION_ID"]
                != backup["CREDENTIAL_INSTALLATION_ID"]
            ):
                raise ValueError("恢复源与未完成安装的实例身份不一致，请选择空目录。")
            if not data.exists():
                restore_data(root, source, settings)
        for name in directories:
            (data / name).mkdir(parents=True, mode=0o700, exist_ok=True)
        # A failed certificate check or download can be retried with corrected arguments.
        if any((web_port, api_port, web_origin, cert, acme, http_loopback)):
            set_install_transport(settings, *transport)
        return settings
    if data.exists():
        raise FileExistsError("实例目录已有 data 而缺少 instance.json，不能覆盖。")
    settings = {
        "APP_ENV": "production",
        "RUNTIME_EDITION": "SERVER",
        "IDENTITY_MODE": "AUTHENTICATED",
        "RUNTIME_NETWORK_SCOPE": "LOOPBACK",
        "DATABASE_PROFILE": "SQLITE_LOCAL",
        "STORAGE_PROFILE": "LOCAL_FILES",
        "TASK_QUEUE_PROFILE": "SQLITE_DB",
        "SECRET_STORE_PROFILE": "ENV_OR_KMS",
        "OS_SECRET_STORE_MODE": "unavailable",
        "DATABASE_URL": f"sqlite:///{data}/database/workspace.sqlite3",
        "STORAGE_ROOT": str(data),
        "SERVER_STATUS_FILE": str(data / "run" / "server-status.json"),
        "APP_HOST": "127.0.0.1",
        "SERVER_API_BIND_HOST": "127.0.0.1",
        "SERVER_WEB_BIND_HOST": "127.0.0.1",
        "SERVER_WEB_ENABLED": "true",
        "INTERNAL_SERVICE_TOKEN": secrets.token_hex(32),
        "CREDENTIAL_INSTALLATION_ID": str(uuid.uuid4()),
        "CREDENTIAL_MASTER_KEY_B64": base64.b64encode(secrets.token_bytes(32)).decode(
            "ascii"
        ),
    }
    set_install_transport(settings, *transport)
    if source:
        restore_data(root, source, settings)
    else:
        private_file(path, json.dumps(settings, indent=2) + "\n")
        for subdir in directories:
            (data / subdir).mkdir(parents=True, mode=0o700, exist_ok=True)
    return settings


def prepare_setup(env, backend):
    url = f"http://127.0.0.1:{env['SERVER_API_PORT']}/api/v2/health/live"
    for _ in range(90):
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    break
        except (OSError, URLError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("API 未就绪，请使用 amp status 检查状态。")
    run(
        [
            sys.executable,
            "-m",
            "app.setup_cli",
            "prepare-container-setup",
            "--web-origin",
            env["WEB_PUBLIC_ORIGIN"],
        ],
        cwd=backend,
        env=env,
    )


def install(root, bin_dir, web_port, api_port, start, source=None, **transport):
    release = Path(sys.executable).resolve().parents[2]
    cache = root / "cache"
    private_env = {
        "HOME": str(root / "home"),
        "PATH": f"{release}/runtime/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "en_US.UTF-8",
    }
    if sys.platform.startswith("linux"):
        # The upstream TeX binaries link to platform libraries by SONAME. The
        # locked fontconfig and its dependencies live in this release, not /usr/lib.
        private_env["LD_LIBRARY_PATH"] = str(release / "runtime" / "lib")
    copy_sources(release)
    backend = release / "backend"
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--only-binary=:all:",
            "--no-deps",
            "--cache-dir",
            cache / "pip",
            "-r",
            backend / "requirements.runtime.lock",
        ],
        cwd=backend,
        env=private_env,
    )
    run([sys.executable, "-m", "pip", "check"], cwd=backend, env=private_env)
    web = install_web(release, cache, private_env)
    install_tex(SOURCE / "backend", release, cache / "texlive", private_env)
    environment = dict(
        private_env,
        **configure_toolchain(release),
        SERVER_WEB_ENTRY=str(web / "server.js"),
    )
    environment["PATH"] = f"{release}/runtime/bin:{release}/texlive/bin:/usr/bin:/bin"
    environment["LIBGS"] = str(
        release
        / "runtime"
        / "lib"
        / ("libgs.dylib" if sys.platform == "darwin" else "libgs.so")
    )
    private_file(release / "environment.json", json.dumps(environment, indent=2))
    settings = instance_settings(root, web_port, api_port, source, **transport)
    if settings.get("SERVER_HTTPS_API_ORIGIN"):
        install_tls_proxy(root, settings)
    private_file(root / "instance.json", json.dumps(settings, indent=2) + "\n")
    env = {**environment, **settings}
    run(
        [sys.executable, "-m", "app.runtime_check", "--profile", "worker"],
        cwd=backend,
        env=env,
    )
    shutil.copy2(Path(__file__).with_name("configure_https.py"), release / "configure_https.py")
    launcher = release / "launcher.py"
    shutil.copy2(Path(__file__).with_name("launcher.py"), launcher)
    # Publish only after both the compiled Web and real TeX/Poppler smoke pass.
    (root / "bin").mkdir(exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    prefix = shlex.quote(str(root / "current"))
    for name in ("amp", "setup-url", "configure-https"):
        script = (
            f"#!/bin/sh\nexec {prefix}/runtime/bin/python -I "
            f'{prefix}/launcher.py {name} "$@"\n'
        )
        private_file(root / "bin" / name, script, mode=0o700)
    (root / "current").symlink_to(release.relative_to(root), target_is_directory=True)
    amp = root / "bin" / "amp"
    if bin_dir.resolve() != amp.parent:
        link = bin_dir.resolve() / "amp"
        private_file(root / ".amp-link", str(link) + "\n")
        link.symlink_to(amp)
    print(f"AcadeMate Paper 已安装。CLI: {bin_dir / 'amp'}", flush=True)
    print(
        f"如需重新生成首次设置链接，可运行 {shlex.quote(str(root / 'bin' / 'setup-url'))}。"
    )
    show_access(settings)
    if start:
        run([amp, "start"], cwd=backend, env=env)
        prepare_setup(env, backend)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("bin_dir", type=Path)
    parser.add_argument("web_port")
    parser.add_argument("api_port")
    parser.add_argument("start", choices=("yes", "no"))
    parser.add_argument("source", nargs="?", default="")
    add_transport_options(parser)
    args = parser.parse_args()
    check_options(parser, args)
    if args.web_origin and (args.web_port or args.api_port):
        parser.error("Set origins or ports; HTTPS origin URLs already contain their ports")
    source = Path(args.source) if args.source else None
    install(
        args.root.resolve(),
        args.bin_dir,
        int(args.web_port) if args.web_port else None,
        int(args.api_port) if args.api_port else None,
        args.start == "yes",
        source, web_origin=args.web_origin, api_origin=args.api_origin,
        cert=args.cert, key=args.key, acme=args.acme, http_loopback=args.http_loopback,
    )
