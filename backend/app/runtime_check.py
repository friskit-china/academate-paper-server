from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.config import get_settings
from app.infrastructure.platform import (
    PlatformAdapterError,
    ToolchainResolutionError,
    build_toolchain_resolver,
    resolve_macos_acceptance_root,
)
from app.infrastructure.runtime_dependencies import probe_worker_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AcadematePaper runtime dependencies.")
    parser.add_argument("--profile", choices=("worker",), default="worker")
    parser.add_argument(
        "--skip-render-smoke",
        action="store_true",
        help="Only resolve required commands; the default also compiles a real table image.",
    )
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    parser.add_argument(
        "--accept-external-toolchain-roots",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--external-tex-bin-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--external-usr-local-root", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    supplied_external_roots = (
        arguments.external_tex_bin_root,
        arguments.external_usr_local_root,
    )
    if any(root is not None for root in supplied_external_roots) and not all(
        root is not None for root in supplied_external_roots
    ):
        parser.error("external macOS acceptance roots must be provided together")
    roots_supplied = all(root is not None for root in supplied_external_roots)
    if arguments.accept_external_toolchain_roots != roots_supplied:
        parser.error(
            "explicit external roots and the acceptance capability must be used together"
        )
    settings = get_settings()
    try:
        acceptance_root = (
            resolve_macos_acceptance_root(
                external_tex_bin_root=arguments.external_tex_bin_root,
                external_usr_local_root=arguments.external_usr_local_root,
            )
            if arguments.accept_external_toolchain_roots
            else None
        )
        toolchain = build_toolchain_resolver(
            mode=settings.toolchain_mode,
            managed_root=settings.toolchain_managed_root,
            manifest_path=settings.toolchain_manifest_path,
            external_tex_bin_root=arguments.external_tex_bin_root,
            external_usr_local_root=arguments.external_usr_local_root,
            external_acceptance_roots=arguments.accept_external_toolchain_roots,
            external_acceptance_root=acceptance_root,
        ).resolve()
        launch_fingerprint = settings.toolchain_fingerprint
        if (
            launch_fingerprint is None
            and arguments.accept_external_toolchain_roots
            and toolchain.acceptance_only
        ):
            # Direct isolated packaging verification has no Supervisor preflight.  It may trust
            # only the fingerprint produced by this same restricted resolver invocation; callers
            # cannot supply an arbitrary expected identity.
            launch_fingerprint = toolchain.fingerprint
        if toolchain.fingerprint != launch_fingerprint:
            raise ToolchainResolutionError(
                "Resolved toolchain fingerprint does not match launch preflight"
            )
        report = probe_worker_runtime(
            toolchain=toolchain,
            render_smoke=not arguments.skip_render_smoke,
        )
    except (PlatformAdapterError, ToolchainResolutionError, ValueError) as error:
        if arguments.json:
            print(json.dumps({"error": str(error), "ok": False}, sort_keys=True))
        else:
            print(str(error), file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(asdict(report), sort_keys=True))
    elif report.ok:
        print(
            "Worker runtime dependencies are ready: "
            f"resolver={report.resolver} "
            f"texlive_profile={report.texlive_profile} "
            f"texlive_release={report.texlive_release} "
            f"texlive_package_smoke={report.texlive_package_smoke} "
            f"texlive_compile_smoke={report.texlive_compile_smoke} "
            f"biber={report.commands.get('biber')} "
            f"bibtex={report.commands.get('bibtex')} "
            f"latex={report.commands.get('latex')} "
            f"pdflatex={report.commands.get('pdflatex')} "
            f"xelatex={report.commands.get('xelatex')} "
            f"dvisvgm={report.commands.get('dvisvgm')} "
            f"pdftocairo={report.commands.get('pdftocairo')} "
            f"pdftotext={report.commands.get('pdftotext')} "
            f"pdfinfo={report.commands.get('pdfinfo')} "
            f"table_render_smoke={report.table_render_smoke} "
            f"figure_render_smoke={report.figure_render_smoke} "
            f"capabilities={json.dumps(report.capabilities, sort_keys=True)}"
        )
    else:
        print(report.error, file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
