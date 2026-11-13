from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_SAFE_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9.+_-]+$")


class RepositoryPlanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PackageRecord:
    name: str
    revision: str
    container_size: int
    container_checksum: str
    dependencies: tuple[str, ...]

    @property
    def archive_name(self) -> str:
        return f"{self.name}.r{self.revision}.tar.xz"


def parse_package_records(tlpdb: str) -> dict[str, PackageRecord]:
    records: dict[str, PackageRecord] = {}
    for raw_record in tlpdb.split("\n\n"):
        lines = raw_record.splitlines()
        if not lines or not lines[0].startswith("name "):
            continue
        name = lines[0][len("name ") :]
        revision: str | None = None
        container_size: int | None = None
        container_checksum: str | None = None
        dependencies: list[str] = []
        for line in lines[1:]:
            if line.startswith("revision "):
                revision = line[len("revision ") :]
            elif line.startswith("containersize "):
                container_size = int(line[len("containersize ") :])
            elif line.startswith("containerchecksum "):
                container_checksum = line[len("containerchecksum ") :]
            elif line.startswith("depend "):
                dependencies.append(line[len("depend ") :])
        if revision is None or container_size is None or container_checksum is None:
            continue
        if _SAFE_PACKAGE_NAME.fullmatch(name) is None:
            raise RepositoryPlanError(f"Unsafe TeX Live package name in database: {name!r}")
        records[name] = PackageRecord(
            name=name,
            revision=revision,
            container_size=container_size,
            container_checksum=container_checksum,
            dependencies=tuple(dependencies),
        )
    return records


def resolve_scheme(
    records: dict[str, PackageRecord],
    *,
    scheme: str,
    platform: str,
) -> tuple[PackageRecord, ...]:
    selected: dict[str, PackageRecord] = {}
    pending = [scheme]
    while pending:
        requested_name = pending.pop()
        platform_dependency = requested_name.endswith(".ARCH")
        name = (
            f"{requested_name[: -len('.ARCH')]}.{platform}"
            if platform_dependency
            else requested_name
        )
        if "/" in name or name in selected:
            continue
        record = records.get(name)
        if record is None:
            # Not every tool ships a binary for every supported TeX Live platform.
            # install-tl skips those unavailable platform variants as well.
            if platform_dependency:
                continue
            raise RepositoryPlanError(f"Unresolved TeX Live dependency: {name}")
        selected[name] = record
        pending.extend(record.dependencies)
    return tuple(selected[name] for name in sorted(selected))


def write_aria2_input(
    packages: tuple[PackageRecord, ...],
    *,
    repository_url: str,
    destination: Path,
) -> None:
    archive_url = repository_url.rstrip("/") + "/archive"
    lines: list[str] = []
    for package in packages:
        lines.extend(
            [
                f"{archive_url}/{quote(package.archive_name)}",
                f"  out={package.archive_name}",
                f"  checksum=sha-512={package.container_checksum}",
            ]
        )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a TeX Live scheme into a deterministic aria2 download list."
    )
    parser.add_argument("--tlpdb", type=Path, required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--scheme", default="scheme-full")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    records = parse_package_records(arguments.tlpdb.read_text(encoding="utf-8"))
    packages = resolve_scheme(
        records,
        scheme=arguments.scheme,
        platform=arguments.platform,
    )
    if not packages:
        raise RepositoryPlanError("The TeX Live scheme resolved to no downloadable packages.")
    write_aria2_input(
        packages,
        repository_url=arguments.repository_url,
        destination=arguments.output,
    )
    total_bytes = sum(package.container_size for package in packages)
    print(
        f"Resolved {arguments.scheme} for {arguments.platform}: "
        f"{len(packages)} archives, {total_bytes} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
