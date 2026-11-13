from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from app.domain.os_adapters import AppDirectories, ToolchainResolver
from app.infrastructure.platform.directories import (
    ComposeAppDirectories,
    MacOSAppDirectories,
    WindowsAppDirectories,
)
from app.infrastructure.platform.macos_toolchain import (
    DEFAULT_MACTEX_TEX_BIN_ROOT,
    DEFAULT_MACTEX_USR_LOCAL_ROOT,
    MacOSExternalToolchainResolver,
)
from app.infrastructure.platform.storage_paths import (
    PosixStorageKeyMapper,
    StorageKeyMapper,
    WindowsStorageKeyMapper,
)
from app.infrastructure.platform.toolchain import (
    ComposePathToolchainResolver,
    ManagedManifestToolchainResolver,
)


class PlatformAdapterError(RuntimeError):
    pass


def build_app_directories(
    *,
    mode: Literal["native", "compose"],
    data_root: Path | None = None,
    platform_name: str = sys.platform,
    environment: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> AppDirectories:
    if mode == "compose":
        if data_root is None:
            raise PlatformAdapterError("Compose AppDirectories requires data_root")
        return ComposeAppDirectories(data_root=data_root)
    if platform_name == "darwin":
        return MacOSAppDirectories(home=home or Path.home())
    if platform_name == "win32":
        local = environment.get("LOCALAPPDATA", "").strip()
        roaming = environment.get("APPDATA", "").strip()
        if not local or not roaming:
            raise PlatformAdapterError("Windows AppDirectories requires LOCALAPPDATA and APPDATA")
        return WindowsAppDirectories(
            local_app_data=Path(local),
            roaming_app_data=Path(roaming),
        )
    raise PlatformAdapterError(f"Native Local Edition is unsupported on {platform_name}")


def build_storage_key_mapper(
    root: Path,
    *,
    platform_name: str = sys.platform,
) -> StorageKeyMapper:
    if platform_name == "win32":
        return WindowsStorageKeyMapper(root)
    return PosixStorageKeyMapper(root)


def build_toolchain_resolver(
    *,
    mode: Literal["managed-manifest", "compose-path", "macos-external"],
    managed_root: Path | None = None,
    manifest_path: Path | None = None,
    external_tex_bin_root: Path | None = None,
    external_usr_local_root: Path | None = None,
    external_acceptance_roots: bool = False,
    external_acceptance_root: Path | None = None,
    platform_name: str = sys.platform,
    process_environment: Mapping[str, str] = os.environ,
) -> ToolchainResolver:
    if mode == "compose-path":
        return ComposePathToolchainResolver(
            profile_path=manifest_path or Path("/opt/texlive/runtime-profile")
        )
    if managed_root is None or manifest_path is None:
        raise PlatformAdapterError(
            "Managed toolchain mode requires an explicit root and manifest path"
        )
    if mode == "macos-external":
        if platform_name != "darwin":
            raise PlatformAdapterError(
                "macos-external toolchain mode requires a native macOS adapter"
            )
        tex_bin_root = external_tex_bin_root or DEFAULT_MACTEX_TEX_BIN_ROOT
        usr_local_root = external_usr_local_root or DEFAULT_MACTEX_USR_LOCAL_ROOT
        roots_overridden = (
            tex_bin_root.absolute() != DEFAULT_MACTEX_TEX_BIN_ROOT
            or usr_local_root.absolute() != DEFAULT_MACTEX_USR_LOCAL_ROOT
        )
        if roots_overridden and (
            external_tex_bin_root is None or external_usr_local_root is None
        ):
            raise PlatformAdapterError(
                "macOS acceptance toolchain roots must be overridden together"
            )
        if roots_overridden and not external_acceptance_roots:
            raise PlatformAdapterError(
                "custom macOS toolchain roots require an explicit acceptance capability"
            )
        if external_acceptance_roots and not roots_overridden:
            raise PlatformAdapterError(
                "the acceptance capability cannot relax production MacTeX roots"
            )
        if external_acceptance_roots and external_acceptance_root is None:
            raise PlatformAdapterError(
                "custom macOS toolchain roots require one validated acceptance tree"
            )
        if not external_acceptance_roots and external_acceptance_root is not None:
            raise PlatformAdapterError(
                "production MacTeX resolution cannot name an acceptance tree"
            )
        return MacOSExternalToolchainResolver(
            managed_root=managed_root,
            manifest_path=manifest_path,
            tex_bin_root=tex_bin_root,
            usr_local_root=usr_local_root,
            allow_acceptance_roots=external_acceptance_roots,
            acceptance_root_parent=external_acceptance_root,
            process_environment=process_environment,
        )
    return ManagedManifestToolchainResolver(
        managed_root=managed_root,
        manifest_path=manifest_path,
        require_executable_bit=platform_name != "win32",
    )
