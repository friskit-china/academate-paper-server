from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass

KATEX_NORMALIZER_VERSION = "katex-local-v15"

_KATEX_LOCAL_METADATA_COMMAND = re.compile(
    r"\\label\*?(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}|"
    r"\\tag\*?\s*\{[^{}]*\}|\\(?:notag|nonumber)\b",
)
_KATEX_OUTER_DISPLAY_ENVIRONMENT = re.compile(
    r"^\s*\\begin\s*\{(?P<environment>equation\*?|eqnarray\*?|align\*?|gather\*?|multline\*?|displaymath)\}"
    r"(?P<body>[\s\S]*)"
    r"\\end\s*\{(?P=environment)\}\s*$",
)
_KATEX_DISPLAY_ENVIRONMENT_REPLACEMENTS = {
    # ``eqnarray`` is part of LaTeX's standard format but is intentionally not
    # implemented by KaTeX. ``aligned`` retains its row and alignment-column
    # structure without changing the mathematical tokens in the cells.
    "eqnarray": "aligned",
    "eqnarray*": "aligned",
    "align": "aligned",
    "align*": "aligned",
    "gather": "gathered",
    "gather*": "gathered",
    # KaTeX has no multline environment.  Its line-break semantics are safely
    # represented by aligned; unlike stripping the wrapper, this keeps row
    # separators valid and never leaves bare alignment tokens in display math.
    "multline": "aligned",
    "multline*": "aligned",
}

# Exact, argument-free LaTeX control words whose math-mode meaning has a proved
# KaTeX spelling. This registry must stay deliberately small: additions require
# a TeX/KaTeX equivalence test and a real regression fixture. In particular it
# must never contain author macros or approximate glyph substitutions.
_KATEX_EXACT_CONTROL_WORD_ALIASES = {
    # LaTeX defines ``\slash`` as a slash with a discretionary line-break hint.
    # Math expressions have no paragraph line breaking, so the visible and
    # mathematical result is exactly the ordinary slash token.
    "slash": "/",
    # ``\hdots`` is LaTeX's horizontal ellipsis spelling.  KaTeX implements the same
    # punctuation atom as ``\ldots``.
    "hdots": r"\ldots",
}
_SIUNITX_COMMAND = re.compile(r"\\(?P<command>SI|qty|si|unit|num)(?![A-Za-z@])")
_VERTICAL_SPACE_COMMAND = re.compile(r"\\vspace\*?(?![A-Za-z@])")
_KATEX_LAYOUT_ONLY_COMMAND = re.compile(
    r"\\(?:protect|hfill|centering)(?![A-Za-z@])"
)
_LEGACY_BRACED_BOLD_MATH_RE = re.compile(
    r"\{\s*\\mbox\s*\{\s*\\boldmath\s*\{\s*\$(?P<body>[^$]+)\$\s*\}\s*\}\s*\}",
    re.DOTALL,
)
_LEGACY_BOLD_MATH_RE = re.compile(
    r"\\mbox\s*\{\s*\\boldmath\s*\{\s*\$(?P<body>[^$]+)\$\s*\}\s*\}",
    re.DOTALL,
)
_MBOX_COMMAND = re.compile(r"\\mbox(?![A-Za-z@])")
_MATH_TABULAR_BEGIN = re.compile(
    r"\\begin\s*\{tabular\}\s*\{(?P<columns>[lcr| \t]+)\}",
)
_MATH_TABULAR_END = re.compile(r"\\end\s*\{tabular\}")
_CONTROL_SPACE = re.compile(r"(?<!\\)\\[ \t]+")


@dataclass(frozen=True, slots=True)
class _PackageCompatibilityAdapter:
    """Translate one declared LaTeX package capability into KaTeX's dialect.

    The registry is intentionally keyed by package/capability families rather than author macro
    names.  Author macros are expanded by the parser's declaration IR first; adapters only see
    standard package syntax and may perform bounded, semantics-preserving rewrites.
    """

    capability: str
    packages: frozenset[str]
    normalize: Callable[[str], str]

# This is intentionally a bounded compatibility table, not a TeX interpreter.  It covers the
# semantic unit commands commonly emitted by siunitx while leaving unknown commands untouched so
# KaTeX can fail visibly and the normal verified-repair path can take over.
_SIUNITX_UNIT_COMMANDS = {
    # SI and binary prefixes.
    "yocto": "y",
    "zepto": "z",
    "atto": "a",
    "femto": "f",
    "pico": "p",
    "nano": "n",
    "micro": r"\mu ",
    "milli": "m",
    "centi": "c",
    "deci": "d",
    "deca": "da",
    "deka": "da",
    "hecto": "h",
    "kilo": "k",
    "mega": "M",
    "giga": "G",
    "tera": "T",
    "peta": "P",
    "exa": "E",
    "zetta": "Z",
    "yotta": "Y",
    "kibi": "Ki",
    "mebi": "Mi",
    "gibi": "Gi",
    "tebi": "Ti",
    # Common units.  Both spellings are accepted where siunitx accepts both.
    "ampere": "A",
    "angstrom": r"\mathring{A}",
    "bar": "bar",
    "becquerel": "Bq",
    "bit": "bit",
    "byte": "B",
    "candela": "cd",
    "celsius": r"{}^{\circ}C",
    "coulomb": "C",
    "day": "d",
    "decibel": "dB",
    "degree": r"{}^{\circ}",
    "electronvolt": "eV",
    "farad": "F",
    "gram": "g",
    "gray": "Gy",
    "henry": "H",
    "hertz": "Hz",
    "hour": "h",
    "joule": "J",
    "kelvin": "K",
    "liter": "L",
    "litre": "L",
    "meter": "m",
    "metre": "m",
    "minute": "min",
    "mole": "mol",
    "newton": "N",
    "ohm": r"\Omega ",
    "pascal": "Pa",
    "percent": r"\%",
    "radian": "rad",
    "second": "s",
    "siemens": "S",
    "sievert": "Sv",
    "steradian": "sr",
    "tesla": "T",
    "volt": "V",
    "watt": "W",
    "weber": "Wb",
    # Unit composition and powers.
    "per": "/",
    "squared": "^{2}",
    "cubed": "^{3}",
    "square": "^{2}",
    "cubic": "^{3}",
}


def _read_group(
    value: str,
    opening_index: int,
    *,
    opening: str,
    closing: str,
) -> tuple[str, int] | None:
    if opening_index >= len(value) or value[opening_index] != opening:
        return None
    depth = 0
    escaped = False
    for index in range(opening_index, len(value)):
        character = value[index]
        if character == "\\":
            escaped = not escaped
            continue
        if not escaped:
            if character == opening:
                depth += 1
            elif character == closing:
                depth -= 1
                if depth == 0:
                    return value[opening_index + 1 : index], index + 1
        escaped = False
    return None


def _skip_space(value: str, cursor: int) -> int:
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    return cursor


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _normalize_math_tabular_body(body: str) -> str | None:
    r"""Lower one simple text-mode tabular body into math-mode array cells.

    Legacy LaTeX generators commonly place ``tabular`` inside ``$...$`` so each cell can switch
    back into math mode with another pair of dollar delimiters. KaTeX cannot enter text-mode
    ``tabular`` from math, but its ``array`` environment has the same row/column structure. This
    adapter removes only balanced inner math delimiters and wraps proved plain-text cell fragments
    in ``\text{...}``. Unknown commands or TeX-special text remain unchanged by rejecting the
    conversion, so the normal local-TeX fallback retains authority for ambiguous cases.
    """

    output: list[str] = []
    plain: list[str] = []
    in_inner_math = False

    def flush_plain() -> bool:
        if not plain:
            return True
        chunk = "".join(plain)
        plain.clear()
        if not chunk.strip():
            output.append(chunk)
            return True
        if any(character in chunk for character in "\\{}%#_^~"):
            return False
        output.append(rf"\text{{{chunk}}}")
        return True

    cursor = 0
    while cursor < len(body):
        if body[cursor] == "$" and not _is_escaped(body, cursor):
            if not in_inner_math and not flush_plain():
                return None
            in_inner_math = not in_inner_math
            cursor += 1
            continue
        if in_inner_math:
            output.append(body[cursor])
            cursor += 1
            continue
        if body.startswith(r"\\", cursor):
            if not flush_plain():
                return None
            output.append(r"\\")
            cursor += 2
            continue
        if body[cursor] == "&":
            if not flush_plain():
                return None
            output.append("&")
            cursor += 1
            continue
        plain.append(body[cursor])
        cursor += 1
    if in_inner_math or not flush_plain():
        return None
    return "".join(output)


def _normalize_math_tabulars(value: str) -> str:
    r"""Convert bounded math-internal ``tabular`` environments to KaTeX ``array`` syntax."""

    output: list[str] = []
    cursor = 0
    while match := _MATH_TABULAR_BEGIN.search(value, cursor):
        end = _MATH_TABULAR_END.search(value, match.end())
        if end is None:
            break
        nested = _MATH_TABULAR_BEGIN.search(value, match.end(), end.start())
        # Line breaks immediately inside the environment delimiters are source formatting, not
        # cell content. Removing only the outer whitespace also prevents a trailing newline from
        # becoming part of the final ``\text{...}`` node.
        body = value[match.end() : end.start()].strip()
        normalized_body = None if nested is not None else _normalize_math_tabular_body(body)
        output.append(value[cursor : match.start()])
        if normalized_body is None:
            output.append(value[match.start() : end.end()])
        else:
            columns = re.sub(r"\s+", "", match.group("columns"))
            output.append(
                rf"\begin{{array}}{{{columns}}}{normalized_body}\end{{array}}"
            )
        cursor = end.end()
    output.append(value[cursor:])
    return "".join(output)


def _siunitx_arguments(
    value: str,
    cursor: int,
    count: int,
) -> tuple[list[str], int] | None:
    cursor = _skip_space(value, cursor)
    # siunitx options affect formatting, rounding, or locale.  Browser rendering keeps the
    # canonical value and unit but deliberately ignores those document-level presentation knobs.
    while cursor < len(value) and value[cursor] == "[":
        optional = _read_group(value, cursor, opening="[", closing="]")
        if optional is None:
            return None
        cursor = _skip_space(value, optional[1])
    arguments: list[str] = []
    for _ in range(count):
        if cursor >= len(value) or value[cursor] != "{":
            return None
        argument = _read_group(value, cursor, opening="{", closing="}")
        if argument is None:
            return None
        arguments.append(argument[0])
        cursor = _skip_space(value, argument[1])
    return arguments, cursor


def _normalize_siunitx_unit(value: str) -> str | None:
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if character.isspace() or character in "{}":
            cursor += 1
            continue
        if character == "~":
            output.append(r"\,")
            cursor += 1
            continue
        if character == "\\":
            spacing = re.match(r"\\(?:,|;|:|!|quad|qquad)(?![A-Za-z@])", value[cursor:])
            if spacing is not None:
                cursor += spacing.end()
                continue
            command = re.match(r"\\([A-Za-z@]+)", value[cursor:])
            if command is None:
                return None
            replacement = _SIUNITX_UNIT_COMMANDS.get(command.group(1))
            if replacement is None:
                return None
            output.append(replacement)
            cursor += command.end()
            continue
        if character.isalnum() or character in "./*+-^_()":
            output.append(character)
            cursor += 1
            continue
        return None
    normalized = "".join(output).strip()
    return normalized or None


def _normalize_siunitx(value: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in _SIUNITX_COMMAND.finditer(value):
        if match.start() < cursor:
            continue
        command = match.group("command")
        argument_count = 2 if command in {"SI", "qty"} else 1
        parsed = _siunitx_arguments(value, match.end(), argument_count)
        if parsed is None:
            continue
        arguments, end = parsed
        if command == "num":
            replacement = arguments[0].strip()
        else:
            unit_source = arguments[-1]
            unit = _normalize_siunitx_unit(unit_source)
            if unit is None:
                continue
            rendered_unit = rf"\mathrm{{{unit}}}"
            replacement = (
                rendered_unit
                if command in {"si", "unit"}
                else rf"{{{arguments[0].strip()}}}\,{rendered_unit}"
            )
        output.append(value[cursor : match.start()])
        output.append(replacement)
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def _strip_vertical_spacing(value: str) -> str:
    """Remove complete LaTeX-only vertical spacing commands from a math render target.

    ``\\vspace`` is valid document layout LaTeX, but it has no mathematical meaning and KaTeX
    intentionally does not implement it.  Only a syntactically complete command with one balanced
    braced argument is removed.  Malformed commands and longer author macros remain visible so the
    normal repair/error path can handle them without guessing.
    """

    output: list[str] = []
    cursor = 0
    for match in _VERTICAL_SPACE_COMMAND.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        argument = _read_group(value, argument_start, opening="{", closing="}")
        if argument is None:
            continue
        output.append(value[cursor : match.start()])
        cursor = argument[1]
    output.append(value[cursor:])
    return "".join(output)


def _normalize_exact_control_word_aliases(value: str) -> str:
    r"""Rewrite only complete, allow-listed TeX control words.

    This tiny lexer matters more than the size of the current registry. TeX
    treats ``\\`` as one control symbol followed by ordinary text, so a regular
    expression starting at the second backslash could incorrectly rewrite
    ``\\slash``. Consuming control symbols as pairs preserves that tokenization,
    and consuming the full letter run prevents prefix matches such as
    ``\slashAuthor``.
    """

    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        if value[cursor] != "\\" or cursor + 1 >= len(value):
            output.append(value[cursor])
            cursor += 1
            continue
        next_character = value[cursor + 1]
        if not (next_character.isalpha() or next_character == "@"):
            output.append(value[cursor : cursor + 2])
            cursor += 2
            continue
        end = cursor + 2
        while end < len(value) and (value[end].isalpha() or value[end] == "@"):
            end += 1
        command = value[cursor + 1 : end]
        output.append(_KATEX_EXACT_CONTROL_WORD_ALIASES.get(command, value[cursor:end]))
        cursor = end
    return "".join(output)


def _normalize_control_spaces(value: str) -> str:
    r"""Give TeX's control-space token a spelling that survives string trimming.

    ``$\ $`` is a common way to create an intentionally blank table cell. A trailing
    ``str.strip()`` cannot distinguish that meaningful space token from source formatting and
    used to leave a lone, invalid backslash. ``\space`` is the same TeX math-space token and is
    implemented by KaTeX. The negative look-behind preserves ``\\`` row separators.
    """

    return _CONTROL_SPACE.sub(r"\\space ", value)


_UPGREEK_NAMES = (
    "alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|"
    "omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega|Gamma|Delta|Theta|Lambda|"
    "Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega"
)
_UPGREEK_COMMAND = re.compile(rf"\\up(?P<name>{_UPGREEK_NAMES})(?![A-Za-z@])")
_TEXTCOMP_MU = re.compile(r"\\textmu(?![A-Za-z@])")


def _normalize_upgreek(value: str) -> str:
    """Render the standard upgreek alphabet through KaTeX's upright math font."""

    return _UPGREEK_COMMAND.sub(
        lambda match: rf"\mathrm{{\{match.group('name')}}}",
        value,
    )


def _normalize_textcomp(value: str) -> str:
    """Keep textcomp's micro sign as the same Unicode text glyph."""

    return _TEXTCOMP_MU.sub("μ", value)


def _normalize_mhchem_in_text(value: str) -> str:
    r"""Move a leading ``\ce`` out of a text-mode wrapper for KaTeX/mhchem.

    LaTeX's mhchem accepts ``\textrm{\ce{H2O} mass fraction}``, while KaTeX's mhchem
    expansion must begin in math mode. Splitting the chemical expression from a bounded plain
    text suffix preserves both components without interpreting chemical syntax ourselves.
    """

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\(?:text|textrm)(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        outer = _read_group(value, argument_start, opening="{", closing="}")
        if outer is None:
            continue
        body, end = outer
        leading = re.match(r"\s*\\ce(?![A-Za-z@])", body)
        if leading is None:
            continue
        chemical_start = _skip_space(body, leading.end())
        chemical = _read_group(body, chemical_start, opening="{", closing="}")
        if chemical is None:
            continue
        suffix = body[chemical[1] :].strip()
        if not suffix or re.search(r"[\\{}$%#_^~]", suffix):
            continue
        output.append(value[cursor : match.start()])
        output.append(rf"\ce{{{chemical[0]}}}\,\text{{{suffix}}}")
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def _normalize_plain_mboxes(value: str) -> str:
    r"""Lower ordinary math ``\mbox{...}`` nodes to KaTeX ``\text{...}`` nodes.

    In a mathematical formula both commands create one horizontal text box.  Nested dollar math,
    declaration programs, and the legacy ``\boldmath`` idiom remain untouched because those forms
    require TeX mode execution rather than a syntax-level spelling change.
    """

    output: list[str] = []
    cursor = 0
    for match in _MBOX_COMMAND.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        parsed = _read_group(value, argument_start, opening="{", closing="}")
        if parsed is None:
            continue
        body, end = parsed
        if (
            any(
                not _is_escaped(body, index)
                for index, character in enumerate(body)
                if character == "$"
            )
            or re.search(r"\\(?:begin|end|boldmath|write|input|include)(?![A-Za-z@])", body)
        ):
            continue
        output.append(value[cursor : match.start()])
        output.append(rf"\text{{{body}}}")
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def _unwrap_math_mode_commands(value: str) -> str:
    r"""Remove complete wrappers whose only job is to enter math mode.

    Every caller is already rendering one isolated math node.  Therefore
    ``\ensuremath{body}`` and ``body`` have the same TeX math list.  Balanced parsing keeps
    malformed invocations and longer author commands untouched.
    """

    normalized = value
    command = re.compile(r"\\ensuremath(?![A-Za-z@])")
    for _ in range(8):
        output: list[str] = []
        cursor = 0
        changed = False
        for match in command.finditer(normalized):
            if match.start() < cursor:
                continue
            argument_start = _skip_space(normalized, match.end())
            parsed = _read_group(normalized, argument_start, opening="{", closing="}")
            if parsed is None:
                continue
            output.append(normalized[cursor : match.start()])
            output.append(parsed[0])
            cursor = parsed[1]
            changed = True
        if not changed:
            break
        output.append(normalized[cursor:])
        normalized = "".join(output)
    return normalized


def _normalize_text_small_caps(value: str) -> str:
    r"""Retain text content when KaTeX cannot provide LaTeX small-cap typography.

    Only a bounded word-like text argument is lowered to upright math text.  Phrases,
    declarations, and nested TeX programs remain visible for the exact local-TeX path.
    """

    # ``\textsc`` may itself occur inside a text-mode wrapper.  KaTeX has no small-cap font,
    # but replacing the nested command with ``\mathrm`` is invalid because ``\mathrm`` is a
    # math-mode command.  Remove only the unsupported typography shell inside a bounded plain
    # text argument before lowering remaining math-mode occurrences below.
    nested_output: list[str] = []
    nested_cursor = 0
    text_command = re.compile(r"\\(?:text|textrm)(?![A-Za-z@])")
    nested_small_caps = re.compile(r"\\textsc(?![A-Za-z@])")
    for text_match in text_command.finditer(value):
        if text_match.start() < nested_cursor:
            continue
        outer_start = _skip_space(value, text_match.end())
        outer = _read_group(value, outer_start, opening="{", closing="}")
        if outer is None:
            continue
        body, outer_end = outer
        rebuilt: list[str] = []
        body_cursor = 0
        changed = False
        for caps_match in nested_small_caps.finditer(body):
            caps_start = _skip_space(body, caps_match.end())
            caps = _read_group(body, caps_start, opening="{", closing="}")
            if caps is None or re.fullmatch(r"[A-Za-z0-9._-]+", caps[0]) is None:
                continue
            rebuilt.append(body[body_cursor : caps_match.start()])
            rebuilt.append(caps[0])
            body_cursor = caps[1]
            changed = True
        if not changed:
            continue
        rebuilt.append(body[body_cursor:])
        nested_output.append(value[nested_cursor : text_match.start()])
        nested_output.append(f"{text_match.group(0)}{{{''.join(rebuilt)}}}")
        nested_cursor = outer_end
    nested_output.append(value[nested_cursor:])
    value = "".join(nested_output)

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\textsc(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        parsed = _read_group(value, argument_start, opening="{", closing="}")
        if parsed is None:
            continue
        body, end = parsed
        if not re.fullmatch(r"[A-Za-z0-9._-]+|[A-Za-z0-9.-]+(?:\\_[A-Za-z0-9.-]+)+", body):
            continue
        output.append(value[cursor : match.start()])
        output.append(rf"\mathrm{{{body}}}")
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def _unwrap_resizeboxes(value: str) -> str:
    r"""Drop only the visual scaling shell around one complete mathematical body."""

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\resizebox(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_cursor = match.end()
        arguments: list[str] = []
        end = argument_cursor
        for _ in range(3):
            argument_cursor = _skip_space(value, end)
            parsed = _read_group(value, argument_cursor, opening="{", closing="}")
            if parsed is None:
                break
            arguments.append(parsed[0])
            end = parsed[1]
        if len(arguments) != 3:
            continue
        body = arguments[2].strip()
        if len(body) >= 2 and body.startswith("$") and body.endswith("$"):
            body = body[1:-1].strip()
        if not body or re.search(r"\\(?:begin|end|write|input|include)(?![A-Za-z@])", body):
            continue
        output.append(value[cursor : match.start()])
        output.append(body)
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def _strip_math_layout_directives(value: str) -> str:
    r"""Remove complete row/layout directives with no mathematical content."""

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\noalign(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        parsed = _read_group(value, argument_start, opening="{", closing="}")
        if parsed is None or re.fullmatch(
            r"\s*\\(?:smallskip|medskip|bigskip|vskip|vspace)\*?(?:\s*\{[^{}]*\})?\s*",
            parsed[0],
        ) is None:
            continue
        output.append(value[cursor : match.start()])
        cursor = parsed[1]
    output.append(value[cursor:])
    return "".join(output)


def _normalize_extended_arrows(value: str) -> str:
    r"""Lower standard extensible-arrow syntax to KaTeX-supported equivalents.

    ``mathtools`` and ``extarrows`` accept the same optional lower label and required upper label
    shape as KaTeX's ``xleftarrow``/``xrightarrow``.  Only the package-specific minimum shaft
    length is lost.  The labels, direction, and mathematical relation are retained verbatim.

    The other patterns are the canonical TeX constructions used by author-defined long hook and
    two-headed arrows.  Matching the complete token construction prevents an isolated low-level
    primitive such as ``\lhook`` from being guessed.
    """

    normalized = _normalize_extensible_arrow_commands(value)
    normalized = re.sub(
        r"\\lhook\s*\\joinrel\s*\\(?:long)?rightarrow(?![A-Za-z@])",
        r"\\hookrightarrow",
        normalized,
    )
    normalized = re.sub(
        r"\\(?:long)?leftarrow\s*\\joinrel\s*\\rhook(?![A-Za-z@])",
        r"\\hookleftarrow",
        normalized,
    )
    normalized = re.sub(
        r"\\relbar\s*\\joinrel\s*\\twoheadrightarrow(?![A-Za-z@])",
        r"\\twoheadrightarrow",
        normalized,
    )
    return re.sub(
        r"\\twoheadleftarrow\s*\\joinrel\s*\\relbar(?![A-Za-z@])",
        r"\\twoheadleftarrow",
        normalized,
    )


_ARROW_LABEL_SPACING = re.compile(r"^\\(?:,|;|:|!|quad|qquad)(?![A-Za-z@])\s*")
_ARROW_LABEL_TRAILING_SPACING = re.compile(
    r"\s*\\(?:,|;|:|!|quad|qquad)(?![A-Za-z@])$"
)


def _unwrap_complete_command(value: str, command: str, *, argument_index: int) -> str | None:
    command_match = re.match(rf"\\{command}(?![A-Za-z@])", value)
    if command_match is None:
        return None
    cursor = command_match.end()
    arguments: list[str] = []
    for _ in range(argument_index + 1):
        cursor = _skip_space(value, cursor)
        parsed = _read_group(value, cursor, opening="{", closing="}")
        if parsed is None:
            return None
        arguments.append(parsed[0])
        cursor = parsed[1]
    return arguments[argument_index] if not value[cursor:].strip() else None


def _normalize_extensible_arrow_label(value: str) -> str:
    """Discard proved layout-only wrappers around one extensible-arrow label."""

    normalized = value.strip()
    while True:
        without_leading = _ARROW_LABEL_SPACING.sub("", normalized, count=1)
        without_spacing = _ARROW_LABEL_TRAILING_SPACING.sub("", without_leading, count=1)
        if without_spacing == normalized:
            break
        normalized = without_spacing.strip()
    changed = True
    while changed:
        changed = False
        for command, argument_index in (("smash", 0), ("raisebox", 1), ("ensuremath", 0)):
            unwrapped = _unwrap_complete_command(
                normalized,
                command,
                argument_index=argument_index,
            )
            if unwrapped is not None:
                normalized = unwrapped.strip()
                changed = True
                break
    return re.sub(
        r"^\\(?:displaystyle|textstyle|scriptstyle|scriptscriptstyle)\b\s*",
        "",
        normalized,
    )


def _normalize_extensible_arrow_commands(value: str) -> str:
    command = re.compile(r"\\xlong(?P<direction>right|left)arrow(?![A-Za-z@])")
    output: list[str] = []
    cursor = 0
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_cursor = _skip_space(value, match.end())
        optional_source = ""
        if argument_cursor < len(value) and value[argument_cursor] == "[":
            optional = _read_group(value, argument_cursor, opening="[", closing="]")
            if optional is None:
                continue
            optional_source = f"[{optional[0]}]"
            argument_cursor = _skip_space(value, optional[1])
        required = _read_group(value, argument_cursor, opening="{", closing="}")
        output.append(value[cursor : match.start()])
        replacement = "xrightarrow" if match.group("direction") == "right" else "xleftarrow"
        if required is None:
            output.append(rf"\{replacement}")
            cursor = match.end()
            continue
        output.append(
            rf"\{replacement}{optional_source}"
            rf"{{{_normalize_extensible_arrow_label(required[0])}}}"
        )
        cursor = required[1]
    output.append(value[cursor:])
    return "".join(output)


_DSFONT_KATEX_GLYPHS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ1hk")


def _normalize_dsfont(value: str) -> str:
    r"""Map the bounded ``dsfont`` glyph repertoire to KaTeX blackboard bold.

    KaTeX has no mechanism for loading ``dsfont.sty`` or its font files.  A single supported glyph
    has a deterministic Unicode/MathML meaning through ``\mathbb``.  Compound expressions and
    unsupported glyphs remain untouched so they fail visibly rather than being approximated.
    """

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\mathds(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        if argument_start >= len(value):
            continue
        argument_end: int
        if value[argument_start] == "{":
            parsed = _read_group(value, argument_start, opening="{", closing="}")
            if parsed is None:
                continue
            argument = parsed[0].strip()
            argument_end = parsed[1]
        else:
            argument = value[argument_start]
            argument_end = argument_start + 1
        if len(argument) != 1 or argument not in _DSFONT_KATEX_GLYPHS:
            continue
        output.append(value[cursor : match.start()])
        output.append(rf"\mathbb{{{argument}}}")
        cursor = argument_end
    output.append(value[cursor:])
    return "".join(output)


def _normalize_bbm(value: str) -> str:
    """Map BBM's proved single-glyph blackboard alphabet to KaTeX's mathbb alphabet."""

    output: list[str] = []
    cursor = 0
    command = re.compile(r"\\mathbbm(?![A-Za-z@])")
    for match in command.finditer(value):
        if match.start() < cursor:
            continue
        argument_start = _skip_space(value, match.end())
        parsed = _read_group(value, argument_start, opening="{", closing="}")
        if parsed is None:
            continue
        glyph = parsed[0].strip()
        if len(glyph) != 1 or glyph not in _DSFONT_KATEX_GLYPHS:
            continue
        output.append(value[cursor : match.start()])
        output.append(rf"\mathbb{{{glyph}}}")
        cursor = parsed[1]
    output.append(value[cursor:])
    return "".join(output)


_PACKAGE_COMPATIBILITY_ADAPTERS = (
    _PackageCompatibilityAdapter(
        capability="extended-arrows",
        packages=frozenset({"amsmath", "mathtools", "extarrows"}),
        normalize=_normalize_extended_arrows,
    ),
    _PackageCompatibilityAdapter(
        capability="double-stroke-math-alphabet",
        packages=frozenset({"dsfont"}),
        normalize=_normalize_dsfont,
    ),
    _PackageCompatibilityAdapter(
        capability="blackboard-bold-math-alphabet",
        packages=frozenset({"bbm", "mathbbm"}),
        normalize=_normalize_bbm,
    ),
    _PackageCompatibilityAdapter(
        capability="physical-units",
        packages=frozenset({"siunitx"}),
        normalize=_normalize_siunitx,
    ),
    _PackageCompatibilityAdapter(
        capability="upright-greek",
        packages=frozenset({"upgreek"}),
        normalize=_normalize_upgreek,
    ),
    _PackageCompatibilityAdapter(
        capability="text-symbols",
        packages=frozenset({"textcomp"}),
        normalize=_normalize_textcomp,
    ),
    _PackageCompatibilityAdapter(
        capability="chemical-formulae-in-text",
        packages=frozenset({"mhchem"}),
        normalize=_normalize_mhchem_in_text,
    ),
)


def _normalize_declared_package_capabilities(
    value: str,
    declared_packages: Collection[str],
) -> str:
    packages = {package.strip().casefold() for package in declared_packages if package.strip()}
    normalized = value
    for adapter in _PACKAGE_COMPATIBILITY_ADAPTERS:
        if packages.intersection(adapter.packages):
            normalized = adapter.normalize(normalized)
    return normalized


def normalize_legacy_bold_math(value: str) -> str:
    r"""Translate the classic ``\mbox{\boldmath{$...$}}`` idiom safely.

    The idiom is valid TeX because ``\mbox`` temporarily switches back to text
    mode.  KaTeX does not implement ``\boldmath`` and cannot accept the nested
    dollar delimiters inside an already extracted formula.  This deterministic
    rewrite preserves the formula symbol-for-symbol and is shared by inline and
    display-math parsing so the two paths cannot drift apart.
    """

    normalized = _LEGACY_BRACED_BOLD_MATH_RE.sub(
        lambda match: rf"{{\boldsymbol{{{match.group('body').strip()}}}}}",
        value,
    )
    return _LEGACY_BOLD_MATH_RE.sub(
        lambda match: rf"\boldsymbol{{{match.group('body').strip()}}}",
        normalized,
    )


def normalize_katex_latex(
    latex: str,
    *,
    environment: str | None = None,
    declared_packages: Collection[str] = (),
) -> str:
    """Return a bounded, deterministic KaTeX rendering form.

    The caller retains canonical source LaTeX separately.  This function performs syntax-only
    cleanup needed to embed a formula in KaTeX.  It deliberately does *not* translate commands
    supplied by LaTeX packages into guessed KaTeX equivalents: unsupported semantics must fail
    visibly and may then be rendered by the paper's TeX context.
    """

    normalized = _normalize_control_spaces(normalize_legacy_bold_math(latex))
    normalized = _KATEX_LOCAL_METADATA_COMMAND.sub("", normalized).strip()
    normalized = _normalize_math_tabulars(normalized)
    normalized = re.sub(r"(?<!\\)#", r"\\#", normalized)
    source_environment = environment
    outer = _KATEX_OUTER_DISPLAY_ENVIRONMENT.fullmatch(normalized)
    if outer is not None:
        source_environment = outer.group("environment")
        body = outer.group("body").strip()
        normalized = body
    replacement = _KATEX_DISPLAY_ENVIRONMENT_REPLACEMENTS.get(source_environment or "")
    already_wrapped = re.match(r"^\s*\\begin\s*\{(?:aligned|gathered)\}", normalized)
    if replacement is not None and already_wrapped is None:
        normalized = rf"\begin{{{replacement}}}{normalized}\end{{{replacement}}}"
    normalized = _strip_vertical_spacing(normalized)
    # ``\protect`` only controls macro expansion in moving LaTeX arguments and ``\hfill`` only
    # controls horizontal placement.  Neither contributes mathematical content, and neither is
    # implemented by KaTeX.  Removing the complete primitive is therefore syntax cleanup rather
    # than a guessed command substitution; author commands with longer names remain untouched.
    normalized = _KATEX_LAYOUT_ONLY_COMMAND.sub("", normalized)
    normalized = re.sub(r"\\/(?![A-Za-z@])", "", normalized)
    normalized = _unwrap_math_mode_commands(normalized)
    normalized = _normalize_text_small_caps(normalized)
    normalized = _unwrap_resizeboxes(normalized)
    normalized = _strip_math_layout_directives(normalized)
    normalized = _normalize_plain_mboxes(normalized)
    normalized = _normalize_declared_package_capabilities(normalized, declared_packages)
    normalized = _normalize_exact_control_word_aliases(normalized)
    return normalized.strip()
