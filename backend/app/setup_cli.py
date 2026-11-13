from __future__ import annotations

import argparse
import os
import sys
from ipaddress import ip_address
from urllib.parse import quote, urlsplit

from sqlalchemy import func, select

from app.application.bootstrap import TrustedBootstrapTokenService
from app.application.local_sessions import TrustedLocalLaunchTokenService
from app.application.runtime_verification import (
    RuntimeVerificationFailedError,
    SetupRuntimeVerificationService,
)
from app.config import get_settings
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.db.models import InstallationStateModel, UserModel
from app.infrastructure.installation_identity import (
    DatabaseLocalInstallationIdentityInvariant,
)
from app.infrastructure.local_sessions import (
    DatabaseLocalSessionRepository,
    SecureLocalSessionTokenCodec,
)
from app.infrastructure.owner_setup import (
    DatabaseBootstrapTokenRegistry,
    DatabaseSetupStateStore,
)
from app.infrastructure.runtime_verification import WorkerRuntimeVerifier
from app.infrastructure.schema_revision import DatabaseSchemaRevisionInvariant
from app.infrastructure.security import SecureBootstrapTokenIssuer


def _validated_web_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError("web origin contains an invalid port") from error
    try:
        loopback = host.lower() == "localhost" or ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not loopback)
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "web origin must be HTTPS, or HTTP loopback, without credentials or a path"
        )
    authority = parsed.hostname or ""
    if ":" in authority:
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _origin_is_loopback(value: str) -> bool:
    host = urlsplit(value).hostname or ""
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _container_setup_needs_owner_proof(session_factory) -> bool:
    """Return true only while an unclaimed installation can consume a new proof."""

    with session_factory() as session:
        installation = session.get(InstallationStateModel, "installation")
        if installation is None:
            raise RuntimeError("The installation-state singleton is missing after migration.")
        active_users = int(
            session.scalar(
                select(func.count()).select_from(UserModel).where(UserModel.status == "ACTIVE")
            )
            or 0
        )
        return installation.state in {"NEW", "MIGRATING", "AWAITING_OWNER"} and (
            active_users == 0
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trusted installation bootstrap and runtime-verification operations."
    )
    parser.add_argument(
        "operation",
        choices=(
            "issue-owner-token",
            "prepare-container-setup",
            "issue-launch-token",
            "verify-runtime",
        ),
        help=(
            "Issue an owner proof, issue a one-use browser launch URL, or complete setup with "
            "the full worker runtime smoke."
        ),
    )
    parser.add_argument(
        "--web-origin",
        default=os.environ.get("WEB_PUBLIC_ORIGIN", "http://localhost:3000"),
        type=_validated_web_origin,
        help="Public Web origin for the one-use setup URL (defaults to WEB_PUBLIC_ORIGIN).",
    )
    parser.add_argument(
        "--accept-external-toolchain-roots",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reverify-ready",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    arguments = parser.parse_args()

    if (
        arguments.accept_external_toolchain_roots
        and arguments.operation != "verify-runtime"
    ):
        parser.error("the external toolchain acceptance capability is verification-only")
    if arguments.reverify_ready and arguments.operation != "verify-runtime":
        parser.error("READY runtime revalidation is verification-only")
    if (
        arguments.operation == "issue-launch-token"
        and not _origin_is_loopback(arguments.web_origin)
    ):
        parser.error("Local browser launch URLs require a loopback web origin")

    settings = get_settings()
    settings.validate_runtime_configuration()
    engine = build_engine(settings.database_url)
    try:
        session_factory = build_session_factory(engine)
        if arguments.operation == "prepare-container-setup":
            if not _container_setup_needs_owner_proof(session_factory):
                print(
                    "Container setup does not require a new owner proof.",
                    file=sys.stderr,
                )
                return 0
            service = TrustedBootstrapTokenService(
                issuer=SecureBootstrapTokenIssuer(),
                registry=DatabaseBootstrapTokenRegistry(session_factory),
            )
            issued = service.issue_owner_token()
            fragment_token = quote(issued.raw_token, safe="")
            print(f"AcadematePaper setup URL: {arguments.web_origin}/setup#token={fragment_token}")
            print(
                "This URL is one-use and expires at "
                f"{issued.expires_at.isoformat()}; it is intentionally visible only in "
                "operator-controlled output (the API log for manual Compose startup, or "
                "launcher stdout).",
                file=sys.stderr,
            )
            return 0

        if arguments.operation == "issue-owner-token":
            service = TrustedBootstrapTokenService(
                issuer=SecureBootstrapTokenIssuer(),
                registry=DatabaseBootstrapTokenRegistry(session_factory),
            )
            issued = service.issue_owner_token()
            # The raw proof is an intentional one-time handoff to the trusted launcher/operator.
            # It is never passed through logging and its dataclass representation is redacted.
            print(issued.raw_token)
            print(
                f"Owner setup proof expires at {issued.expires_at.isoformat()}.",
                file=sys.stderr,
            )
            return 0

        if arguments.operation == "issue-launch-token":
            repository = DatabaseLocalSessionRepository(session_factory)
            service = TrustedLocalLaunchTokenService(
                codec=SecureLocalSessionTokenCodec(),
                registry=repository,
            )
            issued = service.issue()
            # The fragment is never sent in an HTTP request.  The client clears it with
            # replaceState before POSTing the proof through the same-origin BFF.
            fragment_token = quote(issued.raw_token, safe="")
            print(f"{arguments.web_origin}/local-launch#token={fragment_token}")
            print(
                f"Browser launch proof expires at {issued.expires_at.isoformat()} and is one-use.",
                file=sys.stderr,
            )
            return 0

        verifier = SetupRuntimeVerificationService(
            state_store=DatabaseSetupStateStore(session_factory),
            verifier=WorkerRuntimeVerifier(
                settings,
                external_acceptance_roots=arguments.accept_external_toolchain_roots,
            ),
            identity_invariant=DatabaseLocalInstallationIdentityInvariant(session_factory),
            schema_invariant=DatabaseSchemaRevisionInvariant(session_factory),
        )
        try:
            result = verifier.run(reverify_ready=arguments.reverify_ready)
        except RuntimeVerificationFailedError as error:
            print(
                "Runtime verification did not pass "
                f"({error.failure_code}); setup remains retryable.",
                file=sys.stderr,
            )
            return 1
        print(
            "AcadematePaper setup is READY. "
            f"Verified {len(result.capability_states)} runtime capabilities."
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
