from dataclasses import dataclass
from enum import StrEnum


class RuntimeEdition(StrEnum):
    LOCAL = "LOCAL"
    SERVER = "SERVER"


class IdentityMode(StrEnum):
    TRUSTED_LOCAL = "TRUSTED_LOCAL"
    AUTHENTICATED = "AUTHENTICATED"


class DatabaseProfile(StrEnum):
    SQLITE_LOCAL = "SQLITE_LOCAL"
    POSTGRES_SERVER = "POSTGRES_SERVER"


class StorageProfile(StrEnum):
    LOCAL_FILES = "LOCAL_FILES"
    OBJECT_STORAGE = "OBJECT_STORAGE"


class TaskQueueProfile(StrEnum):
    SQLITE_DB = "SQLITE_DB"
    POSTGRES_DB = "POSTGRES_DB"


class SecretStoreProfile(StrEnum):
    OS_KEYRING = "OS_KEYRING"
    ENV_OR_KMS = "ENV_OR_KMS"


class RuntimeNetworkScope(StrEnum):
    LOOPBACK = "LOOPBACK"
    PRIVATE_COMPOSE = "PRIVATE_COMPOSE"
    PUBLIC = "PUBLIC"


class RuntimeProfileError(ValueError):
    """Raised when runtime policy and adapter profiles form an unsafe combination."""


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Authoritative, immutable edition and runtime-adapter policy."""

    edition: RuntimeEdition
    identity_mode: IdentityMode
    database: DatabaseProfile
    storage: StorageProfile
    task_queue: TaskQueueProfile
    secret_store: SecretStoreProfile
    network_scope: RuntimeNetworkScope

    def __post_init__(self) -> None:
        violations: list[str] = []

        if self.edition is RuntimeEdition.SERVER:
            if self.identity_mode is IdentityMode.TRUSTED_LOCAL:
                violations.append("SERVER edition cannot use TRUSTED_LOCAL identity")
            if self.secret_store is SecretStoreProfile.OS_KEYRING:
                violations.append("SERVER edition cannot use the local OS_KEYRING secret store")
        elif self.identity_mode is not IdentityMode.TRUSTED_LOCAL:
            violations.append("LOCAL edition requires TRUSTED_LOCAL identity")

        if self.edition is RuntimeEdition.LOCAL:
            if self.network_scope is RuntimeNetworkScope.PUBLIC:
                violations.append("LOCAL edition cannot use PUBLIC network scope")
            if self.database is not DatabaseProfile.SQLITE_LOCAL:
                violations.append("LOCAL edition requires the SQLITE_LOCAL database profile")
            if self.storage is not StorageProfile.LOCAL_FILES:
                violations.append("LOCAL edition requires the LOCAL_FILES storage profile")
            if self.task_queue is not TaskQueueProfile.SQLITE_DB:
                violations.append("LOCAL edition requires the SQLITE_DB task queue profile")
        else:
            # The first container distribution is intentionally a single-node Server profile:
            # SQLite, local files and the SQLite-backed queue share one persistent volume.  The
            # future horizontally-scaled profile changes all three adapters together.  Mixed
            # tuples would provide neither set of consistency guarantees and therefore fail
            # before adapter construction.
            compact_profile = (
                DatabaseProfile.SQLITE_LOCAL,
                StorageProfile.LOCAL_FILES,
                TaskQueueProfile.SQLITE_DB,
            )
            scaled_profile = (
                DatabaseProfile.POSTGRES_SERVER,
                StorageProfile.OBJECT_STORAGE,
                TaskQueueProfile.POSTGRES_DB,
            )
            selected_profile = (self.database, self.storage, self.task_queue)
            if selected_profile not in {compact_profile, scaled_profile}:
                violations.append(
                    "SERVER edition requires either the complete single-node SQLite/local-files "
                    "profile or the complete PostgreSQL/object-storage profile"
                )

        if (
            self.database is DatabaseProfile.SQLITE_LOCAL
            and self.task_queue is not TaskQueueProfile.SQLITE_DB
        ):
            violations.append("SQLITE_LOCAL must use the SQLITE_DB task queue")
        if (
            self.database is DatabaseProfile.POSTGRES_SERVER
            and self.task_queue is not TaskQueueProfile.POSTGRES_DB
        ):
            violations.append("POSTGRES_SERVER must use the POSTGRES_DB task queue")

        if violations:
            raise RuntimeProfileError("Invalid RuntimeProfile: " + "; ".join(violations))
