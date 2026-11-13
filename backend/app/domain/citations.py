from __future__ import annotations

import unicodedata

MIN_RECOVERABLE_QUOTE_CHARS = 8

_TYPOGRAPHIC_EQUIVALENTS = {
    "\u00a0": " ",
    "\u2007": " ",
    "\u202f": " ",
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "−": "-",
}


def _fold_with_offsets(value: str) -> tuple[str, list[int], list[int]]:
    folded: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, source_character in enumerate(value):
        equivalent = _TYPOGRAPHIC_EQUIVALENTS.get(source_character, source_character)
        normalized = unicodedata.normalize("NFKC", equivalent).casefold()
        for character in normalized:
            character = _TYPOGRAPHIC_EQUIVALENTS.get(character, character)
            if character.isspace():
                if folded and folded[-1] == " ":
                    ends[-1] = index + 1
                    continue
                character = " "
            folded.append(character)
            starts.append(index)
            ends.append(index + 1)
    return "".join(folded), starts, ends


def resolve_canonical_quote(canonical_text: str, supplied_quote: str) -> str | None:
    """Return the exact canonical slice for a safely equivalent provider quote.

    Recovery is deliberately not fuzzy: only Unicode compatibility, case,
    whitespace, quote marks, and dash typography may differ, and the normalized
    match must be unique inside the cited block.
    """
    if not canonical_text or not supplied_quote:
        return None
    if supplied_quote in canonical_text:
        return supplied_quote

    folded_text, starts, ends = _fold_with_offsets(canonical_text)
    folded_quote, _, _ = _fold_with_offsets(supplied_quote)
    folded_quote = folded_quote.strip()
    if len(folded_quote) < MIN_RECOVERABLE_QUOTE_CHARS:
        return None

    matches: list[int] = []
    cursor = 0
    while True:
        position = folded_text.find(folded_quote, cursor)
        if position < 0:
            break
        matches.append(position)
        cursor = position + 1
    if len(matches) != 1:
        return None

    position = matches[0]
    end_position = position + len(folded_quote) - 1
    if position >= len(starts) or end_position >= len(ends):
        return None
    return canonical_text[starts[position] : ends[end_position]]
