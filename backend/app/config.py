from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.credentials import SecretKeyMaterial
from app.domain.os_adapters import (
    ExternalToolchainComponents,
    ExternalToolchainComponentState,
    ExternalToolchainKind,
    ExternalToolchainSetupStatus,
    ExternalToolchainState,
)
from app.domain.runtime import (
    DatabaseProfile,
    IdentityMode,
    RuntimeEdition,
    RuntimeNetworkScope,
    RuntimeProfile,
    SecretStoreProfile,
    StorageProfile,
    TaskQueueProfile,
)


class RuntimeConfigurationError(ValueError):
    """Raised before adapter construction when Settings contradict RuntimeProfile."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    runtime_edition: RuntimeEdition = RuntimeEdition.LOCAL
    identity_mode: IdentityMode = IdentityMode.TRUSTED_LOCAL
    database_profile: DatabaseProfile = DatabaseProfile.SQLITE_LOCAL
    storage_profile: StorageProfile = StorageProfile.LOCAL_FILES
    task_queue_profile: TaskQueueProfile = TaskQueueProfile.SQLITE_DB
    secret_store_profile: SecretStoreProfile = SecretStoreProfile.OS_KEYRING
    os_secret_store_mode: Literal["unavailable", "native"] = "unavailable"
    local_installation_id: UUID | None = None
    credential_installation_id: UUID | None = None
    credential_master_key_b64: SecretStr | None = None
    runtime_network_scope: RuntimeNetworkScope = RuntimeNetworkScope.LOOPBACK
    database_url: str = "sqlite:///../data/database/workspace.sqlite3"
    storage_backend: str = "local"
    storage_root: Path = Path("../data")
    internal_service_token: SecretStr | None = None
    single_user_auto_login_enabled: bool = False
    web_bind_host: str | None = None
    web_public_origin: str | None = None
    web_session_cookie_name: str = "academate_session"
    web_session_ttl_seconds: int = Field(default=43_200, ge=300, le=604_800)
    web_session_cookie_secure: bool = False
    # Opt in to explicitly configured HTTP origins without relaxing origin or authentication checks.
    web_allow_insecure_http: bool = False
    toolchain_mode: Literal[
        "managed-manifest",
        "compose-path",
        "macos-external",
    ] = "managed-manifest"
    toolchain_managed_root: Path | None = None
    toolchain_manifest_path: Path | None = None
    toolchain_external_tex_bin_root: Path | None = None
    toolchain_external_usr_local_root: Path | None = None
    toolchain_fingerprint: str | None = None
    external_toolchain_kind: ExternalToolchainKind | None = None
    external_toolchain_state: ExternalToolchainState | None = None
    external_toolchain_official_url: str | None = None
    external_toolchain_restart_required: bool = False
    external_toolchain_component_mactex: ExternalToolchainComponentState | None = None
    external_toolchain_component_ghostscript: ExternalToolchainComponentState | None = None
    external_toolchain_component_ghostscript_extras: ExternalToolchainComponentState | None = None
    web_client_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    llm_provider: str = "glm"
    llm_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_model: str = "glm-5.2"
    repair_render_reasoning_effort: Literal["off", "low", "medium", "high"] = "off"
    translation_target_language: str = "zh-CN"
    translation_timeout_seconds: int = Field(default=60, ge=5, le=300)
    translation_max_attempts: int = Field(default=4, ge=1, le=10)
    translation_concurrency: int = Field(default=2, ge=1, le=8)
    qa_timeout_seconds: int = Field(default=90, ge=5, le=300)
    qa_max_attempts: int = Field(default=4, ge=1, le=10)
    qa_context_token_budget: int = Field(default=12_000, ge=1_000, le=100_000)
    qa_max_output_tokens: int = Field(default=2_000, ge=128, le=16_000)
    paper_map_timeout_seconds: int = Field(default=120, ge=5, le=300)
    paper_map_max_attempts: int = Field(default=4, ge=1, le=10)
    paper_map_context_token_budget: int = Field(default=30_000, ge=2_000, le=150_000)
    paper_map_max_output_tokens: int = Field(default=6_000, ge=512, le=24_000)
    # Retained only for the legacy Compose environment-provider path.  Keep assignment-compatible
    # with existing adapters while ensuring Settings repr/model repr cannot disclose the value.
    zhipuai_api_key: str = Field(default="", repr=False, exclude=True)
    arxiv_api_base: str = "https://export.arxiv.org/api/query"
    arxiv_source_base: str = "https://export.arxiv.org/e-print"
    arxiv_abstract_base: str = "https://arxiv.org/abs"
    arxiv_user_agent: str = "AcadematePaper/0.1 (private research workspace)"
    arxiv_timeout_seconds: int = 45
    arxiv_metadata_max_bytes: int = 2_000_000
    arxiv_source_max_bytes: int = 50_000_000
    table_render_timeout_seconds: int = 20
    table_render_max_output_bytes: int = 5_000_000
    worker_poll_seconds: float = 0.5
    worker_setup_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_runtime_retry_seconds: float = Field(default=30.0, gt=0, le=300)
    worker_heartbeat_seconds: float = Field(default=5.0, gt=0, le=60)
    # Keep the default bounded drain inside Compose's default ten-second stop grace period.
    worker_drain_timeout_seconds: float = Field(default=8.0, ge=0, le=300)

    @field_validator("cors_origins", "web_client_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("internal_service_token", mode="before")
    @classmethod
    def normalize_internal_service_token(cls, value: object) -> object:
        if value is None or value == "":
            return None
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw_value, str) and len(raw_value.encode("utf-8")) < 32:
            raise ValueError("INTERNAL_SERVICE_TOKEN must contain at least 32 UTF-8 bytes.")
        return value

    @field_validator("web_session_cookie_name")
    @classmethod
    def validate_web_session_cookie_name(cls, value: str) -> str:
        # The Next BFF deliberately recognizes one fixed application cookie. Keeping the backend
        # setting explicit makes the middleware boundary auditable without accepting an unsafe
        # deployment where the two processes silently disagree.
        if value != "academate_session":
            raise ValueError("WEB_SESSION_COOKIE_NAME must be 'academate_session'.")
        return value

    @field_validator(
        "local_installation_id",
        "credential_installation_id",
        "credential_master_key_b64",
        mode="before",
    )
    @classmethod
    def normalize_optional_credential_configuration(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("credential_installation_id")
    @classmethod
    def validate_credential_installation_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("CREDENTIAL_INSTALLATION_ID must not be the nil UUID.")
        return value

    @field_validator(
        "toolchain_managed_root",
        "toolchain_manifest_path",
        "toolchain_external_tex_bin_root",
        "toolchain_external_usr_local_root",
        "external_toolchain_kind",
        "external_toolchain_state",
        "external_toolchain_official_url",
        "external_toolchain_component_mactex",
        "external_toolchain_component_ghostscript",
        "external_toolchain_component_ghostscript_extras",
        "toolchain_fingerprint",
        mode="before",
    )
    @classmethod
    def normalize_optional_runtime_value(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_external_toolchain_status(self) -> Settings:
        if self.toolchain_fingerprint is not None and (
            len(self.toolchain_fingerprint) != 71
            or not self.toolchain_fingerprint.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.toolchain_fingerprint.removeprefix("sha256:")
            )
        ):
            raise ValueError("toolchain fingerprint must be a lowercase SHA-256 identity")
        status_values = (
            self.external_toolchain_kind,
            self.external_toolchain_state,
            self.external_toolchain_official_url,
            self.external_toolchain_component_mactex,
            self.external_toolchain_component_ghostscript,
            self.external_toolchain_component_ghostscript_extras,
        )
        if all(value is None for value in status_values):
            if self.external_toolchain_restart_required:
                raise ValueError(
                    "external toolchain restart state requires a public status snapshot"
                )
            return self
        if any(value is None for value in status_values):
            raise ValueError("external toolchain status snapshot is incomplete")
        assert self.external_toolchain_kind is not None
        assert self.external_toolchain_state is not None
        assert self.external_toolchain_official_url is not None
        assert self.external_toolchain_component_mactex is not None
        assert self.external_toolchain_component_ghostscript is not None
        assert self.external_toolchain_component_ghostscript_extras is not None
        ExternalToolchainSetupStatus(
            kind=self.external_toolchain_kind,
            state=self.external_toolchain_state,
            official_url=self.external_toolchain_official_url,
            restart_required=self.external_toolchain_restart_required,
            components=ExternalToolchainComponents(
                mactex=self.external_toolchain_component_mactex,
                ghostscript=self.external_toolchain_component_ghostscript,
                ghostscript_extras=self.external_toolchain_component_ghostscript_extras,
            ),
        )
        return self

    @property
    def runtime_profile(self) -> RuntimeProfile:
        return RuntimeProfile(
            edition=self.runtime_edition,
            identity_mode=self.identity_mode,
            database=self.database_profile,
            storage=self.storage_profile,
            task_queue=self.task_queue_profile,
            secret_store=self.secret_store_profile,
            network_scope=self.runtime_network_scope,
        )

    @property
    def external_toolchain_setup_status(self) -> ExternalToolchainSetupStatus | None:
        if self.external_toolchain_kind is None:
            return None
        assert self.external_toolchain_state is not None
        assert self.external_toolchain_official_url is not None
        assert self.external_toolchain_component_mactex is not None
        assert self.external_toolchain_component_ghostscript is not None
        assert self.external_toolchain_component_ghostscript_extras is not None
        return ExternalToolchainSetupStatus(
            kind=self.external_toolchain_kind,
            state=self.external_toolchain_state,
            official_url=self.external_toolchain_official_url,
            restart_required=self.external_toolchain_restart_required,
            components=ExternalToolchainComponents(
                mactex=self.external_toolchain_component_mactex,
                ghostscript=self.external_toolchain_component_ghostscript,
                ghostscript_extras=self.external_toolchain_component_ghostscript_extras,
            ),
        )

    def validate_runtime_configuration(self) -> RuntimeProfile:
        """Validate deployment inputs before constructing any concrete adapter."""

        profile = self.runtime_profile

        if (
            self.app_env.strip().lower() in {"prod", "production"}
            and profile.edition is RuntimeEdition.LOCAL
            and not _is_loopback_host(self.app_host)
        ):
            raise RuntimeConfigurationError(
                "Production Local Edition must bind to a loopback address; "
                "TRUSTED_LOCAL identity cannot be published by a container gateway"
            )

        if profile.network_scope is RuntimeNetworkScope.LOOPBACK and not _is_loopback_host(
            self.app_host
        ):
            raise RuntimeConfigurationError(
                "LOOPBACK runtime profile requires APP_HOST to be localhost or a loopback address"
            )

        if (
            profile.identity_mode is IdentityMode.AUTHENTICATED
            and profile.network_scope is RuntimeNetworkScope.PUBLIC
            and not self.web_session_cookie_secure
            and not self.web_allow_insecure_http
        ):
            raise RuntimeConfigurationError(
                "PUBLIC authenticated runtime requires WEB_SESSION_COOKIE_SECURE=true "
                "unless WEB_ALLOW_INSECURE_HTTP is explicitly enabled"
            )

        if self.single_user_auto_login_enabled:
            if (
                profile.edition is not RuntimeEdition.SERVER
                or profile.identity_mode is not IdentityMode.AUTHENTICATED
                or profile.network_scope is not RuntimeNetworkScope.PRIVATE_COMPOSE
            ):
                raise RuntimeConfigurationError(
                    "Single-user automatic owner sessions require the "
                    "SERVER + AUTHENTICATED + PRIVATE_COMPOSE runtime profile"
                )
            if self.web_bind_host is None or not _is_numeric_loopback_host(self.web_bind_host):
                raise RuntimeConfigurationError(
                    "Single-user automatic owner sessions require WEB_BIND_HOST to be a "
                    "numeric loopback address"
                )
            if self.web_public_origin is None or not _is_loopback_web_origin(
                self.web_public_origin
            ):
                raise RuntimeConfigurationError(
                    "Single-user automatic owner sessions require WEB_PUBLIC_ORIGIN to be an "
                    "exact loopback HTTP(S) origin"
                )

        database_backend = _database_backend(self.database_url)
        expected_database_backend = {
            DatabaseProfile.SQLITE_LOCAL: "sqlite",
            DatabaseProfile.POSTGRES_SERVER: "postgresql",
        }[profile.database]
        if database_backend != expected_database_backend:
            raise RuntimeConfigurationError(
                f"DATABASE_URL selects {database_backend!r}, but RuntimeProfile requires "
                f"{expected_database_backend!r}"
            )

        storage_backend = self.storage_backend.strip().lower().replace("-", "_")
        expected_storage_backends = {
            StorageProfile.LOCAL_FILES: {"local", "local_files"},
            StorageProfile.OBJECT_STORAGE: {"object", "object_storage", "s3"},
        }[profile.storage]
        if storage_backend not in expected_storage_backends:
            raise RuntimeConfigurationError(
                f"STORAGE_BACKEND {self.storage_backend!r} does not match {profile.storage.value}"
            )

        # This build intentionally contains only the adapters used by the current Compose/local
        # path.  A structurally valid future SERVER profile must not silently instantiate them.
        if profile.database is not DatabaseProfile.SQLITE_LOCAL:
            raise RuntimeConfigurationError(
                f"Database profile {profile.database.value} is not implemented by this build"
            )
        if profile.storage is not StorageProfile.LOCAL_FILES:
            raise RuntimeConfigurationError(
                f"Storage profile {profile.storage.value} is not implemented by this build"
            )
        if profile.task_queue is not TaskQueueProfile.SQLITE_DB:
            raise RuntimeConfigurationError(
                f"Task queue profile {profile.task_queue.value} is not implemented by this build"
            )

        if self.os_secret_store_mode == "native":
            if profile.secret_store is not SecretStoreProfile.OS_KEYRING:
                raise RuntimeConfigurationError(
                    "Native OS secret storage requires SECRET_STORE_PROFILE=OS_KEYRING"
                )
            if profile.network_scope is not RuntimeNetworkScope.LOOPBACK:
                raise RuntimeConfigurationError(
                    "Native OS secret storage requires the LOOPBACK runtime profile"
                )
            if self.local_installation_id is None:
                raise RuntimeConfigurationError(
                    "Native OS secret storage requires a stable LOCAL_INSTALLATION_ID UUID"
                )

        container_credential_values = (
            self.credential_installation_id is not None,
            self.credential_master_key_b64 is not None,
        )
        if any(container_credential_values) and not all(container_credential_values):
            raise RuntimeConfigurationError(
                "ENV_OR_KMS credential storage requires both "
                "CREDENTIAL_INSTALLATION_ID and CREDENTIAL_MASTER_KEY_B64"
            )
        if any(container_credential_values):
            if profile.edition is not RuntimeEdition.SERVER:
                raise RuntimeConfigurationError(
                    "Container credential settings require Server Edition"
                )
            if profile.secret_store is not SecretStoreProfile.ENV_OR_KMS:
                raise RuntimeConfigurationError(
                    "Container credential settings require SECRET_STORE_PROFILE=ENV_OR_KMS"
                )
            # Decode during startup validation so malformed deployment secrets fail before any
            # database or storage adapter is constructed.  The exception never includes input.
            _ = self.credential_master_key_material

        return profile

    @property
    def credential_master_key_material(self) -> SecretKeyMaterial | None:
        """Decode the deployment root key without exposing its encoded Settings value."""

        if self.credential_master_key_b64 is None:
            return None
        encoded = self.credential_master_key_b64.get_secret_value()
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise RuntimeConfigurationError(
                "CREDENTIAL_MASTER_KEY_B64 must be strict standard Base64 encoding"
            ) from error
        if len(decoded) != 32:
            raise RuntimeConfigurationError(
                "CREDENTIAL_MASTER_KEY_B64 must decode to exactly 256 bits"
            )
        return SecretKeyMaterial(decoded)

    @property
    def selected_llm_api_key(self) -> str:
        return self.zhipuai_api_key if self.llm_provider.lower() == "glm" else ""

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.llm_provider.lower() == "glm"
            and self.llm_api_base
            and self.llm_model
            and self.selected_llm_api_key
        )

    @property
    def translation_configured(self) -> bool:
        return self.llm_configured

    @property
    def qa_configured(self) -> bool:
        return self.llm_configured

    @property
    def paper_map_configured(self) -> bool:
        return self.llm_configured


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _database_backend(database_url: str) -> str:
    scheme = database_url.partition(":")[0].strip().lower()
    if scheme == "sqlite":
        return "sqlite"
    if scheme in {"postgres", "postgresql"} or scheme.startswith("postgresql+"):
        return "postgresql"
    return scheme or "unknown"


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_numeric_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_web_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` performs urllib's range and syntax validation.
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and _is_loopback_host(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )
