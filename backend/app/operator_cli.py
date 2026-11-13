from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Never

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.application.invitations import (
    InvitationActor,
    InvitationDeniedError,
    InvitationModeError,
    InvitationNotFoundError,
    InvitationNotRevocableError,
    InvitationService,
    InvitationStateError,
    InvitationSummary,
)
from app.config import RuntimeConfigurationError, Settings, get_settings
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.invitations import (
    DatabaseInvitationRepository,
    SecureInvitationTokenCodec,
)

EXIT_USAGE = 2
EXIT_INSTALLATION_POLICY = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_RUNTIME = 6


class _OperatorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"operator-cli: [INVALID_ARGUMENT] {message}\n")


def _duration(value: str) -> timedelta:
    units = {
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
    }
    normalized = value.strip().lower()
    if len(normalized) < 2 or normalized[-1] not in units:
        raise argparse.ArgumentTypeError("有效期必须使用正整数加 m、h 或 d，例如 30m、24h、7d")
    magnitude = normalized[:-1]
    if not magnitude.isascii() or not magnitude.isdecimal() or int(magnitude) <= 0:
        raise argparse.ArgumentTypeError("有效期必须使用正整数加 m、h 或 d，例如 30m、24h、7d")
    try:
        return timedelta(seconds=int(magnitude) * units[normalized[-1]])
    except OverflowError as error:
        raise argparse.ArgumentTypeError("有效期超出可表示范围") from error


def _parser() -> argparse.ArgumentParser:
    parser = _OperatorArgumentParser(
        prog="python -m app.operator_cli",
        description="AcadematePaper container-only deployment operator commands.",
    )
    resources = parser.add_subparsers(dest="resource", required=True)
    invitation = resources.add_parser("invite", help="管理当前安装 owner workspace 的邀请码")
    commands = invitation.add_subparsers(dest="operation", required=True)

    create = commands.add_parser("create", help="创建一次性邀请码")
    create.add_argument("--expires-in", type=_duration, default=_duration("7d"))
    create.add_argument("--label")

    list_parser = commands.add_parser("list", help="列出邀请码（默认仅 ACTIVE）")
    list_parser.add_argument("--all", action="store_true", dest="include_inactive")

    status = commands.add_parser("status", help="查看一个邀请码")
    status.add_argument("invitation_id")

    revoke = commands.add_parser("revoke", help="撤销一个尚未兑换的邀请码")
    revoke.add_argument("invitation_id")
    return parser


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _summary_payload(invitation: InvitationSummary) -> dict[str, object]:
    # InvitationSummary deliberately contains neither the token digest nor the raw proof.
    return {
        key: _json_value(value)
        for key, value in asdict(invitation).items()
    }


def _write_json(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _invitation_error(error: Exception) -> int:
    if isinstance(error, InvitationModeError):
        print(
            "operator-cli: [INVITATIONS_MODE_DISABLED] 当前安装不是 READY + SERVER + "
            "MULTI_USER；请先在首次配置中选择多用户模式。",
            file=sys.stderr,
        )
        return EXIT_INSTALLATION_POLICY
    if isinstance(error, (InvitationStateError, InvitationDeniedError)):
        print(
            "operator-cli: [INVITATIONS_NOT_READY] 当前安装状态或 owner workspace 无法安全"
            "管理邀请码；请运行 amp status，并先完成多用户首次配置。",
            file=sys.stderr,
        )
        return EXIT_INSTALLATION_POLICY
    if isinstance(error, InvitationNotFoundError):
        print(
            "operator-cli: [INVITATION_NOT_FOUND] 未找到当前 workspace 中的该邀请码。",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND
    if isinstance(error, InvitationNotRevocableError):
        print(
            "operator-cli: [INVITATION_NOT_REVOCABLE] 只有 ACTIVE 且尚未兑换的邀请码可以撤销。",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    raise error


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        application_settings = settings or get_settings()
        application_settings.validate_runtime_configuration()
        if application_settings.web_public_origin is None:
            raise RuntimeConfigurationError(
                "WEB_PUBLIC_ORIGIN is required for operator invitation URLs."
            )
        engine = build_engine(application_settings.database_url)
    except (RuntimeConfigurationError, ValidationError, ValueError):
        print(
            "operator-cli: [RUNTIME_CONFIGURATION_INVALID] 容器运行配置无效；请恢复受管 "
            "amp.env 后重试。",
            file=sys.stderr,
        )
        return EXIT_RUNTIME

    try:
        service = InvitationService(
            repository=DatabaseInvitationRepository(build_session_factory(engine)),
            token_codec=SecureInvitationTokenCodec(),
            web_public_origin=application_settings.web_public_origin,
        )
        actor = InvitationActor.operator_cli()
        try:
            if arguments.operation == "create":
                created = service.create(
                    actor=actor,
                    expires_in=arguments.expires_in,
                    label=arguments.label,
                )
                # This is the sole intended raw-token handoff. Do not add prefixes, JSON, logging,
                # or a second copy: amp forwards this exact stdout line to its operator.
                print(created.registration_url)
                print(
                    "operator-cli: 已创建 OPERATOR_CLI 邀请 "
                    f"{created.invitation.id}（{created.invitation.masked_hint}），"
                    f"到期时间 {created.invitation.expires_at.isoformat()}。",
                    file=sys.stderr,
                )
                return 0
            if arguments.operation == "list":
                invitations = service.list(
                    actor=actor,
                    include_inactive=arguments.include_inactive,
                )
                _write_json(
                    {
                        "invitations": [
                            _summary_payload(invitation) for invitation in invitations
                        ]
                    }
                )
                return 0
            if arguments.operation == "status":
                _write_json(
                    _summary_payload(
                        service.status(
                            actor=actor,
                            invitation_id=arguments.invitation_id,
                        )
                    )
                )
                return 0
            if arguments.operation == "revoke":
                _write_json(
                    _summary_payload(
                        service.revoke(
                            actor=actor,
                            invitation_id=arguments.invitation_id,
                        )
                    )
                )
                return 0
            raise AssertionError("argparse accepted an unsupported operator operation")
        except ValueError:
            print(
                "operator-cli: [INVALID_INVITATION_INPUT] 邀请参数无效；有效期必须介于 1 分钟"
                "和 90 天，备注必须是最多 128 字符的单行文本。",
                file=sys.stderr,
            )
            return EXIT_USAGE
        except (
            InvitationDeniedError,
            InvitationModeError,
            InvitationNotFoundError,
            InvitationNotRevocableError,
            InvitationStateError,
        ) as error:
            return _invitation_error(error)
        except SQLAlchemyError:
            print(
                "operator-cli: [DATABASE_UNAVAILABLE] 无法读取容器数据卷中的数据库；请检查 "
                "amp status 和 API 容器日志后重试。",
                file=sys.stderr,
            )
            return EXIT_RUNTIME
        except Exception:
            # Deployment operators need a stable, secret-free recovery surface. Detailed
            # tracebacks stay out of stdout/stderr because this command may have just minted a
            # one-use proof; API container diagnostics can still identify infrastructure faults.
            print(
                "operator-cli: [OPERATOR_RUNTIME_FAILED] 容器内邀请操作失败；请检查 API 容器"
                "状态与日志后重试。",
                file=sys.stderr,
            )
            return EXIT_RUNTIME
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
