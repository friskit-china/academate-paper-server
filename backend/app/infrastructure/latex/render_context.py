from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal

_CONTROL_SEQUENCE_RE = re.compile(r"\\([A-Za-z@]+|.)")
_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{([^{}]+)\}")
_FORBIDDEN_CONTEXT_RE = re.compile(
    r"\\(write18|write|openin|openout|read|catcode|loop|csname)\b",
    re.IGNORECASE,
)

# A block renderer needs a typesetting shell, not the paper's front matter and
# publication workflow.  Known distribution classes have predictable document
# hooks; a project-local or publisher-specific class may execute title/author
# hooks, require companion files, or emit unrelated content at begin/end document.
# The complete source preamble remains a later deterministic attempt, so this
# allowlist only governs the dependency-focused isolation tier.
_ISOLATED_DOCUMENT_CLASSES = frozenset(
    {
        "article",
        "report",
        "book",
        "letter",
        "minimal",
        "proc",
        "amsart",
        "amsbook",
        "amsproc",
        "scrartcl",
        "scrreprt",
        "scrbook",
        "extarticle",
        "extreport",
        "extbook",
        "revtex4",
        "revtex4-1",
        "revtex4-2",
        "IEEEtran",
        "elsarticle",
    }
)
_DOCUMENT_CLASS_NAME_RE = re.compile(
    r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{\s*([^{}]+?)\s*\}",
    re.DOTALL,
)
_TEX_DISTRIBUTION_SHADOW_SUFFIXES = frozenset(
    {".sty", ".cls", ".clo", ".def", ".ltx", ".fd", ".cfg"}
)


@lru_cache(maxsize=512)
def _tex_distribution_resolves(filename: str, kpsewhich_command: str) -> bool:
    """Return whether TeX Live owns a top-level dependency with this exact basename."""

    command = shutil.which(kpsewhich_command)
    if command is None or PurePosixPath(filename).name != filename:
        return False
    try:
        result = subprocess.run(
            [command, filename],
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def without_tex_distribution_shadows(
    assets: Mapping[str, bytes],
    *,
    kpsewhich_command: str = "kpsewhich",
) -> dict[str, bytes]:
    """Keep private paper assets while removing top-level files already owned by TeX Live.

    The exact paper-local asset set remains the first render tier. This reduced set is for a
    deterministic compatibility retry when an old vendored class/package shadows the current
    arXiv-compatible TeX distribution. Explicit subdirectory paths are retained because the
    author deliberately addressed those files rather than relying on TeX's package lookup.
    """

    return {
        path: content
        for path, content in assets.items()
        if not (
            len(PurePosixPath(path).parts) == 1
            and PurePosixPath(path).suffix.lower() in _TEX_DISTRIBUTION_SHADOW_SUFFIXES
            and _tex_distribution_resolves(PurePosixPath(path).name, kpsewhich_command)
        )
    }


def contains_unescaped_tex_command(value: str, pattern: re.Pattern[str]) -> bool:
    r"""Match a TeX control sequence only when its backslash is not part of ``\\``.

    Regex engines otherwise see the second character of ``\\input`` as ``\input`` even though
    TeX tokenizes it as the line-break control symbol followed by plain text. An odd-length run
    still contains a real control word (for example ``\\\\\input``), so it remains forbidden.
    """

    for match in pattern.finditer(value):
        slash_count = 1
        cursor = match.start() - 1
        while cursor >= 0 and value[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        if slash_count % 2 == 1:
            return True
    return False


_DECLARATION_COMMANDS = {
    "DeclareGraphicsExtensions",
    "DeclareDocumentCommand",
    "DeclareMathAlphabet",
    "DeclareMathOperator",
    "DeclarePairedDelimiter",
    "DeclarePairedDelimiterX",
    "DeclareRobustCommand",
    "PassOptionsToPackage",
    "RequirePackage",
    "addtolength",
    "begin",
    "colorlet",
    "definecolor",
    "def",
    "documentclass",
    "edef",
    "end",
    "gdef",
    "graphicspath",
    "let",
    "makeatletter",
    "makeatother",
    "newcolumntype",
    "newcommand",
    "NewDocumentCommand",
    "newenvironment",
    "newlength",
    "newsavebox",
    "providecolor",
    "providecommand",
    "ProvideDocumentCommand",
    "renewcommand",
    "renewenvironment",
    "RenewDocumentCommand",
    "setlength",
    "tikzset",
    "tikzstyle",
    "usepackage",
    "usetikzlibrary",
    "xdef",
}


@dataclass(frozen=True, slots=True)
class PreambleUnit:
    start: int
    end: int
    kind: str
    source: str
    provides: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    package_options: str = ""
    unsafe_commands: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        names = self.provides or self.packages
        suffix = ",".join(names[:4])
        return f"{self.kind}:{suffix}" if suffix else self.kind


@dataclass(frozen=True, slots=True)
class RenderContext:
    preamble: str
    context_hash: str
    declared_packages: tuple[str, ...]
    included_units: tuple[str, ...]
    skipped_unsafe_units: tuple[str, ...]
    skipped_unsafe_commands: tuple[str, ...]
    skipped_missing_packages: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "strategy": "focused-paper-context",
            "context_hash": self.context_hash,
            "declared_packages": list(self.declared_packages),
            "included_units": list(self.included_units),
            "skipped_unsafe_units": list(self.skipped_unsafe_units),
            "skipped_unsafe_commands": list(self.skipped_unsafe_commands),
            "skipped_missing_packages": list(self.skipped_missing_packages),
        }


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _mask_comments(value: str) -> str:
    output = list(value)
    cursor = 0
    while cursor < len(value):
        if value[cursor] == "%" and not _is_escaped(value, cursor):
            end = value.find("\n", cursor + 1)
            if end == -1:
                end = len(value)
            for index in range(cursor, end):
                output[index] = " "
            cursor = end
            continue
        cursor += 1
    return "".join(output)


def _skip_space(value: str, cursor: int) -> int:
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    return cursor


def _read_balanced(
    value: str,
    cursor: int,
    opening: Literal["{", "["],
    closing: Literal["}", "]"],
) -> tuple[str, int] | None:
    cursor = _skip_space(value, cursor)
    if cursor >= len(value) or value[cursor] != opening:
        return None
    depth = 0
    for index in range(cursor, len(value)):
        character = value[index]
        if _is_escaped(value, index):
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return value[cursor + 1 : index], index + 1
    return None


def _read_optional(value: str, cursor: int) -> tuple[str | None, int]:
    cursor = _skip_space(value, cursor)
    parsed = _read_balanced(value, cursor, "[", "]")
    if parsed is None:
        return None, cursor
    return parsed


def _read_required(value: str, cursor: int) -> tuple[str, int] | None:
    return _read_balanced(value, cursor, "{", "}")


def _read_control_target(value: str, cursor: int) -> tuple[str, int] | None:
    cursor = _skip_space(value, cursor)
    braced = _read_required(value, cursor)
    if braced is not None:
        target, end = braced
        match = _CONTROL_SEQUENCE_RE.search(target)
        return (match.group(1), end) if match is not None else None
    match = _CONTROL_SEQUENCE_RE.match(value, cursor)
    if match is None:
        return None
    return match.group(1), match.end()


def _references(source: str, *, provides: tuple[str, ...] = ()) -> tuple[str, ...]:
    references = {match.group(1) for match in _CONTROL_SEQUENCE_RE.finditer(source)}
    references.update(match.group(1).strip() for match in _ENVIRONMENT_RE.finditer(source))
    references.difference_update(_DECLARATION_COMMANDS)
    references.difference_update(provides)
    return tuple(sorted(references))


def _unsafe_commands(source: str) -> tuple[str, ...]:
    return tuple(
        sorted({match.group(1).lower() for match in _FORBIDDEN_CONTEXT_RE.finditer(source)})
    )


def _unit(
    preamble: str,
    *,
    start: int,
    end: int,
    kind: str,
    provides: tuple[str, ...] = (),
    packages: tuple[str, ...] = (),
    package_options: str = "",
) -> PreambleUnit:
    source = preamble[start:end]
    return PreambleUnit(
        start=start,
        end=end,
        kind=kind,
        source=source,
        provides=provides,
        references=_references(source, provides=provides),
        packages=packages,
        package_options=package_options,
        unsafe_commands=_unsafe_commands(source),
    )


def _parse_package_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    if command == "PassOptionsToPackage":
        options = _read_required(masked, cursor)
        if options is None:
            return None
        package_group = _read_required(masked, options[1])
        if package_group is None:
            return None
        packages = tuple(item.strip() for item in package_group[0].split(",") if item.strip())
        return _unit(
            preamble,
            start=start,
            end=package_group[1],
            kind=command,
            packages=packages,
            package_options=options[0],
        )
    options, cursor = _read_optional(masked, cursor)
    package_group = _read_required(masked, cursor)
    if package_group is None:
        return None
    packages = tuple(item.strip() for item in package_group[0].split(",") if item.strip())
    return _unit(
        preamble,
        start=start,
        end=package_group[1],
        kind=command,
        packages=packages,
        package_options=options or "",
    )


def _parse_macro_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    cursor = _skip_space(masked, cursor)
    if cursor < len(masked) and masked[cursor] == "*":
        cursor += 1
    target = _read_control_target(masked, cursor)
    if target is None:
        return None
    name, cursor = target
    _argument_count, cursor = _read_optional(masked, cursor)
    _default, cursor = _read_optional(masked, cursor)
    body = _read_required(masked, cursor)
    if body is None:
        return None
    return _unit(
        preamble,
        start=start,
        end=body[1],
        kind=command,
        provides=(name,),
    )


def _parse_xparse_macro_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    cursor = _skip_space(masked, cursor)
    if cursor < len(masked) and masked[cursor] == "*":
        cursor += 1
    target = _read_control_target(masked, cursor)
    if target is None:
        return None
    name, cursor = target
    argument_specification = _read_required(masked, cursor)
    if argument_specification is None:
        return None
    body = _read_required(masked, argument_specification[1])
    if body is None:
        return None
    return _unit(
        preamble,
        start=start,
        end=body[1],
        kind=command,
        provides=(name,),
    )


def _parse_paired_delimiter_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    target = _read_control_target(masked, cursor)
    if target is None:
        return None
    name, cursor = target
    if command == "DeclarePairedDelimiterX":
        _argument_count, cursor = _read_optional(masked, cursor)
    group_count = 3 if command == "DeclarePairedDelimiterX" else 2
    end = cursor
    for _ in range(group_count):
        parsed = _read_required(masked, end)
        if parsed is None:
            return None
        end = parsed[1]
    return _unit(
        preamble,
        start=start,
        end=end,
        kind=command,
        provides=(name,),
    )


def _parse_environment_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    environment = _read_required(masked, cursor)
    if environment is None:
        return None
    name = environment[0].strip()
    _argument_count, cursor = _read_optional(masked, environment[1])
    _default, cursor = _read_optional(masked, cursor)
    begin_body = _read_required(masked, cursor)
    if begin_body is None:
        return None
    end_body = _read_required(masked, begin_body[1])
    if end_body is None:
        return None
    return _unit(
        preamble,
        start=start,
        end=end_body[1],
        kind=command,
        provides=(name,),
    )


def _parse_def_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
) -> PreambleUnit | None:
    target = _read_control_target(masked, cursor)
    if target is None:
        return None
    name, cursor = target
    body_start = masked.find("{", cursor)
    if body_start == -1:
        return None
    body = _read_required(masked, body_start)
    if body is None:
        return None
    return _unit(
        preamble,
        start=start,
        end=body[1],
        kind=command,
        provides=(name,),
    )


def _parse_fixed_groups_unit(
    preamble: str,
    masked: str,
    *,
    start: int,
    command: str,
    cursor: int,
    groups: int,
    provides_from: int | None = None,
    control_target: bool = False,
) -> PreambleUnit | None:
    values: list[str] = []
    end = cursor
    if control_target:
        target = _read_control_target(masked, end)
        if target is None:
            return None
        values.append(target[0])
        end = target[1]
        groups -= 1
    for _ in range(groups):
        parsed = _read_required(masked, end)
        if parsed is None:
            return None
        values.append(parsed[0])
        end = parsed[1]
    provides = ()
    if provides_from is not None and provides_from < len(values):
        provided = values[provides_from].strip()
        match = _CONTROL_SEQUENCE_RE.search(provided)
        provides = (match.group(1),) if match is not None else (provided,)
    return _unit(
        preamble,
        start=start,
        end=end,
        kind=command,
        provides=provides,
    )


def extract_preamble_units(preamble: str) -> tuple[PreambleUnit, ...]:
    """Extract bounded, independently selectable declarations from a paper preamble."""

    masked = _mask_comments(preamble)
    units: list[PreambleUnit] = []
    cursor = 0
    brace_depth = 0
    group_depth = 0
    while cursor < len(masked):
        if masked.startswith(r"\begingroup", cursor):
            group_depth += 1
            cursor += len(r"\begingroup")
            continue
        if masked.startswith(r"\endgroup", cursor):
            group_depth = max(0, group_depth - 1)
            cursor += len(r"\endgroup")
            continue
        character = masked[cursor]
        if character == "{" and not _is_escaped(masked, cursor):
            brace_depth += 1
            cursor += 1
            continue
        if character == "}" and not _is_escaped(masked, cursor):
            brace_depth = max(0, brace_depth - 1)
            cursor += 1
            continue
        command_match = _CONTROL_SEQUENCE_RE.match(masked, cursor)
        if command_match is None or brace_depth or group_depth:
            cursor += 1
            continue
        start = cursor
        command = command_match.group(1)
        after_command = command_match.end()
        parsed: PreambleUnit | None = None
        if command == "documentclass":
            _options, required_cursor = _read_optional(masked, after_command)
            document_class = _read_required(masked, required_cursor)
            if document_class is not None:
                parsed = _unit(
                    preamble,
                    start=start,
                    end=document_class[1],
                    kind=command,
                )
        elif command in {"usepackage", "RequirePackage", "PassOptionsToPackage"}:
            parsed = _parse_package_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {
            "newcommand",
            "renewcommand",
            "providecommand",
            "DeclareRobustCommand",
        }:
            parsed = _parse_macro_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {
            "NewDocumentCommand",
            "RenewDocumentCommand",
            "ProvideDocumentCommand",
            "DeclareDocumentCommand",
        }:
            parsed = _parse_xparse_macro_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {"DeclarePairedDelimiter", "DeclarePairedDelimiterX"}:
            parsed = _parse_paired_delimiter_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {"newenvironment", "renewenvironment"}:
            parsed = _parse_environment_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {"def", "gdef", "edef", "xdef"}:
            parsed = _parse_def_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
            )
        elif command in {"newsavebox", "newlength"}:
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
                groups=1,
                provides_from=0,
            )
        elif command in {"setlength", "addtolength"}:
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
                groups=2,
                provides_from=0,
            )
        elif command in {"definecolor", "providecolor"}:
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
                groups=3,
                provides_from=0,
            )
        elif command == "colorlet":
            _options, required_cursor = _read_optional(masked, after_command)
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=required_cursor,
                groups=2,
                provides_from=0,
            )
        elif command == "newcolumntype":
            column = _read_required(masked, after_command)
            if column is not None:
                _argument_count, body_cursor = _read_optional(masked, column[1])
                body = _read_required(masked, body_cursor)
                if body is not None:
                    parsed = _unit(
                        preamble,
                        start=start,
                        end=body[1],
                        kind=command,
                        provides=(f"column:{column[0].strip()}",),
                    )
        elif command in {"DeclareMathOperator", "DeclareMathAlphabet"}:
            command_cursor = _skip_space(masked, after_command)
            if command_cursor < len(masked) and masked[command_cursor] == "*":
                command_cursor += 1
            group_count = 2 if command == "DeclareMathOperator" else 5
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=command_cursor,
                groups=group_count,
                provides_from=0,
                control_target=True,
            )
        elif command in {
            "DeclareGraphicsExtensions",
            "graphicspath",
            "tikzset",
            "usetikzlibrary",
        }:
            parsed = _parse_fixed_groups_unit(
                preamble,
                masked,
                start=start,
                command=command,
                cursor=after_command,
                groups=1,
            )
        elif command == "let":
            target = _read_control_target(masked, after_command)
            if target is not None:
                line_end = masked.find("\n", target[1])
                end = len(masked) if line_end == -1 else line_end
                parsed = _unit(
                    preamble,
                    start=start,
                    end=end,
                    kind=command,
                    provides=(target[0],),
                )
        if parsed is None:
            cursor = after_command
            continue
        units.append(parsed)
        cursor = parsed.end
    return tuple(units)


def extract_declared_packages(preamble: str) -> tuple[str, ...]:
    """Return explicit package declarations in stable source order.

    Package discovery is shared by focused local-LaTeX contexts and browser-math capability
    selection.  Keeping one extractor prevents the parser from claiming a dependency that the
    isolated TeX renderer would not see (or vice versa).
    """

    return tuple(
        dict.fromkeys(
            package
            for unit in extract_preamble_units(preamble)
            for package in unit.packages
            if unit.kind in {"usepackage", "RequirePackage"}
        )
    )


def split_leading_render_declarations(source: str) -> tuple[str, str]:
    """Separate declarations injected at the start of an isolated block body.

    The project pass may copy a lexically active body declaration into a figure or table so the
    block remains independently compilable. Metadata normalization must not inspect or rewrite
    command bodies (for example, a helper macro may itself contain ``\\label``), so renderers
    preserve the contiguous declaration prefix verbatim and normalize only the visual body.
    """

    units = extract_preamble_units(source)
    cursor = 0
    for unit in units:
        if source[cursor : unit.start].strip():
            break
        if unit.kind in {"documentclass", "usepackage", "RequirePackage", "PassOptionsToPackage"}:
            break
        cursor = unit.end
    return source[:cursor].strip(), source[cursor:].strip()


def _render_package_unit(unit: PreambleUnit, excluded_packages: set[str]) -> str:
    remaining = [package for package in unit.packages if package not in excluded_packages]
    if not remaining:
        return ""
    if len(remaining) == len(unit.packages):
        return unit.source
    packages = ",".join(remaining)
    if unit.kind == "PassOptionsToPackage":
        return rf"\PassOptionsToPackage{{{unit.package_options}}}{{{packages}}}"
    options = f"[{unit.package_options}]" if unit.package_options else ""
    return f"\\{unit.kind}{options}{{{packages}}}"


def build_render_context(
    source_latex: str,
    preamble: str,
    *,
    excluded_packages: set[str] | None = None,
) -> RenderContext:
    """Build a paper-local context containing only safe declarations needed by a block.

    Package declarations remain available initially because TeX packages do not expose a
    machine-readable command registry. The renderer may deterministically exclude a declared
    package after the compiler proves that the package is unavailable; custom definitions are
    selected by a transitive control-sequence/environment dependency closure.
    """

    exclusions = excluded_packages or set()
    units = extract_preamble_units(preamble)
    needed = set(_references(source_latex))
    selected: set[int] = set()
    skipped_unsafe: list[str] = []

    for index, unit in enumerate(units):
        if unit.kind == "documentclass":
            class_match = _DOCUMENT_CLASS_NAME_RE.fullmatch(unit.source.strip())
            document_class = class_match.group(1).strip() if class_match else ""
            if document_class in _ISOLATED_DOCUMENT_CLASSES and not unit.unsafe_commands:
                selected.add(index)
            else:
                skipped_unsafe.append(f"documentclass:{document_class or 'unresolved'}")
        elif unit.kind in {"usepackage", "RequirePackage", "PassOptionsToPackage"}:
            if unit.unsafe_commands:
                skipped_unsafe.append(unit.label)
            else:
                selected.add(index)
        elif unit.kind in {"definecolor", "providecolor", "colorlet", "newcolumntype"}:
            if unit.unsafe_commands:
                skipped_unsafe.append(unit.label)
            else:
                selected.add(index)
                needed.update(unit.references)
        elif (
            unit.kind in {"graphicspath", "DeclareGraphicsExtensions"}
            and re.search(r"\\includegraphics\b", source_latex)
        ) or (
            unit.kind in {"tikzset", "usetikzlibrary"}
            and re.search(
                r"\\(?:tikz|draw|node|path)\b|\\begin\s*\{tikz(?:picture|cd)\}",
                source_latex,
            )
        ):
            selected.add(index)

    changed = True
    while changed:
        changed = False
        for index, unit in enumerate(units):
            if index in selected or not set(unit.provides).intersection(needed):
                continue
            if unit.unsafe_commands:
                skipped_unsafe.append(unit.label)
                continue
            selected.add(index)
            needed.update(unit.references)
            changed = True

    rendered_units: list[str] = []
    included_labels: list[str] = []
    declared_packages: list[str] = []
    skipped_packages: list[str] = []
    for index in sorted(selected, key=lambda item: units[item].start):
        unit = units[index]
        if unit.packages:
            declared_packages.extend(unit.packages)
            skipped_packages.extend(package for package in unit.packages if package in exclusions)
            rendered = _render_package_unit(unit, exclusions)
        else:
            rendered = unit.source
        if not rendered.strip():
            continue
        if "@" in rendered and unit.kind not in {
            "documentclass",
            "usepackage",
            "RequirePackage",
            "PassOptionsToPackage",
        }:
            rendered = f"\\makeatletter\n{rendered}\n\\makeatother"
        rendered_units.append(rendered.strip())
        included_labels.append(unit.label)

    focused = "\n".join(rendered_units).strip()
    unsafe_commands = tuple(
        sorted({match.group(1).lower() for match in _FORBIDDEN_CONTEXT_RE.finditer(preamble)})
    )
    return RenderContext(
        preamble=focused,
        context_hash=hashlib.sha256(focused.encode("utf-8")).hexdigest(),
        declared_packages=tuple(dict.fromkeys(declared_packages)),
        included_units=tuple(included_labels),
        skipped_unsafe_units=tuple(dict.fromkeys(skipped_unsafe)),
        skipped_unsafe_commands=unsafe_commands,
        skipped_missing_packages=tuple(dict.fromkeys(skipped_packages)),
    )
