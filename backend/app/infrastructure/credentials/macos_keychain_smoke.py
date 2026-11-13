from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from app.infrastructure.credentials.macos_keychain import (
    KeychainCommandRunner,
    run_macos_keychain_smoke,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: KeychainCommandRunner | None = None,
    platform_name: str = sys.platform,
) -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in native macOS Keychain write/read/delete smoke.",
    )
    parser.add_argument("--installation-id", required=True, type=UUID)
    parser.add_argument(
        "--confirm-write-delete",
        action="store_true",
        help="confirm that the isolated smoke service may be modified and cleaned up",
    )
    arguments = parser.parse_args(argv)
    if not arguments.confirm_write_delete:
        parser.error("--confirm-write-delete is required; no Keychain operation was attempted")
    run_macos_keychain_smoke(
        arguments.installation_id,
        platform_name=platform_name,
        runner=runner,
    )
    print(json.dumps({"status": "ok"}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
