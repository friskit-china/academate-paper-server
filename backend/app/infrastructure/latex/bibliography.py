from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from app.domain.ports import Storage

BIBLIOGRAPHY_PROCESSOR_VERSION = "biber-bibtex-bbl-v7"
BIBLIOGRAPHY_FALLBACK_STYLE = "IEEEtran"
BIBLIOGRAPHY_MAX_SOURCE_BYTES = 20_000_000
BIBLIOGRAPHY_MAX_ENTRIES = 20_000
BIBLIOGRAPHY_MAX_OUTPUT_BYTES = 50_000_000
BIBLIOGRAPHY_TIMEOUT_SECONDS = 60

_BIBLATEX_XML_NAMESPACE = "http://biblatex-biber.sourceforge.net/biblatexml"
_BIBLATEX_TAG_PREFIX = f"{{{_BIBLATEX_XML_NAMESPACE}}}"
_SAFE_STYLE_RE = re.compile(r"^[A-Za-z0-9_.+/-]+$")
_ENTRY_START_RE = re.compile(r"@(?P<type>[A-Za-z]+)\s*(?P<opening>[{(])")
_CITE_AUTHOR_YEAR_RE = re.compile(r"\\(?:protect\s*)?\\?citeauthoryear\s*\{")
_BIBER_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:.+/-]+$")
_BIBTEX_RELATIONSHIP_FIELD_RE = re.compile(
    r"(?P<prefix>\b(?:crossref|xref|xdata|related|entryset)\s*=\s*)"
    r"(?P<opening>[{\"])(?P<value>[^{}\"]*)(?P<closing>[}\"])",
    re.IGNORECASE,
)


class BibliographyProcessingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BibliographyPerson:
    role: str
    position: int
    display_name: str
    given_names: tuple[str, ...]
    family_names: tuple[str, ...]
    prefix_names: tuple[str, ...]
    suffix_names: tuple[str, ...]
    initials: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BibliographyEntry:
    citation_key: str
    entry_type: str
    source_path: str | None
    raw_bibtex: str | None
    fields: dict[str, Any]
    persons: tuple[BibliographyPerson, ...]
    title: str | None = None
    container_title: str | None = None
    year: str | None = None
    doi: str | None = None
    url: str | None = None
    eprint: str | None = None
    archive_prefix: str | None = None
    isbn: str | None = None
    issn: str | None = None
    ordinal: int | None = None
    display_label: str | None = None
    citation_author: str | None = None
    citation_year: str | None = None
    formatted_latex: str | None = None


@dataclass(frozen=True, slots=True)
class BibliographyResult:
    status: str
    requested_style: str | None
    resolved_style: str | None
    fallback_used: bool
    source_files: tuple[str, ...]
    entries: tuple[BibliographyEntry, ...]
    warnings: tuple[str, ...]
    processor_version: str = BIBLIOGRAPHY_PROCESSOR_VERSION


def _decode_source(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _read_balanced(
    value: str,
    cursor: int,
    *,
    opening: str,
    closing: str,
) -> tuple[str, int] | None:
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    if cursor >= len(value) or value[cursor] != opening:
        return None
    depth = 0
    brace_depth = 0
    quoted = False
    for index in range(cursor, len(value)):
        character = value[index]
        if _is_escaped(value, index):
            continue
        if character == '"' and brace_depth == 0:
            quoted = not quoted
            continue
        if quoted:
            continue
        if character == "{":
            brace_depth += 1
        elif character == "}":
            brace_depth = max(0, brace_depth - 1)
        if character == opening and (opening == "{" or brace_depth == 0):
            depth += 1
        elif character == closing and (closing == "}" or brace_depth == 0):
            depth -= 1
            if depth == 0:
                return value[cursor + 1 : index], index + 1
    return None


def _raw_entries_by_key(
    sources: list[tuple[str, str]],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    entries: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    for source_path, source in sources:
        cursor = 0
        while match := _ENTRY_START_RE.search(source, cursor):
            entry_type = match.group("type").casefold()
            opening = match.group("opening")
            closing = "}" if opening == "{" else ")"
            parsed = _read_balanced(source, match.end() - 1, opening=opening, closing=closing)
            if parsed is None:
                warnings.append(f"Unbalanced BibTeX entry in {source_path}.")
                cursor = match.end()
                continue
            content, end = parsed
            cursor = end
            if entry_type in {"comment", "preamble", "string"}:
                continue
            key = content.split(",", 1)[0].strip()
            if not key:
                continue
            normalized = key.casefold()
            if normalized in entries:
                warnings.append(
                    f"Duplicate BibTeX key {key!r}; the first declaration remains canonical."
                )
                continue
            entries[normalized] = (source_path, source[match.start() : end].strip())
    return entries, warnings


def _deduplicate_bibtex_sources_for_compilers(
    sources: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], int]:
    """Keep the first case-insensitive BibTeX key in the temporary compiler view.

    arXiv submissions sometimes concatenate several bibliography databases and leave
    repeated entries behind. BibTeX/Biber may tolerate exact duplicates or treat keys
    that differ only by case as distinct, while AcadematePaper deliberately resolves
    citation keys case-insensitively. Removing only the later entry declarations from
    the temporary compiler input keeps those semantics aligned without modifying the
    archived source, string declarations, preambles, comments, or citation text.
    """

    seen_keys: set[str] = set()
    deduplicated_sources: list[tuple[str, str]] = []
    duplicate_count = 0
    for source_path, source in sources:
        removals: list[tuple[int, int]] = []
        cursor = 0
        while match := _ENTRY_START_RE.search(source, cursor):
            opening = match.group("opening")
            parsed = _read_balanced(
                source,
                match.end() - 1,
                opening=opening,
                closing="}" if opening == "{" else ")",
            )
            if parsed is None:
                cursor = match.end()
                continue
            content, end = parsed
            cursor = end
            if match.group("type").casefold() in {"comment", "preamble", "string"}:
                continue
            citation_key = content.split(",", 1)[0].strip()
            if not citation_key:
                continue
            normalized_key = citation_key.casefold()
            if normalized_key in seen_keys:
                removals.append((match.start(), end))
                duplicate_count += 1
                continue
            seen_keys.add(normalized_key)
        for start, end in reversed(removals):
            source = f"{source[:start]}{source[end:]}"
        deduplicated_sources.append((source_path, source))
    return deduplicated_sources, duplicate_count


def _bibtex_entry_keys(source: str) -> tuple[str, ...]:
    keys: list[str] = []
    cursor = 0
    while match := _ENTRY_START_RE.search(source, cursor):
        opening = match.group("opening")
        parsed = _read_balanced(
            source,
            match.end() - 1,
            opening=opening,
            closing="}" if opening == "{" else ")",
        )
        if parsed is None:
            cursor = match.end()
            continue
        content, end = parsed
        cursor = end
        if match.group("type").casefold() in {"comment", "preamble", "string"}:
            continue
        key = content.split(",", 1)[0].strip()
        if key:
            keys.append(key)
    return tuple(keys)


def _biber_safe_aliases(sources: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    keys = tuple(key for _path, source in sources for key in _bibtex_entry_keys(source))
    occupied = {key.casefold() for key in keys}
    aliases: dict[str, tuple[str, str]] = {}
    for key in keys:
        normalized = key.casefold()
        if _BIBER_SAFE_KEY_RE.fullmatch(key) or normalized in aliases:
            continue
        digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
        length = 16
        safe_key = f"academate-biber-{digest[:length]}"
        while safe_key.casefold() in occupied:
            length += 4
            safe_key = f"academate-biber-{digest[:length]}"
        occupied.add(safe_key.casefold())
        aliases[normalized] = (key, safe_key)
    return aliases


def _replace_bibtex_relationship_keys(
    source: str,
    aliases: dict[str, tuple[str, str]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        values: list[str] = []
        for raw_value in match.group("value").split(","):
            leading = raw_value[: len(raw_value) - len(raw_value.lstrip())]
            trailing = raw_value[len(raw_value.rstrip()) :]
            key = raw_value.strip()
            alias = aliases.get(key.casefold())
            values.append(f"{leading}{alias[1] if alias else key}{trailing}")
        return (
            f"{match.group('prefix')}{match.group('opening')}"
            f"{','.join(values)}{match.group('closing')}"
        )

    return _BIBTEX_RELATIONSHIP_FIELD_RE.sub(replace, source)


def _replace_bibtex_entry_keys(
    source: str,
    aliases: dict[str, tuple[str, str]],
) -> str:
    replacements: list[tuple[int, int, str]] = []
    cursor = 0
    while match := _ENTRY_START_RE.search(source, cursor):
        opening = match.group("opening")
        parsed = _read_balanced(
            source,
            match.end() - 1,
            opening=opening,
            closing="}" if opening == "{" else ")",
        )
        if parsed is None:
            cursor = match.end()
            continue
        content, end = parsed
        cursor = end
        comma = content.find(",")
        if comma < 0:
            continue
        raw_key = content[:comma]
        key = raw_key.strip()
        alias = aliases.get(key.casefold())
        if alias is None:
            continue
        leading = len(raw_key) - len(raw_key.lstrip())
        trailing = len(raw_key) - len(raw_key.rstrip())
        key_start = match.end() + leading
        key_end = match.end() + len(raw_key) - trailing
        replacements.append((key_start, key_end, alias[1]))
    for start, end, replacement in reversed(replacements):
        source = f"{source[:start]}{replacement}{source[end:]}"
    return source


def _biber_staging_bibliography(
    sources: list[tuple[str, str]],
) -> tuple[str, dict[str, str], int]:
    """Create a compiler-only BibTeX view with reversible, Biber-safe entry IDs.

    Some author sources use citation keys accepted by their BibTeX workflow but rejected by
    Biber's tool-mode parser. Only the temporary compiler input is rewritten; canonical source,
    citation occurrences, raw BibTeX, and persisted keys retain the author's exact identifiers.
    """

    aliases = _biber_safe_aliases(sources)
    staged_sources = []
    for _path, source in sources:
        staged = _replace_bibtex_relationship_keys(source, aliases)
        staged_sources.append(_replace_bibtex_entry_keys(staged, aliases))
    reverse_aliases = {safe.casefold(): original for original, safe in aliases.values()}
    return "\n\n".join(staged_sources), reverse_aliases, len(aliases)


def _local_name(element: ET.Element) -> str:
    return element.tag.removeprefix(_BIBLATEX_TAG_PREFIX)


def _xml_scalar(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _xml_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return _xml_scalar(element)
    if _local_name(element) == "list":
        return [_xml_value(child) for child in children]
    grouped: dict[str, Any] = {}
    for child in children:
        key = _local_name(child)
        child_value = _xml_value(child)
        if key not in grouped:
            grouped[key] = child_value
        elif isinstance(grouped[key], list):
            grouped[key].append(child_value)
        else:
            grouped[key] = [grouped[key], child_value]
    attributes = {f"@{key}": value for key, value in element.attrib.items()}
    return {**attributes, **grouped}


def _restore_biber_aliases(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        return aliases.get(value.casefold(), value)
    if isinstance(value, list):
        return [_restore_biber_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _restore_biber_aliases(item, aliases) for key, item in value.items()}
    return value


def _name_part_values(element: ET.Element, part_type: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in element.findall(f"{_BIBLATEX_TAG_PREFIX}namepart"):
        if part.get("type") != part_type:
            continue
        nested = list(part)
        if nested:
            parts.extend(_xml_scalar(item) for item in nested if _xml_scalar(item))
        else:
            value = _xml_scalar(part)
            if value:
                parts.append(value)
    return tuple(parts)


def _name_initials(element: ET.Element) -> tuple[str, ...]:
    initials: list[str] = []
    for part in element.iter(f"{_BIBLATEX_TAG_PREFIX}namepart"):
        initial = part.get("initial")
        if initial:
            initials.append(initial)
    return tuple(initials)


def _persons(entry_element: ET.Element) -> tuple[BibliographyPerson, ...]:
    people: list[BibliographyPerson] = []
    for names in entry_element.findall(f"{_BIBLATEX_TAG_PREFIX}names"):
        role = names.get("type", "author")
        for position, name in enumerate(names.findall(f"{_BIBLATEX_TAG_PREFIX}name")):
            given = _name_part_values(name, "given")
            family = _name_part_values(name, "family")
            prefix = _name_part_values(name, "prefix")
            suffix = _name_part_values(name, "suffix")
            display = " ".join((*given, *prefix, *family, *suffix)).strip()
            people.append(
                BibliographyPerson(
                    role=role,
                    position=position,
                    display_name=display,
                    given_names=given,
                    family_names=family,
                    prefix_names=prefix,
                    suffix_names=suffix,
                    initials=_name_initials(name),
                )
            )
    return tuple(people)


def _flatten_field(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [part for item in value if (part := _flatten_field(item))]
        return "; ".join(parts) or None
    if isinstance(value, dict):
        if "start" in value:
            start = _flatten_field(value.get("start")) or ""
            end = _flatten_field(value.get("end"))
            return f"{start}--{end}" if end else start or None
        public_values = [
            part
            for key, item in value.items()
            if not key.startswith("@") and (part := _flatten_field(item))
        ]
        return "; ".join(public_values) or None
    return None


def parse_biblatexml(
    xml_source: str,
    *,
    raw_entries: dict[str, tuple[str, str]] | None = None,
    citation_key_aliases: dict[str, str] | None = None,
) -> tuple[BibliographyEntry, ...]:
    try:
        root = ET.fromstring(xml_source)
    except ET.ParseError as error:
        raise BibliographyProcessingError(f"Biber returned invalid BibLaTeXML: {error}") from error
    entries: list[BibliographyEntry] = []
    raw_by_key = raw_entries or {}
    aliases = citation_key_aliases or {}
    for entry_element in root.findall(f"{_BIBLATEX_TAG_PREFIX}entry"):
        if len(entries) >= BIBLIOGRAPHY_MAX_ENTRIES:
            raise BibliographyProcessingError(
                f"Bibliography exceeds the {BIBLIOGRAPHY_MAX_ENTRIES}-entry safety limit."
            )
        compiler_key = entry_element.get("id", "").strip()
        citation_key = aliases.get(compiler_key.casefold(), compiler_key)
        if not citation_key:
            continue
        fields: dict[str, Any] = {}
        for child in entry_element:
            name = _local_name(child)
            if name == "names":
                continue
            value = _restore_biber_aliases(_xml_value(child), aliases)
            if name not in fields:
                fields[name] = value
            elif isinstance(fields[name], list):
                fields[name].append(value)
            else:
                fields[name] = [fields[name], value]
        source_path, raw_bibtex = raw_by_key.get(citation_key.casefold(), (None, None))
        title = _flatten_field(fields.get("title"))
        container_title = _flatten_field(
            fields.get("journaltitle") or fields.get("booktitle") or fields.get("maintitle")
        )
        date = _flatten_field(fields.get("date") or fields.get("year"))
        year_match = re.search(r"[+-]?\d{4,}", date or "")
        entries.append(
            BibliographyEntry(
                citation_key=citation_key,
                entry_type=entry_element.get("entrytype", "misc").casefold(),
                source_path=source_path,
                raw_bibtex=raw_bibtex,
                fields=fields,
                persons=_persons(entry_element),
                title=title,
                container_title=container_title,
                year=year_match.group(0) if year_match else date,
                doi=_flatten_field(fields.get("doi")),
                url=_flatten_field(fields.get("url")),
                eprint=_flatten_field(fields.get("eprint")),
                archive_prefix=_flatten_field(
                    fields.get("eprinttype") or fields.get("archiveprefix")
                ),
                isbn=_flatten_field(fields.get("isbn")),
                issn=_flatten_field(fields.get("issn")),
            )
        )
    return tuple(entries)


def _plain_latex(value: str) -> str:
    accent_marks = {
        "'": "\N{COMBINING ACUTE ACCENT}",
        "`": "\N{COMBINING GRAVE ACCENT}",
        "^": "\N{COMBINING CIRCUMFLEX ACCENT}",
        '"': "\N{COMBINING DIAERESIS}",
        "~": "\N{COMBINING TILDE}",
        "=": "\N{COMBINING MACRON}",
        ".": "\N{COMBINING DOT ABOVE}",
        "u": "\N{COMBINING BREVE}",
        "v": "\N{COMBINING CARON}",
        "H": "\N{COMBINING DOUBLE ACUTE ACCENT}",
        "c": "\N{COMBINING CEDILLA}",
        "d": "\N{COMBINING DOT BELOW}",
        "b": "\N{COMBINING MACRON BELOW}",
        "r": "\N{COMBINING RING ABOVE}",
        "k": "\N{COMBINING OGONEK}",
    }

    def replace_accent(match: re.Match[str]) -> str:
        base = match.group("braced") or match.group("bare") or ""
        base = {r"\i": "i", r"\j": "j"}.get(base, base)
        return unicodedata.normalize("NFC", base + accent_marks[match.group("accent")])

    text = re.sub(r"\\ndash\s*", "--", value)
    text = re.sub(
        r'''\\(?P<accent>['`^"~=.]|[uvHcdbrk](?![A-Za-z@]))\s*'''
        r"(?:\{\s*(?P<braced>\\[ij]|[A-Za-z])\s*\}|(?P<bare>\\[ij]|[A-Za-z]))",
        replace_accent,
        text,
    )
    symbols = {
        "ss": "ß",
        "ae": "æ",
        "AE": "Æ",
        "oe": "œ",
        "OE": "Œ",
        "aa": "å",
        "AA": "Å",
        "o": "ø",
        "O": "Ø",
        "l": "ł",
        "L": "Ł",
    }
    text = re.sub(
        r"\\(?P<symbol>ss|ae|AE|oe|OE|aa|AA|o|O|l|L)(?![A-Za-z@])",
        lambda match: symbols[match.group("symbol")],
        text,
    )
    text = re.sub(r"\\(?:protect|newblock)\b", " ", text)
    text = re.sub(r"\\(?:url|href)\s*\{([^{}]*)\}(?:\s*\{([^{}]*)\})?", r"\2\1", text)
    text = re.sub(r"\\[A-Za-z@]+\*?", "", text)
    text = text.replace("{", "").replace("}", "").replace("~", " ")
    text = text.replace("---", "—").replace("--", "–")
    return " ".join(text.split())


def _parse_cite_author_year(label_latex: str) -> tuple[str | None, str | None]:
    match = _CITE_AUTHOR_YEAR_RE.search(label_latex)
    if match is None:
        plain_label = _plain_latex(label_latex)
        # Natbib-compatible BST files encode both the short and long author forms in the
        # optional bibitem label: ``short author(year)long author``.  Only the prefix through
        # the balanced year is the visible citation identity; the suffix exists for commands
        # such as ``\citeauthor*`` and must not leak into ordinary citations or reference labels.
        natbib_label = re.match(
            r"^(?P<author>.+?)\((?P<year>[^()]+)\)(?P<long_author>.*)$",
            plain_label,
        )
        if natbib_label is None:
            return None, None
        return natbib_label.group("author").strip(), natbib_label.group("year").strip()
    cursor = match.end() - 1
    arguments: list[str] = []
    for _index in range(3):
        parsed = _read_balanced(label_latex, cursor, opening="{", closing="}")
        if parsed is None:
            return None, None
        argument, cursor = parsed
        arguments.append(argument)
        while cursor < len(label_latex) and label_latex[cursor].isspace():
            cursor += 1
    return _plain_latex(arguments[1] or arguments[0]) or None, _plain_latex(arguments[2]) or None


def _compact_author_year_label(author: object, year: object) -> str | None:
    normalized_author = " ".join(str(author or "").split())
    normalized_year = " ".join(str(year or "").split())
    if not normalized_author or not normalized_year:
        return None
    return f"{normalized_author} ({normalized_year})"


def parse_bbl_items(bbl_source: str) -> tuple[dict[str, Any], ...]:
    token_re = re.compile(r"\\bibitem(?P<star>\*)?")
    matches = list(token_re.finditer(bbl_source))
    items: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        cursor = match.end()
        while cursor < len(bbl_source) and bbl_source[cursor].isspace():
            cursor += 1
        label_latex: str | None = None
        if cursor < len(bbl_source) and bbl_source[cursor] == "[":
            parsed_label = _read_balanced(bbl_source, cursor, opening="[", closing="]")
            if parsed_label is None:
                continue
            label_latex, cursor = parsed_label
        parsed_key = _read_balanced(bbl_source, cursor, opening="{", closing="}")
        if parsed_key is None:
            continue
        citation_key, content_start = parsed_key
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(bbl_source)
        bibliography_end = bbl_source.find(r"\end{thebibliography}", content_start, content_end)
        if bibliography_end >= 0:
            content_end = bibliography_end
        content = bbl_source[content_start:content_end].strip()
        author, year = _parse_cite_author_year(label_latex or "")
        plain_label = _plain_latex(label_latex or "") or None
        items.append(
            {
                "citation_key": citation_key.strip(),
                "label_latex": label_latex,
                "label_text": _compact_author_year_label(author, year) or plain_label,
                "citation_author": author,
                "citation_year": year,
                "formatted_latex": content,
            }
        )
    return tuple(items)


def _amsrefs_fields(value: str) -> dict[str, Any]:
    """Parse the balanced field list emitted by the AMSRefs compiler.

    Repeated fields (most notably ``author``) remain ordered lists. Unknown fields are
    retained instead of being filtered through a schema, which keeps the representation
    forward-compatible with AMSRefs extensions.
    """

    fields: dict[str, Any] = {}
    cursor = 0
    while cursor < len(value):
        while cursor < len(value) and (value[cursor].isspace() or value[cursor] == ","):
            cursor += 1
        if cursor >= len(value):
            break
        field_match = re.match(r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=", value[cursor:])
        if field_match is None:
            next_comma = value.find(",", cursor)
            cursor = len(value) if next_comma < 0 else next_comma + 1
            continue
        name = field_match.group("name").casefold()
        cursor += field_match.end()
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] == "{":
            parsed = _read_balanced(value, cursor, opening="{", closing="}")
            if parsed is None:
                break
            raw, cursor = parsed
        elif cursor < len(value) and value[cursor] == '"':
            start = cursor + 1
            cursor = start
            while cursor < len(value):
                if value[cursor] == '"' and not _is_escaped(value, cursor):
                    break
                cursor += 1
            raw = value[start:cursor]
            cursor = min(len(value), cursor + 1)
        else:
            end = value.find(",", cursor)
            end = len(value) if end < 0 else end
            raw = value[cursor:end].strip()
            cursor = end
        previous = fields.get(name)
        if previous is None:
            fields[name] = raw
        elif isinstance(previous, list):
            previous.append(raw)
        else:
            fields[name] = [previous, raw]
    return fields


def _field_values(fields: dict[str, Any], name: str) -> tuple[str, ...]:
    value = fields.get(name.casefold())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value is None or not str(value).strip():
        return ()
    return (str(value).strip(),)


def _top_level_comma(value: str) -> int | None:
    depth = 0
    for index, character in enumerate(value):
        if _is_escaped(value, index):
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            return index
    return None


def _amsrefs_person(raw: str, *, role: str, position: int) -> BibliographyPerson:
    comma = _top_level_comma(raw)
    if comma is None:
        plain = _plain_latex(raw)
        parts = plain.split()
        given = tuple(parts[:-1])
        family = tuple(parts[-1:])
    else:
        family_text = _plain_latex(raw[:comma])
        given_text = _plain_latex(raw[comma + 1 :])
        given = tuple(given_text.split())
        family = (family_text,) if family_text else ()
    display = " ".join((*given, *family)).strip()
    return BibliographyPerson(
        role=role,
        position=position,
        display_name=display,
        given_names=given,
        family_names=family,
        prefix_names=(),
        suffix_names=(),
        initials=tuple(part[0] for part in given if part and part[0].isalpha()),
    )


def _clean_amsrefs_fields(raw_fields: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {
        "source_kind": "standalone-bbl",
        "compiled_format": "amsrefs",
    }
    for name, value in raw_fields.items():
        if isinstance(value, list):
            cleaned[name] = [_plain_latex(str(item)) for item in value]
        else:
            cleaned[name] = _plain_latex(str(value))
    return cleaned


def _amsrefs_formatted_latex(
    *,
    entry_type: str,
    raw_fields: dict[str, Any],
    persons: tuple[BibliographyPerson, ...],
) -> str:
    authors = [person.display_name for person in persons if person.role == "author"]
    author_text = ""
    if authors:
        author_text = (
            authors[0]
            if len(authors) == 1
            else f"{authors[0]} and {authors[1]}"
            if len(authors) == 2
            else f"{', '.join(authors[:-1])}, and {authors[-1]}"
        )
    title = next(iter(_field_values(raw_fields, "title")), "")
    if title and entry_type in {"book", "collection", "proceedings", "thesis"}:
        title = rf"\emph{{{title}}}"
    container = next(
        iter(_field_values(raw_fields, "journal") or _field_values(raw_fields, "booktitle")),
        "",
    )
    volume = next(iter(_field_values(raw_fields, "volume")), "")
    number = next(iter(_field_values(raw_fields, "number")), "")
    if container and volume:
        container = f"{container} {volume}"
    if container and number:
        container = f"{container} ({number})"
    publisher = next(iter(_field_values(raw_fields, "publisher")), "")
    date = next(iter(_field_values(raw_fields, "date")), "")
    pages = re.sub(
        r"\\ndash\s*",
        "--",
        next(iter(_field_values(raw_fields, "pages")), ""),
    )
    note = next(iter(_field_values(raw_fields, "note")), "")
    parts = [author_text, title, container, publisher, date, pages, note]
    return ". ".join(part.rstrip(".") for part in parts if part).strip() + "."


def parse_amsrefs_items(bbl_source: str) -> tuple[dict[str, Any], ...]:
    r"""Parse compiler-produced AMSRefs ``\bib`` records without a BibTeX database."""

    items: list[dict[str, Any]] = []
    token_re = re.compile(r"\\bib(?![A-Za-z@])")
    cursor = 0
    while match := token_re.search(bbl_source, cursor):
        argument_cursor = match.end()
        parsed_key = _read_balanced(bbl_source, argument_cursor, opening="{", closing="}")
        if parsed_key is None:
            cursor = match.end()
            continue
        citation_key, argument_cursor = parsed_key
        parsed_type = _read_balanced(bbl_source, argument_cursor, opening="{", closing="}")
        if parsed_type is None:
            cursor = argument_cursor
            continue
        entry_type, argument_cursor = parsed_type
        parsed_fields = _read_balanced(
            bbl_source,
            argument_cursor,
            opening="{",
            closing="}",
        )
        if parsed_fields is None:
            cursor = argument_cursor
            continue
        fields_source, end = parsed_fields
        raw_fields = _amsrefs_fields(fields_source)
        persons = tuple(
            _amsrefs_person(raw, role=role, position=position)
            for role in ("author", "editor", "translator")
            for position, raw in enumerate(_field_values(raw_fields, role))
        )
        normalized_type = entry_type.strip().casefold() or "misc"
        items.append(
            {
                "citation_key": citation_key.strip(),
                "entry_type": normalized_type,
                "raw_fields": raw_fields,
                "fields": _clean_amsrefs_fields(raw_fields),
                "persons": persons,
                "formatted_latex": _amsrefs_formatted_latex(
                    entry_type=normalized_type,
                    raw_fields=raw_fields,
                    persons=persons,
                ),
            }
        )
        if len(items) > BIBLIOGRAPHY_MAX_ENTRIES:
            raise BibliographyProcessingError(
                f"Bibliography exceeds the {BIBLIOGRAPHY_MAX_ENTRIES}-entry safety limit."
            )
        cursor = end
    return tuple(items)


def _amsalpha_label(entry: BibliographyEntry) -> str:
    authors = [person for person in entry.persons if person.role == "author"]
    families = ["".join(person.family_names) for person in authors]
    ascii_families = [
        re.sub(
            r"[^A-Za-z]",
            "",
            unicodedata.normalize("NFKD", family).encode("ascii", "ignore").decode(),
        )
        for family in families
    ]
    if len(ascii_families) == 1:
        author_part = ascii_families[0][:3].title()
    elif 2 <= len(ascii_families) <= 3:
        author_part = "".join(family[:1].upper() for family in ascii_families)
    elif ascii_families:
        author_part = "".join(family[:1].upper() for family in ascii_families[:3]) + "+"
    else:
        author_part = re.sub(r"[^A-Za-z]", "", entry.title or "")[:3].title() or "Ref"
    year_match = re.search(r"\d{2}(?!.*\d)", entry.year or "")
    return f"{author_part}{year_match.group(0) if year_match else ''}"


def _parse_compiled_amsrefs_text(value: str) -> tuple[dict[str, str], ...]:
    matches = list(re.finditer(r"(?m)^\s*\[(?P<label>[^\]\r\n]{1,64})\]\s*", value))
    items: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        content = " ".join(value[match.end() : end].replace("\f", " ").split())
        if content:
            items.append({"label_text": match.group("label").strip(), "formatted_text": content})
    return tuple(items)


def _compile_amsrefs_text(
    *,
    bbl_source: str,
    package_options: str,
    latex_command: str,
    pdftotext_command: str,
) -> tuple[tuple[dict[str, str], ...] | None, str | None]:
    safe_options = ",".join(
        option
        for option in (part.strip() for part in package_options.split(","))
        if re.fullmatch(r"[A-Za-z0-9_.=+-]+", option)
    )
    option_source = f"[{safe_options}]" if safe_options else ""
    with tempfile.TemporaryDirectory(prefix="academate-amsrefs-") as temporary:
        workdir = Path(temporary)
        (workdir / "compiled.bbl").write_text(bbl_source, encoding="utf-8")
        (workdir / "bibliography.tex").write_text(
            "\n".join(
                (
                    r"\documentclass{amsart}",
                    r"\usepackage{amssymb}",
                    rf"\usepackage{option_source}{{amsrefs}}",
                    r"\begin{document}",
                    r"\input{compiled.bbl}",
                    r"\end{document}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        latex_result = _run_bounded(
            [
                latex_command,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "bibliography.tex",
            ],
            workdir=workdir,
            timeout_seconds=BIBLIOGRAPHY_TIMEOUT_SECONDS,
        )
        pdf_path = workdir / "bibliography.pdf"
        if latex_result.returncode != 0 or not pdf_path.is_file():
            return None, _bounded_diagnostic(latex_result) or "LaTeX did not produce a PDF."
        text_result = _run_bounded(
            [pdftotext_command, pdf_path.name, "-"],
            workdir=workdir,
            timeout_seconds=BIBLIOGRAPHY_TIMEOUT_SECONDS,
        )
        if text_result.returncode != 0:
            return None, _bounded_diagnostic(text_result) or "pdftotext failed."
        if len(text_result.stdout) > BIBLIOGRAPHY_MAX_OUTPUT_BYTES:
            return None, "Compiled bibliography text exceeded the output size limit."
        items = _parse_compiled_amsrefs_text(_decode_source(text_result.stdout))
        return (items, None) if items else (None, "Compiled bibliography contained no items.")


def _compiled_bibliography_result(
    *,
    sources: list[tuple[str, str]],
    bibliography_index: dict[str, Any],
    requested_style: str | None,
    latex_command: str | None,
    pdftotext_command: str | None,
) -> BibliographyResult | None:
    parsed_entries: list[BibliographyEntry] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
    contains_unstructured_items = False
    for source_path, source in sources:
        amsrefs_items = parse_amsrefs_items(source)
        if amsrefs_items:
            compiled_items: tuple[dict[str, str], ...] = ()
            if latex_command is not None and pdftotext_command is not None:
                rendered, render_error = _compile_amsrefs_text(
                    bbl_source=source,
                    package_options=str(bibliography_index.get("amsrefs_options") or ""),
                    latex_command=latex_command,
                    pdftotext_command=pdftotext_command,
                )
                compiled_items = rendered or ()
                if render_error is not None:
                    warnings.append(
                        "AMSRefs compiler rendering failed; the structured fallback was used: "
                        f"{render_error}"
                    )
            for source_ordinal, item in enumerate(amsrefs_items, start=1):
                citation_key = str(item["citation_key"])
                normalized_key = citation_key.casefold()
                if normalized_key in seen_keys:
                    warnings.append(
                        f"Duplicate compiled bibliography key {citation_key!r}; "
                        "the first item remains canonical."
                    )
                    continue
                seen_keys.add(normalized_key)
                fields = dict(item["fields"])
                raw_fields = item["raw_fields"]
                date = next(iter(_field_values(raw_fields, "date")), "")
                year_match = re.search(r"[+-]?\d{4,}", date)
                persons = tuple(item["persons"])
                compiler_item = (
                    compiled_items[source_ordinal - 1]
                    if source_ordinal <= len(compiled_items)
                    else None
                )
                parsed_entries.append(
                    BibliographyEntry(
                        citation_key=citation_key,
                        entry_type=str(item["entry_type"]),
                        source_path=source_path,
                        raw_bibtex=None,
                        fields=fields,
                        persons=persons,
                        title=_plain_latex(
                            next(iter(_field_values(raw_fields, "title")), "")
                        )
                        or None,
                        container_title=_plain_latex(
                            next(
                                iter(
                                    _field_values(raw_fields, "journal")
                                    or _field_values(raw_fields, "booktitle")
                                ),
                                "",
                            )
                        )
                        or None,
                        year=year_match.group(0) if year_match else (_plain_latex(date) or None),
                        doi=_plain_latex(next(iter(_field_values(raw_fields, "doi")), "")) or None,
                        url=_plain_latex(next(iter(_field_values(raw_fields, "url")), "")) or None,
                        eprint=_plain_latex(
                            next(iter(_field_values(raw_fields, "eprint")), "")
                        )
                        or None,
                        archive_prefix=_plain_latex(
                            next(iter(_field_values(raw_fields, "archiveprefix")), "")
                        )
                        or None,
                        isbn=_plain_latex(
                            next(iter(_field_values(raw_fields, "isbn")), "")
                        )
                        or None,
                        issn=_plain_latex(
                            next(iter(_field_values(raw_fields, "issn")), "")
                        )
                        or None,
                        ordinal=len(parsed_entries) + 1,
                        display_label=(
                            compiler_item.get("label_text") if compiler_item is not None else None
                        ),
                        formatted_latex=(
                            compiler_item.get("formatted_text")
                            if compiler_item is not None
                            else str(item["formatted_latex"])
                        ),
                    )
                )
            continue
        contains_unstructured_items = True
        for item in parse_bbl_items(source):
            citation_key = str(item["citation_key"])
            normalized_key = citation_key.casefold()
            if normalized_key in seen_keys:
                continue
            seen_keys.add(normalized_key)
            parsed_entries.append(
                BibliographyEntry(
                    citation_key=citation_key,
                    entry_type="compiled",
                    source_path=source_path,
                    raw_bibtex=None,
                    fields={
                        "source_kind": "standalone-bbl",
                        "compiled_format": "bibitem",
                        "label_latex": item.get("label_latex"),
                    },
                    persons=(),
                    ordinal=len(parsed_entries) + 1,
                    display_label=item.get("label_text") or str(len(parsed_entries) + 1),
                    citation_author=item.get("citation_author"),
                    citation_year=item.get("citation_year"),
                    formatted_latex=str(item.get("formatted_latex") or "").strip() or None,
                )
            )
    if not parsed_entries:
        return None
    citation_style = str(
        bibliography_index.get("amsrefs_citation_style") or "numeric"
    ).casefold()
    unlabeled_entries = [entry for entry in parsed_entries if not entry.display_label]
    if citation_style in {"alphabetic", "shortalphabetic"} and unlabeled_entries:
        base_labels = [_amsalpha_label(entry) for entry in parsed_entries]
        counts = {label: base_labels.count(label) for label in set(base_labels)}
        collision_positions: dict[str, int] = {}
        labeled_entries: list[BibliographyEntry] = []
        for entry, label in zip(parsed_entries, base_labels, strict=True):
            position = collision_positions.get(label, 0)
            labeled_entries.append(
                replace(
                    entry,
                    display_label=(
                        label if counts[label] == 1 else f"{label}{chr(ord('a') + position)}"
                    ),
                )
            )
            collision_positions[label] = position + 1
        parsed_entries = labeled_entries
    elif citation_style == "author-year" and unlabeled_entries:
        labeled_entries = []
        for entry in parsed_entries:
            citation_author = (
                " and ".join(
                    " ".join(person.family_names)
                    for person in entry.persons
                    if person.role == "author"
                )
                or None
            )
            labeled_entries.append(
                replace(
                    entry,
                    display_label=(
                        entry.display_label
                        or _compact_author_year_label(citation_author, entry.year)
                    ),
                    citation_author=citation_author,
                    citation_year=entry.year,
                )
            )
        parsed_entries = labeled_entries
    else:
        parsed_entries = [
            replace(entry, display_label=entry.display_label or str(entry.ordinal))
            for entry in parsed_entries
        ]
    citation_keys = tuple(
        dict.fromkeys(
            str(key).strip()
            for key in bibliography_index.get("citation_keys", [])
            if isinstance(key, str) and key.strip() and key.strip() != "*"
        )
    )
    missing = [key for key in citation_keys if key.casefold() not in seen_keys]
    warnings.extend(f"Citation key {key!r} has no compiled bibliography item." for key in missing)
    warnings.append(
        "Bibliography data was recovered from the submission's compiler-produced BBL file."
    )
    if contains_unstructured_items:
        warnings.append(
            "Standard bibitems preserve citation identity and presentation, but fields absent "
            "from the compiled output were not guessed."
        )
    return BibliographyResult(
        status="PARTIAL" if missing or contains_unstructured_items else "READY",
        requested_style=requested_style,
        resolved_style=requested_style or "author-supplied-bbl",
        fallback_used=False,
        source_files=tuple(path for path, _source in sources),
        entries=tuple(parsed_entries),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _inline_bibliography_result(
    *,
    bibliography_index: dict[str, Any],
    requested_style: str | None,
) -> BibliographyResult | None:
    inline_sources = [
        source
        for source in bibliography_index.get("inline_sources", [])
        if isinstance(source, dict)
        and isinstance(source.get("latex"), str)
        and str(source["latex"]).strip()
    ]
    if not inline_sources:
        return None
    total_bytes = sum(
        len(str(source["latex"]).encode("utf-8", errors="replace"))
        for source in inline_sources
    )
    if total_bytes > BIBLIOGRAPHY_MAX_SOURCE_BYTES:
        raise BibliographyProcessingError(
            "Inline bibliography exceeds the bounded parser input limit."
        )
    parsed_items: list[tuple[str, dict[str, Any]]] = []
    seen_keys: set[str] = set()
    warnings: list[str] = []
    for source in inline_sources:
        source_path = str(source.get("source_path") or "inline-thebibliography")
        for item in parse_bbl_items(str(source["latex"])):
            citation_key = str(item.get("citation_key") or "").strip()
            normalized_key = citation_key.casefold()
            if not citation_key:
                continue
            if normalized_key in seen_keys:
                warnings.append(
                    f"Duplicate inline bibliography key {citation_key!r}; "
                    "the first item remains canonical."
                )
                continue
            if len(parsed_items) >= BIBLIOGRAPHY_MAX_ENTRIES:
                raise BibliographyProcessingError(
                    f"Bibliography exceeds the {BIBLIOGRAPHY_MAX_ENTRIES}-entry safety limit."
                )
            seen_keys.add(normalized_key)
            parsed_items.append((source_path, item))
    if not parsed_items:
        return None
    entries: list[BibliographyEntry] = []
    for ordinal, (source_path, item) in enumerate(parsed_items, start=1):
        citation_author = item.get("citation_author")
        citation_year = item.get("citation_year")
        label_text = item.get("label_text")
        entries.append(
            BibliographyEntry(
                citation_key=str(item["citation_key"]),
                # A compiled bibitem no longer contains enough information to recover the
                # original BibTeX type safely.  The explicit provenance marker is preferable
                # to fabricating article/book fields that could poison a future citation graph.
                entry_type="compiled",
                source_path=source_path,
                raw_bibtex=None,
                fields={
                    "source_kind": "inline-thebibliography",
                    "label_latex": item.get("label_latex"),
                },
                persons=(),
                ordinal=ordinal,
                display_label=str(
                    label_text
                    or _compact_author_year_label(citation_author, citation_year)
                    or ordinal
                ),
                citation_author=(str(citation_author) if citation_author else None),
                citation_year=(str(citation_year) if citation_year else None),
                formatted_latex=str(item.get("formatted_latex") or "").strip() or None,
            )
        )
    citation_keys = tuple(
        dict.fromkeys(
            str(key).strip()
            for key in bibliography_index.get("citation_keys", [])
            if isinstance(key, str) and key.strip() and key.strip() != "*"
        )
    )
    missing = [key for key in citation_keys if key.casefold() not in seen_keys]
    warnings.extend(
        f"Citation key {key!r} has no inline bibliography item." for key in missing
    )
    warnings.append(
        "The submission contains compiler-produced bibitems but no BibTeX database; "
        "citation keys and formatted entries were preserved without guessing absent fields."
    )
    return BibliographyResult(
        status="PARTIAL" if missing or entries else "EMPTY",
        requested_style=requested_style,
        resolved_style=requested_style or "author-supplied-thebibliography",
        fallback_used=False,
        source_files=tuple(dict.fromkeys(path for path, _item in parsed_items)),
        entries=tuple(entries),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _run_bounded(
    command: list[str],
    *,
    workdir: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "BIBINPUTS": f"{workdir}//:",
            "BSTINPUTS": f"{workdir}//:",
            "openin_any": "p",
            "openout_any": "p",
            "shell_escape": "f",
            "SOURCE_DATE_EPOCH": "0",
        }
    )
    try:
        return subprocess.run(
            command,
            cwd=workdir,
            env=environment,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BibliographyProcessingError(f"Bibliography tool failed to run: {error}") from error


def _bounded_diagnostic(result: subprocess.CompletedProcess[bytes]) -> str:
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"\b(?:ERROR|FATAL)\b", line)]
    tail = lines[-24:]
    diagnostic = " ".join(dict.fromkeys((*errors, *tail)))
    return diagnostic[-6000:]


def _write_staged_file(workdir: Path, relative_path: str, content: bytes) -> None:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BibliographyProcessingError(f"Unsafe bibliography project path: {relative_path!r}")
    destination = workdir.joinpath(*path.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _compile_bbl(
    *,
    workdir: Path,
    merged_bib: str,
    citation_keys: tuple[str, ...],
    nocite_all: bool,
    style: str,
    bibtex_command: str,
) -> tuple[str | None, str | None]:
    style_path = PurePosixPath(style)
    if (
        not _SAFE_STYLE_RE.fullmatch(style)
        or style_path.is_absolute()
        or ".." in style_path.parts
    ):
        return None, f"Unsafe bibliography style name: {style!r}."
    normalized_style = style.removesuffix(".bst")
    (workdir / "merged.bib").write_text(merged_bib, encoding="utf-8")
    citations = "*" if nocite_all else ",".join(citation_keys)
    aux_lines = [r"\relax"]
    if citations:
        aux_lines.append(f"\\citation{{{citations}}}")
    aux_lines.extend((f"\\bibstyle{{{normalized_style}}}", r"\bibdata{merged}"))
    (workdir / "bibliography.aux").write_text("\n".join(aux_lines) + "\n", encoding="utf-8")
    result = _run_bounded(
        [bibtex_command, "bibliography"],
        workdir=workdir,
        timeout_seconds=BIBLIOGRAPHY_TIMEOUT_SECONDS,
    )
    bbl_path = workdir / "bibliography.bbl"
    if result.returncode != 0 or not bbl_path.is_file():
        return None, _bounded_diagnostic(result) or "BibTeX did not produce a BBL file."
    content = bbl_path.read_bytes()
    if len(content) > BIBLIOGRAPHY_MAX_OUTPUT_BYTES:
        return None, "BibTeX output exceeded the bibliography size limit."
    return _decode_source(content), None


def _fallback_entry_latex(entry: BibliographyEntry) -> str:
    authors = [person.display_name for person in entry.persons if person.role == "author"]
    parts = [", ".join(authors), entry.title, entry.container_title, entry.year]
    return ". ".join(part.rstrip(".") for part in parts if part) + "."


def materialize_citation_segment(
    segment: dict[str, Any],
    *,
    entries_by_key: dict[str, tuple[str, BibliographyEntry]],
) -> dict[str, Any]:
    result = dict(segment)
    items: list[dict[str, Any]] = []
    for key in segment.get("keys", []):
        if not isinstance(key, str) or not key.strip():
            continue
        match = entries_by_key.get(key.casefold())
        if match is None:
            items.append(
                {
                    "citation_key": key,
                    "status": "UNRESOLVED",
                    "text": "?",
                }
            )
            continue
        entry_id, entry = match
        author_year = bool(entry.citation_author and entry.citation_year)
        label = (
            f"{entry.citation_author}, {entry.citation_year}"
            if author_year
            else entry.display_label or str(entry.ordinal or "?")
        )
        items.append(
            {
                "citation_key": entry.citation_key,
                "entry_id": entry_id,
                "status": "RESOLVED" if entry.ordinal is not None else "NOT_PRINTED",
                "label": label,
                "author": entry.citation_author,
                "year": entry.citation_year or entry.year,
                "text": label,
            }
        )
    command = str(segment.get("command") or "cite").casefold()
    prefix = str(segment.get("prefix") or "").strip()
    suffix = str(segment.get("suffix") or "").strip()
    author_year_style = bool(items) and all(
        item.get("author") and item.get("year") for item in items
    )
    pieces: list[str] = []
    for item in items:
        author = str(item.get("author") or "?")
        year = str(item.get("year") or "?")
        if "author" in command:
            pieces.append(author)
        elif "year" in command:
            pieces.append(year)
        elif author_year_style and command in {"cite", "citet", "textcite"}:
            pieces.append(f"{author} ({year})")
        elif author_year_style and command == "citealt":
            pieces.append(f"{author} {year}")
        elif author_year_style:
            pieces.append(f"{author}, {year}")
        else:
            pieces.append(str(item.get("label") or item.get("text") or "?"))
    separator = "; " if author_year_style else ", "
    core = separator.join(pieces) or "?"
    if prefix:
        core = f"{prefix} {core}"
    if suffix:
        core = f"{core}, {suffix}"
    if author_year_style:
        textual_commands = {
            "cite",
            "citealp",
            "citealt",
            "citeauthor",
            "citeyear",
            "citet",
            "textcite",
        }
        text = core if command in textual_commands else f"({core})"
    else:
        text = core if command in {"citeauthor", "citeyear", "supercite"} else f"[{core}]"
    statuses = {str(item.get("status")) for item in items}
    status = (
        "RESOLVED"
        if statuses == {"RESOLVED"}
        else "PARTIAL"
        if "RESOLVED" in statuses
        else "UNRESOLVED"
    )
    result.update(
        {
            "status": status,
            "style": "author-year" if author_year_style else "label",
            "separator": separator,
            "items": items,
            "text": text,
        }
    )
    return result


def process_bibliography(
    *,
    manifest: dict[str, Any],
    storage: Storage,
    bibliography_index: dict[str, Any],
    biber_command: str | None = None,
    bibtex_command: str | None = None,
    latex_command: str | None = None,
    pdftotext_command: str | None = None,
) -> BibliographyResult:
    manifest_items = {
        str(item.get("path", "")): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("path") and item.get("storage_key")
    }
    source_paths = tuple(
        dict.fromkeys(
            str(path)
            for path in bibliography_index.get("source_files", [])
            if isinstance(path, str) and path
        )
    )
    requested_style = (
        str(bibliography_index.get("requested_style") or "").strip() or None
    )
    compiled_source_paths = tuple(
        dict.fromkeys(
            str(path)
            for path in bibliography_index.get("compiled_source_files", [])
            if isinstance(path, str) and path
        )
    )
    compiled_sources: list[tuple[str, str]] = []
    compiled_bytes = 0
    for source_path in compiled_source_paths:
        item = manifest_items.get(source_path)
        if item is None:
            continue
        with storage.get(str(item["storage_key"])) as stream:
            content = stream.read(BIBLIOGRAPHY_MAX_SOURCE_BYTES + 1)
        compiled_bytes += len(content)
        if compiled_bytes > BIBLIOGRAPHY_MAX_SOURCE_BYTES:
            raise BibliographyProcessingError(
                "Compiled bibliography sources exceed the bounded parser input limit."
            )
        compiled_sources.append((source_path, _decode_source(content)))
    compiled_result = _compiled_bibliography_result(
        sources=compiled_sources,
        bibliography_index=bibliography_index,
        requested_style=requested_style,
        latex_command=latex_command or shutil.which("pdflatex"),
        pdftotext_command=pdftotext_command or shutil.which("pdftotext"),
    )
    if not source_paths:
        inline_result = _inline_bibliography_result(
            bibliography_index=bibliography_index,
            requested_style=requested_style,
        )
        if inline_result is not None:
            return inline_result
        if compiled_result is not None:
            return compiled_result
        return BibliographyResult(
            status="EMPTY",
            requested_style=requested_style,
            resolved_style=None,
            fallback_used=False,
            source_files=(),
            entries=(),
            warnings=(),
        )
    sources: list[tuple[str, str]] = []
    total_bytes = 0
    for source_path in source_paths:
        item = manifest_items.get(source_path)
        if item is None:
            continue
        with storage.get(str(item["storage_key"])) as stream:
            content = stream.read(BIBLIOGRAPHY_MAX_SOURCE_BYTES + 1)
        total_bytes += len(content)
        if total_bytes > BIBLIOGRAPHY_MAX_SOURCE_BYTES:
            raise BibliographyProcessingError(
                "Bibliography sources exceed the bounded parser input limit."
            )
        sources.append((source_path, _decode_source(content)))
    if not sources:
        raise BibliographyProcessingError("Declared bibliography data files are unavailable.")
    compiler_sources, duplicate_key_count = _deduplicate_bibtex_sources_for_compilers(
        sources
    )
    merged_bib = "\n\n".join(source for _path, source in compiler_sources)
    biber_staging_bib, biber_key_aliases, aliased_key_count = _biber_staging_bibliography(
        compiler_sources
    )
    raw_entries, warnings = _raw_entries_by_key(sources)
    if duplicate_key_count:
        warnings.append(
            f"Removed {duplicate_key_count} duplicate BibTeX entr"
            f"{'y' if duplicate_key_count == 1 else 'ies'} from the temporary compiler view; "
            "the first declaration for each case-insensitive key remains canonical."
        )
    if aliased_key_count:
        warnings.append(
            f"Applied a reversible Biber-safe staging alias to {aliased_key_count} "
            "BibTeX identifier(s); canonical citation keys remain unchanged."
        )
    resolved_biber = biber_command or shutil.which("biber")
    resolved_bibtex = bibtex_command or shutil.which("bibtex")
    if resolved_biber is None or resolved_bibtex is None:
        raise BibliographyProcessingError(
            "The worker bibliography runtime requires both Biber and BibTeX."
        )
    with tempfile.TemporaryDirectory(prefix="academate-bibliography-") as temporary:
        workdir = Path(temporary)
        for path, item in manifest_items.items():
            if PurePosixPath(path).suffix.casefold() != ".bst":
                continue
            with storage.get(str(item["storage_key"])) as stream:
                _write_staged_file(workdir, path, stream.read(BIBLIOGRAPHY_MAX_SOURCE_BYTES + 1))
        merged_path = workdir / "merged.bib"
        merged_path.write_text(biber_staging_bib, encoding="utf-8")
        xml_path = workdir / "structured.xml"
        biber_result = _run_bounded(
            [
                resolved_biber,
                "--tool",
                f"--output-directory={workdir}",
                "--output-format=biblatexml",
                "--output-file=structured.xml",
                merged_path.name,
            ],
            workdir=workdir,
            timeout_seconds=BIBLIOGRAPHY_TIMEOUT_SECONDS,
        )
        if biber_result.returncode != 0 or not xml_path.is_file():
            if compiled_result is not None:
                return replace(
                    compiled_result,
                    source_files=tuple(
                        dict.fromkeys((*source_paths, *compiled_result.source_files))
                    ),
                    warnings=tuple(
                        dict.fromkeys(
                            (
                                "Biber could not structure the BibTeX database; the "
                                "submission's compiler-produced BBL preserved citation "
                                "identity and presentation: "
                                + _bounded_diagnostic(biber_result),
                                *warnings,
                                *compiled_result.warnings,
                            )
                        )
                    ),
                )
            raise BibliographyProcessingError(
                "Biber could not structure the bibliography: " + _bounded_diagnostic(biber_result)
            )
        xml_bytes = xml_path.read_bytes()
        if len(xml_bytes) > BIBLIOGRAPHY_MAX_OUTPUT_BYTES:
            raise BibliographyProcessingError("Biber XML output exceeded the size limit.")
        entries = parse_biblatexml(
            _decode_source(xml_bytes),
            raw_entries=raw_entries,
            citation_key_aliases=biber_key_aliases,
        )
        backend = str(bibliography_index.get("backend") or "bibtex")
        fallback_used = backend != "bibtex" or requested_style is None
        style_candidates = (
            (requested_style, BIBLIOGRAPHY_FALLBACK_STYLE)
            if requested_style
            and backend == "bibtex"
            and requested_style != BIBLIOGRAPHY_FALLBACK_STYLE
            else (BIBLIOGRAPHY_FALLBACK_STYLE,)
        )
        if backend != "bibtex" and requested_style:
            warnings.append(
                f"The {requested_style!r} BibLaTeX style cannot be executed as a BST program; "
                f"the bibliography uses {BIBLIOGRAPHY_FALLBACK_STYLE}."
            )
        citation_keys = tuple(
            dict.fromkeys(
                str(key).strip()
                for key in bibliography_index.get("citation_keys", [])
                if isinstance(key, str) and key.strip() and key.strip() != "*"
            )
        )
        nocite_all = bool(bibliography_index.get("nocite_all"))
        resolved_style: str | None = None
        inline_result = _inline_bibliography_result(
            bibliography_index=bibliography_index,
            requested_style=requested_style,
        )
        if inline_result is not None:
            # If both forms are present, Biber remains authoritative for structured fields,
            # while the paper's already-compiled BBL remains authoritative for presentation.
            bbl_items = tuple(
                {
                    "citation_key": entry.citation_key,
                    "label_latex": entry.fields.get("label_latex"),
                    "label_text": entry.display_label,
                    "citation_author": entry.citation_author,
                    "citation_year": entry.citation_year,
                    "formatted_latex": entry.formatted_latex,
                }
                for entry in inline_result.entries
            )
            resolved_style = requested_style or "author-supplied-thebibliography"
            fallback_used = False
        elif compiled_result is not None:
            # The author's BBL is the exact output of the submission's own BibTeX/style
            # workflow. Biber still supplies structured fields, while this compiler-produced
            # artifact remains authoritative for labels, ordering, and formatted prose.
            bbl_items = tuple(
                {
                    "citation_key": entry.citation_key,
                    "label_latex": entry.fields.get("label_latex"),
                    "label_text": entry.display_label,
                    "citation_author": entry.citation_author,
                    "citation_year": entry.citation_year,
                    "formatted_latex": entry.formatted_latex,
                }
                for entry in compiled_result.entries
            )
            resolved_style = requested_style or compiled_result.resolved_style
            fallback_used = False
            warnings.extend(
                warning
                for warning in compiled_result.warnings
                if warning.startswith(("Citation key", "Duplicate"))
            )
            warnings.append(
                "Bibliography labels, order, and presentation were reused from the "
                "submission's compiler-produced BBL file."
            )
        else:
            bbl_source: str | None = None
            for candidate_index, style in enumerate(style_candidates):
                if style is None:
                    continue
                bbl_source, error = _compile_bbl(
                    workdir=workdir,
                    merged_bib=merged_bib,
                    citation_keys=citation_keys,
                    nocite_all=nocite_all,
                    style=style,
                    bibtex_command=resolved_bibtex,
                )
                if bbl_source is not None:
                    resolved_style = style.removesuffix(".bst")
                    fallback_used = fallback_used or candidate_index > 0
                    break
                warnings.append(f"BibTeX style {style!r} failed: {error}")
            bbl_items = parse_bbl_items(bbl_source or "")

    by_key: dict[str, BibliographyEntry] = {}
    structured_order: list[str] = []
    for entry in entries:
        normalized_key = entry.citation_key.casefold()
        if normalized_key in by_key:
            warnings.append(
                f"Duplicate structured bibliography key {entry.citation_key!r}; "
                "the first entry remains canonical."
            )
            continue
        by_key[normalized_key] = entry
        structured_order.append(normalized_key)
    ordered_keys: set[str] = set()
    for ordinal, item in enumerate(bbl_items, start=1):
        normalized_key = str(item["citation_key"]).casefold()
        entry = by_key.get(normalized_key)
        if entry is None:
            warnings.append(
                f"Formatted bibliography item {item['citation_key']!r} has no structured entry."
            )
            continue
        ordered_keys.add(normalized_key)
        label_text = item.get("label_text")
        citation_author = item.get("citation_author")
        citation_year = item.get("citation_year")
        display_label = (
            label_text
            or _compact_author_year_label(citation_author, citation_year)
            or str(ordinal)
        )
        by_key[normalized_key] = replace(
            entry,
            ordinal=ordinal,
            display_label=display_label,
            citation_author=citation_author,
            citation_year=citation_year,
            formatted_latex=str(item.get("formatted_latex") or "").strip() or None,
        )
    if resolved_style is None:
        fallback_order = list(by_key) if nocite_all else [key.casefold() for key in citation_keys]
        for ordinal, normalized_key in enumerate(dict.fromkeys(fallback_order), start=1):
            entry = by_key.get(normalized_key)
            if entry is None:
                continue
            ordered_keys.add(normalized_key)
            by_key[normalized_key] = replace(
                entry,
                ordinal=ordinal,
                display_label=str(ordinal),
                formatted_latex=_fallback_entry_latex(entry),
            )
    final_entries = tuple(by_key[normalized_key] for normalized_key in structured_order)
    cited_missing = [key for key in citation_keys if key.casefold() not in by_key]
    warnings.extend(f"Citation key {key!r} has no BibTeX entry." for key in cited_missing)
    status = "READY" if resolved_style is not None and not cited_missing else "PARTIAL"
    digest = hashlib.sha256(merged_bib.encode("utf-8")).hexdigest()[:12]
    if warnings:
        warnings.insert(0, f"Bibliography source digest: {digest}.")
    return BibliographyResult(
        status=status,
        requested_style=requested_style,
        resolved_style=resolved_style,
        fallback_used=fallback_used,
        source_files=tuple(path for path, _source in sources),
        entries=final_entries,
        warnings=tuple(dict.fromkeys(warnings)),
    )
