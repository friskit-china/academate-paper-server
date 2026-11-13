"""Install the Server's checksum-locked TeX Live tree under one private prefix."""

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def download(url, destination, checksum):
    if destination.is_file():
        with destination.open("rb") as existing:
            if hashlib.file_digest(existing, "sha512").hexdigest() == checksum:
                return
    temporary = destination.with_suffix(".part")
    subprocess.run(
        ["curl", "-fsSL", "--retry", "4", "--max-time", "600", "-o", temporary, url],
        check=True,
    )
    with temporary.open("rb") as archive:
        if hashlib.file_digest(archive, "sha512").hexdigest() != checksum:
            raise ValueError(f"TeX Live checksum mismatch for {url}")
    temporary.replace(destination)


def ensure_formats(binaries, env):
    env = {**env, "PATH": f"{binaries}:{env['PATH']}"}
    for name in ("latex", "pdflatex", "xelatex"):
        check = subprocess.run(
            [
                binaries / "kpsewhich",
                f"-engine={'xetex' if name == 'xelatex' else 'pdftex'}",
                f"{name}.fmt",
            ],
            capture_output=True,
            env=env,
            check=False,
        )
        if check.returncode:
            subprocess.run(
                [binaries / "fmtutil-sys", "--byfmt", name], env=env, check=True
            )


def install(backend, release, cache, env=None):
    env = os.environ.copy() if env is None else env
    lock = dict(
        line.split("=", 1)
        for line in (backend / "texlive-runtime.lock").read_text().splitlines()
        if line and not line.startswith("#")
    )
    prefix = release / "texlive"
    bin_link = prefix / "bin"
    if bin_link.is_symlink() and (bin_link / "kpsewhich").is_file():
        ensure_formats(bin_link, env)
        return
    cache.mkdir(parents=True, exist_ok=True)
    installer_tar = cache / "install-tl-unx.tar.gz"
    download(
        lock["TEXLIVE_INSTALLER_URL"], installer_tar, lock["TEXLIVE_INSTALLER_SHA512"]
    )
    with tarfile.open(installer_tar) as archive:
        archive.extractall(cache, filter="data")
    (installer,) = cache.glob("install-tl-*/install-tl")
    repository = cache / "tlnet"
    (repository / "archive").mkdir(parents=True, exist_ok=True)
    (repository / "tlpkg").mkdir(exist_ok=True)
    url = lock["TEXLIVE_REPOSITORY_URL"]
    database = repository / "tlpkg" / "texlive.tlpdb"
    digest = lock["TEXLIVE_TLPDB_SHA512"]
    download(f"{url}/tlpkg/texlive.tlpdb", database, digest)
    database.with_suffix(".tlpdb.sha512").write_text(digest + "  texlive.tlpdb\n")
    platform = subprocess.check_output(
        ["perl", installer, "--print-platform"], text=True
    ).strip()
    archive_list = cache / f"archives-{platform}.txt"
    subprocess.run(
        [
            sys.executable,
            backend / "scripts" / "prepare_texlive_repository.py",
            "--tlpdb",
            database,
            "--repository-url",
            url,
            "--platform",
            platform,
            "--output",
            archive_list,
        ],
        check=True,
    )
    entries = archive_list.read_text().splitlines()
    plans = [entries[index : index + 3] for index in range(0, len(entries), 3)]

    def missing(plan):
        filename = plan[1].strip().removeprefix("out=")
        expected = plan[2].strip().removeprefix("checksum=sha-512=")
        artifact = repository / "archive" / filename
        if not artifact.is_file():
            return plan
        with artifact.open("rb") as file:
            return (
                plan
                if hashlib.file_digest(file, "sha512").hexdigest() != expected
                else []
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        needed = [line for plan in pool.map(missing, plans) for line in plan]
    pending = cache / f"pending-{platform}.txt"
    pending.write_text("\n".join(needed) + "\n")
    if needed:
        _fetch_archives(repository, pending)
    profile = cache / "private.profile"
    profile.write_text(
        (backend / "texlive.profile").read_text().replace("/opt/texlive", str(prefix))
    )
    subprocess.run(
        [
            "perl",
            installer,
            "--profile",
            profile,
            "--repository",
            repository,
            "--no-interaction",
            "--no-persistent-downloads",
            "--no-continue",
            "--no-doc-install",
            "--no-src-install",
        ],
        env=env,
        check=True,
    )
    binaries = prefix / lock["TEXLIVE_RELEASE"] / "bin" / platform
    if not (binaries / "kpsewhich").is_file():
        raise RuntimeError("The TeX Live installer did not create the target toolchain")
    bin_link.symlink_to(binaries.relative_to(prefix), target_is_directory=True)
    ensure_formats(bin_link, env)


def _fetch_archives(repository, pending):
    subprocess.run(
        [
            "aria2c",
            f"--input-file={pending}",
            f"--dir={repository / 'archive'}",
            "--user-agent=curl/7.88.1",
            "--http-accept-gzip=false",
            "--continue=true",
            "--max-concurrent-downloads=12",
            "--split=1",
            "--max-tries=5",
            "--retry-wait=2",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--console-log-level=warn",
            "--summary-interval=30",
            "--show-console-readout=false",
            "--download-result=hide",
        ],
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", type=Path)
    parser.add_argument("release", type=Path)
    parser.add_argument("cache", type=Path)
    args = parser.parse_args()
    install(args.backend.resolve(), args.release.resolve(), args.cache.resolve())
