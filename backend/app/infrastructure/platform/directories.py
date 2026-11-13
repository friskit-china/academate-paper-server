from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.os_adapters import AppDirectoryPaths
from app.infrastructure.platform.filesystem import classify_filesystem_error


@dataclass(frozen=True, slots=True)
class _BaseAppDirectories:
    _paths: AppDirectoryPaths

    def paths(self) -> AppDirectoryPaths:
        return self._paths

    def initialize(self) -> AppDirectoryPaths:
        for path in (
            self._paths.data,
            self._paths.config,
            self._paths.logs,
            self._paths.cache,
            self._paths.temporary,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise classify_filesystem_error(
                    error,
                    operation="initialize application directory",
                    path=path,
                ) from error
        return self._paths


class MacOSAppDirectories(_BaseAppDirectories):
    def __init__(self, *, home: Path, application_name: str = "AcadematePaper") -> None:
        support = home / "Library" / "Application Support" / application_name
        super().__init__(
            AppDirectoryPaths(
                data=support / "Data",
                config=support / "Configuration",
                logs=home / "Library" / "Logs" / application_name,
                cache=home / "Library" / "Caches" / application_name,
                temporary=home / "Library" / "Caches" / application_name / "Temporary",
            )
        )


class WindowsAppDirectories(_BaseAppDirectories):
    def __init__(
        self,
        *,
        local_app_data: Path,
        roaming_app_data: Path,
        application_name: str = "AcadematePaper",
    ) -> None:
        local = local_app_data / application_name
        roaming = roaming_app_data / application_name
        super().__init__(
            AppDirectoryPaths(
                data=local / "Data",
                config=roaming / "Configuration",
                logs=local / "Logs",
                cache=local / "Cache",
                temporary=local / "Temporary",
            )
        )


class ComposeAppDirectories(_BaseAppDirectories):
    def __init__(self, *, data_root: Path) -> None:
        super().__init__(
            AppDirectoryPaths(
                data=data_root,
                config=data_root / "configuration",
                logs=data_root / "logs",
                cache=data_root / "cache",
                temporary=data_root / "tmp",
            )
        )
