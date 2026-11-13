from __future__ import annotations

import unicodedata

PERSONAL_ACCOUNT = "admin"

MAX_DISPLAY_NAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 1024


def normalized_username(value: str) -> str:
    """Return the canonical account identifier used for storage and login."""

    normalized = unicodedata.normalize("NFKC", value.strip()).casefold()
    if not 3 <= len(normalized) <= 64:
        raise ValueError("Username must contain between 3 and 64 characters.")
    if len(normalized.encode("utf-8")) > 256:
        raise ValueError("Username is too large after UTF-8 encoding.")
    if not normalized[0].isalnum() or any(
        not (character.isalnum() or character in "._-") for character in normalized
    ):
        raise ValueError(
            "Username must start with a letter or number and contain only letters, numbers, "
            "., _, or -."
        )
    return normalized


def normalized_display_name(value: str) -> str:
    """Return the validated, single-line display label shared by every account flow."""

    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= MAX_DISPLAY_NAME_LENGTH
        or len(normalized.encode("utf-8")) > 512
    ):
        raise ValueError("Display name must contain between 1 and 128 characters.")
    if any(character in "\x00\r\n" for character in normalized):
        raise ValueError("Display name must be one line without null characters.")
    return normalized


def validated_password(value: str) -> str:
    """Validate the account password without normalizing or otherwise changing it."""

    if (
        not 12 <= len(value) <= MAX_PASSWORD_LENGTH
        or len(value.encode("utf-8")) > 4096
    ):
        raise ValueError("Initial password must contain between 12 and 1024 characters.")
    if "\x00" in value:
        raise ValueError("Initial password must not contain null characters.")
    return value
