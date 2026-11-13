from __future__ import annotations

from dataclasses import dataclass

from app.application.runtime_verification import RuntimeVerificationOutcome
from app.config import Settings
from app.domain.os_adapters import ResolvedToolchain
from app.infrastructure.platform import (
    PlatformAdapterError,
    ToolchainResolutionError,
    build_toolchain_resolver,
    resolve_macos_acceptance_root,
)
from app.infrastructure.runtime_dependencies import probe_worker_runtime


def toolchain_fingerprint_is_accepted(
    settings: Settings,
    toolchain: ResolvedToolchain,
) -> bool:
    """Apply the same preflight identity rule to setup verification and the worker.

    Compose images are functionally verified from their locked image contents and have no separate
    launcher preflight fingerprint. Managed/native artifacts still require an exact fingerprint.
    """

    expected_fingerprint = settings.toolchain_fingerprint
    return (
        expected_fingerprint is None
        and settings.toolchain_mode == "compose-path"
        and toolchain.resolver == "compose-path"
    ) or (expected_fingerprint is not None and toolchain.fingerprint == expected_fingerprint)


@dataclass(frozen=True, slots=True)
class WorkerRuntimeVerifier:
    """Runs the same functional worker probe used by the packaged-runtime gate."""

    settings: Settings
    external_acceptance_roots: bool = False

    def verify(self) -> RuntimeVerificationOutcome:
        try:
            acceptance_root = (
                resolve_macos_acceptance_root(
                    external_tex_bin_root=self.settings.toolchain_external_tex_bin_root,
                    external_usr_local_root=(self.settings.toolchain_external_usr_local_root),
                )
                if self.external_acceptance_roots
                else None
            )
            toolchain = build_toolchain_resolver(
                mode=self.settings.toolchain_mode,
                managed_root=self.settings.toolchain_managed_root,
                manifest_path=self.settings.toolchain_manifest_path,
                external_tex_bin_root=self.settings.toolchain_external_tex_bin_root,
                external_usr_local_root=self.settings.toolchain_external_usr_local_root,
                external_acceptance_roots=self.external_acceptance_roots,
                external_acceptance_root=acceptance_root,
            ).resolve()
            if not toolchain_fingerprint_is_accepted(self.settings, toolchain):
                return RuntimeVerificationOutcome(
                    ok=False,
                    failure_code="TOOLCHAIN_FINGERPRINT_CHANGED",
                )
            report = probe_worker_runtime(toolchain=toolchain, render_smoke=True)
        except (PlatformAdapterError, ToolchainResolutionError, ValueError):
            return RuntimeVerificationOutcome(
                ok=False,
                failure_code="TOOLCHAIN_RESOLUTION_FAILED",
            )
        capability_states = tuple(sorted(report.capabilities.items()))
        return RuntimeVerificationOutcome(
            ok=report.ok,
            capability_states=capability_states,
            failure_code=None if report.ok else "WORKER_RUNTIME_SMOKE_FAILED",
        )
