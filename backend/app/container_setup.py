from __future__ import annotations

import sys

from app.application.container_setup import (
    ContainerSetupExitState,
    ContainerSetupProcessError,
    ContainerSetupVerificationLoop,
)
from app.application.runtime_verification import SetupRuntimeVerificationService
from app.config import RuntimeConfigurationError, get_settings
from app.domain.runtime import RuntimeEdition
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.installation_identity import (
    DatabaseInstallationOwnerIdentityInvariant,
)
from app.infrastructure.owner_setup import DatabaseSetupStateStore
from app.infrastructure.runtime_verification import WorkerRuntimeVerifier
from app.infrastructure.schema_revision import DatabaseSchemaRevisionInvariant
from app.infrastructure.worker_runtime import SignalStopRequest


def _run_container_setup(stop_request: SignalStopRequest) -> ContainerSetupExitState:
    settings = get_settings()
    profile = settings.validate_runtime_configuration()
    if profile.edition is not RuntimeEdition.SERVER:
        raise RuntimeConfigurationError(
            "The container setup verifier requires the Server runtime profile."
        )

    engine = build_engine(settings.database_url)
    try:
        session_factory = build_session_factory(engine)
        state_store = DatabaseSetupStateStore(session_factory)
        verification = SetupRuntimeVerificationService(
            state_store=state_store,
            verifier=WorkerRuntimeVerifier(settings),
            identity_invariant=DatabaseInstallationOwnerIdentityInvariant(
                session_factory,
                expected_edition=RuntimeEdition.SERVER,
            ),
            schema_invariant=DatabaseSchemaRevisionInvariant(session_factory),
        )
        return ContainerSetupVerificationLoop(
            state_store=state_store,
            verification=verification,
            stop_request=stop_request,
            poll_interval_seconds=settings.worker_setup_poll_seconds,
            retry_delay_seconds=settings.worker_runtime_retry_seconds,
        ).run()
    finally:
        engine.dispose()


def main() -> int:
    stop_request = SignalStopRequest()
    try:
        stop_request.install()
        outcome = _run_container_setup(stop_request)
    except KeyboardInterrupt:
        outcome = ContainerSetupExitState.CANCELLED
    except ContainerSetupProcessError as error:
        print(f"container_setup failure={error.failure_code}", file=sys.stderr)
        return 1
    except (RuntimeConfigurationError, ValueError):
        print("container_setup failure=CONTAINER_CONFIGURATION_INVALID", file=sys.stderr)
        return 1
    except Exception:
        print("container_setup failure=CONTAINER_SETUP_UNAVAILABLE", file=sys.stderr)
        return 1

    print(f"container_setup status={outcome.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
