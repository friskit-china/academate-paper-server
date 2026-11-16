from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.setup import IssuedBootstrapToken

_BOOTSTRAP_DIGEST_DOMAIN = b"academatepaper.bootstrap-token.v1\0"
_PASSWORD_SCHEME = "scrypt-v1"
_OWNER_SETUP_TOKEN_TTL = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SecureBootstrapTokenIssuer:
    ttl: timedelta = _OWNER_SETUP_TOKEN_TTL
    entropy_bytes: int = 32

    def __post_init__(self) -> None:
        if self.entropy_bytes < 32:
            raise ValueError("Bootstrap tokens require at least 256 bits of entropy.")
        if not timedelta(seconds=1) <= self.ttl <= _OWNER_SETUP_TOKEN_TTL:
            raise ValueError("Bootstrap token TTL must be between one second and 24 hours.")

    def issue(self, *, now: datetime | None = None) -> IssuedBootstrapToken:
        issued_at = now or datetime.now(UTC)
        raw_token = secrets.token_urlsafe(self.entropy_bytes)
        return IssuedBootstrapToken(
            raw_token=raw_token,
            token_digest=self._digest(raw_token),
            expires_at=issued_at + self.ttl,
        )

    def matches(self, raw_token: str, token_digest: str) -> bool:
        if not raw_token or not token_digest:
            return False
        return hmac.compare_digest(self._digest(raw_token), token_digest)

    @staticmethod
    def _digest(raw_token: str) -> str:
        digest = hashlib.sha256(_BOOTSTRAP_DIGEST_DOMAIN + raw_token.encode("utf-8"))
        return f"sha256-v1:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class ScryptPasswordHasher:
    """Versioned password hashing using the packaged Python/OpenSSL runtime.

    This is deliberately independent of the OS keyring: login credentials remain portable user
    data, while encryption master keys belong to SecretStore adapters.
    """

    n: int = 2**14
    r: int = 8
    p: int = 1
    salt_bytes: int = 16
    derived_key_bytes: int = 32

    def hash(self, password: str) -> str:
        if not password:
            raise ValueError("Password must not be empty.")
        salt = secrets.token_bytes(self.salt_bytes)
        derived_key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.derived_key_bytes,
        )
        return "$".join(
            (
                _PASSWORD_SCHEME,
                f"n={self.n},r={self.r},p={self.p}",
                base64.urlsafe_b64encode(salt).decode("ascii"),
                base64.urlsafe_b64encode(derived_key).decode("ascii"),
            )
        )

    def verify(self, password: str, encoded_hash: str) -> bool:
        try:
            scheme, parameters, salt_text, expected_text = encoded_hash.split("$", maxsplit=3)
            if scheme != _PASSWORD_SCHEME:
                return False
            parsed = dict(item.split("=", maxsplit=1) for item in parameters.split(","))
            n = int(parsed["n"])
            r = int(parsed["r"])
            p = int(parsed["p"])
            salt = base64.b64decode(salt_text, altchars=b"-_", validate=True)
            expected = base64.b64decode(expected_text, altchars=b"-_", validate=True)
            if (
                n < 2**14
                or n > 2**20
                or n & (n - 1) != 0
                or not 8 <= r <= 32
                or not 1 <= p <= 16
                or not 16 <= len(salt) <= 64
                or not 32 <= len(expected) <= 64
            ):
                return False
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                dklen=len(expected),
            )
        except (KeyError, MemoryError, OverflowError, TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)
