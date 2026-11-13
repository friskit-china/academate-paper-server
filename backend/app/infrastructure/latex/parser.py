from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from app.domain.ports import Storage
from app.infrastructure.latex.katex import normalize_katex_latex, normalize_legacy_bold_math
from app.infrastructure.latex.render_context import (
    extract_declared_packages,
    extract_preamble_units,
)

PARSER_VERSION = "latex-tiered-v5.24"
PROJECT_IR_VERSION = "latex-project-ir-v1"
MATH_RENDER_CONTRACT_VERSION = "math-render-contract-v1"

# Include expansion is intentionally bounded even though ordinary projects may
# nest \input/\include to arbitrary practical depth. Hitting either guard is a
# hard parse failure so the system never presents a silently truncated paper.
MAX_INCLUDE_DEPTH = 128
MAX_EXPANDED_SOURCE_CHARS = 50_000_000


class LatexParseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    block_type: str
    ordinal_path: str
    source_text: str | None
    source_payload: dict[str, Any]
    render_payload: dict[str, Any]
    source_locator: dict[str, Any]
    content_hash: str
    heading_level: int | None = None
    parent_ordinal_path: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    root_file: str
    blocks: list[ParsedBlock]
    warnings: list[str]
    renderer_preamble: str = ""
    project_index: dict[str, Any] | None = None
    parser_version: str = PARSER_VERSION


SECTION_LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
# These commands are parser grammar, even when a class or package redefines
# their TeX presentation. Registering a style shim such as
# ``\providecommand{\section}{}`` as an author macro can erase the structural
# token before block parsing when the subsequent ``\@startsection`` renewal is
# intentionally rejected by the safe macro IR. Author aliases whose replacement
# emits one of these commands remain expandable.
_STRUCTURAL_COMMAND_NAMES = frozenset(SECTION_LEVELS)
_DISPLAY_COUNTER_NAMES = frozenset(
    {
        *SECTION_LEVELS,
        "algorithm",
        "equation",
        "figure",
        "page",
        "table",
    }
)
_COUNTER_REPRESENTATION_NAMES = frozenset(
    f"the{counter}" for counter in _DISPLAY_COUNTER_NAMES
)
ENVIRONMENT_TYPES = {
    "abstract": "PARAGRAPH",
    "equation": "EQUATION",
    "equation*": "EQUATION",
    "eqnarray": "EQUATION",
    "eqnarray*": "EQUATION",
    "align": "EQUATION",
    "align*": "EQUATION",
    "gather": "EQUATION",
    "gather*": "EQUATION",
    "multline": "EQUATION",
    "multline*": "EQUATION",
    "displaymath": "EQUATION",
    "figure": "FIGURE",
    "figure*": "FIGURE",
    "wrapfigure": "FIGURE",
    "sidewaysfigure": "FIGURE",
    "sidewaysfigure*": "FIGURE",
    "tikzcd": "FIGURE",
    "tikzpicture": "FIGURE",
    "table": "TABLE",
    "table*": "TABLE",
    "wraptable": "TABLE",
    "sidewaystable": "TABLE",
    "sidewaystable*": "TABLE",
    "tabular": "TABLE",
    "tabular*": "TABLE",
    "tabularx": "TABLE",
    "longtable": "TABLE",
    "itemize": "LIST",
    "enumerate": "LIST",
    "description": "LIST",
    "verbatim": "CODE",
    "verbatim*": "CODE",
    "lstlisting": "CODE",
    "minted": "CODE",
    "algorithm": "ALGORITHM",
    "algorithm*": "ALGORITHM",
    "algorithmic": "ALGORITHM",
    # ``tabbing`` is a layout program, not ordinary prose.  Its ``\\=``/``\\>`` tab-stop
    # commands and ``\\kill`` measurement rows are meaningful only to TeX; flattening the
    # environment leaks those controls and the invisible measurement text into the reader.
    # Preserve the canonical environment for the deterministic native-LaTeX renderer.
    "tabbing": "LATEX_BLOCK",
    "theorem": "THEOREM",
    "lemma": "THEOREM",
    "proposition": "THEOREM",
    "corollary": "THEOREM",
    "definition": "THEOREM",
    "assumption": "THEOREM",
    "remark": "THEOREM",
    "claim": "THEOREM",
    "conjecture": "THEOREM",
    "example": "THEOREM",
    "fact": "THEOREM",
    "observation": "THEOREM",
    "axiom": "THEOREM",
    "proof": "THEOREM",
    "thebibliography": "BIBLIOGRAPHY",
}
THEOREM_ENVIRONMENT_LABELS = {
    name: name.replace("*", "").replace("_", " ").title()
    for name, block_type in ENVIRONMENT_TYPES.items()
    if block_type == "THEOREM"
}
_NEW_THEOREM_RE = re.compile(
    r"\\newtheorem(?P<star>\*)?\s*"
    r"\{(?P<environment>[A-Za-z@][A-Za-z0-9@_*-]*)\}\s*"
    r"(?:\[(?P<shared_counter>[^\]]*)\]\s*)?"
    r"\{(?P<label>[^{}]+)\}"
    r"(?:\s*\[(?P<within_counter>[^\]]*)\])?",
)
_INCLUDE_RE = re.compile(
    r"\\(?P<command>input|include)(?![A-Za-z@])\s*"
    r"(?:\{(?P<braced>[^{}\r\n]+)\}|(?P<bare>[^\s%{}]+))",
)
_EXTERNAL_CODE_COMMANDS = (
    "VerbatimInput",
    "BVerbatimInput",
    "LVerbatimInput",
    "verbatiminput",
    "verbatimtabinput",
    "lstinputlisting",
    "inputminted",
)
_DISPLAY_CONTAINER_ENVIRONMENTS = ("center", "flushleft", "flushright")
_EXTERNAL_CODE_COMMAND_TOKEN_RE = re.compile(
    r"\\(?P<command>"
    + "|".join(re.escape(command) for command in _EXTERNAL_CODE_COMMANDS)
    + r")(?P<star>\*)?(?![A-Za-z@])"
)
_CITATION_COMMAND_TOKEN_RE = re.compile(
    r"\\(?P<command>[A-Za-z@]*cite[A-Za-z@]*)(?P<star>\*)?"
)
_NON_CITATION_COMMANDS = {
    "bstctlcite",
    "citefield",
    "citereset",
    "citestyle",
    "citetitle",
    "citeurl",
}
_REFERENCE_RE = re.compile(
    r"(?:~|\s)*(?:\\S\s*)?\\(?:ref|pageref|eqref|autoref|cref|Cref)\*?\s*\{[^{}]*\}",
)
_REFERENCE_COMMAND_RE = re.compile(
    r"\\(?P<command>pageref|eqref|autoref|cref|Cref|ref)\*?\s*"
    r"\{(?P<labels>[^{}]+)\}",
)
_LABEL_RE = re.compile(
    r"\\label(?:\s*\[[^\]]*\])?\s*\{(?P<label>[^{}]+)\}"
)
_TAG_RE = re.compile(r"\\tag\*?\s*\{(?P<tag>[^{}]+)\}")
_BIBLIOGRAPHY_COMMAND_RE = re.compile(
    r"\\(?:bibliography|bibliographystyle|printbibliography|addbibresource)\s*\{[^{}]*\}",
)
_BIBLIOGRAPHY_CONTROL_RE = re.compile(r"\\bstctlcite\s*\{[^{}]*\}")
_BIBLIOGRAPHY_STYLE_RE = re.compile(
    r"\\bibliographystyle\s*\{(?P<style>[^{}]+)\}",
)
_BIBLIOGRAPHY_DATA_RE = re.compile(
    r"\\bibliography\s*\{(?P<files>[^{}]+)\}",
)
_BIBLATEX_RESOURCE_RE = re.compile(
    r"\\addbibresource(?:\s*\[[^\]]*\])?\s*\{(?P<file>[^{}]+)\}",
)
_MATH_START_RE = re.compile(r"\$\$|\$|\\\[|\\\(")
_TEXT_MARK_COMMAND_RE = re.compile(
    r"\\(?P<command>textbf|emph|textit|texttt|underline|textsc)\s*\{",
)
_TEXT_MARKS = {
    "textbf": "strong",
    "emph": "emphasis",
    "textit": "emphasis",
    "texttt": "code",
    "underline": "underline",
    "textsc": "smallcaps",
}
_TRANSPARENT_TEXT_WRAPPER_RE = re.compile(
    r"\\(?P<command>textcolor|colorbox|fcolorbox|href)(?![A-Za-z@])\s*"
    r"(?:\[[^\]]*\]\s*)?\{",
)
_TRANSPARENT_TEXT_WRAPPER_ARGUMENTS = {
    "textcolor": 2,
    "colorbox": 2,
    "fcolorbox": 3,
    "href": 2,
}
_MACRO_DEFINITION_RE = re.compile(
    r"\\(?P<kind>newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*"
    r"\{?\\(?P<name>[A-Za-z@]+)\}?",
)
_DEF_MACRO_DEFINITION_RE = re.compile(r"\\def\s*\\(?P<name>[A-Za-z@]+)")
_DOCUMENT_COMMAND_DEFINITION_RE = re.compile(
    r"\\(?P<kind>NewDocumentCommand|RenewDocumentCommand|"
    r"ProvideDocumentCommand|DeclareDocumentCommand)\*?(?![A-Za-z@])"
)
_MATH_OPERATOR_DEFINITION_RE = re.compile(
    r"\\DeclareMathOperator(?P<star>\*)?(?![A-Za-z@])"
)
_PAIRED_DELIMITER_DEFINITION_RE = re.compile(
    r"\\(?P<kind>DeclarePairedDelimiterX|DeclarePairedDelimiter)(?![A-Za-z@])"
)
_ENVIRONMENT_SCOPE_RE = re.compile(
    r"\\(?P<kind>begin|end)\s*\{(?P<environment>[^{}]+)\}"
)
# Preserve the existing browser-friendly preference order for extension-less
# ``includegraphics`` references, then fall back to legacy PostScript formats
# that require deterministic server-side rendering.
_IMAGE_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".eps", ".ps")


def _special_re(environment_types: dict[str, str]) -> re.Pattern[str]:
    environments = "|".join(
        re.escape(name) for name in sorted(environment_types, key=len, reverse=True)
    )
    external_code_commands = "|".join(
        re.escape(command) for command in _EXTERNAL_CODE_COMMANDS
    )
    display_containers = "|".join(
        re.escape(environment) for environment in _DISPLAY_CONTAINER_ENVIRONMENTS
    )
    return re.compile(
        r"\\(?P<section>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)"
        r"(?P<section_star>\*)?\s*(?:\[[^\]]*\]\s*)?\{"
        r"|\\(?P<bibliography>bibliography)(?![A-Za-z@])\s*\{"
        r"|\\(?P<printbibliography>printbibliography)(?![A-Za-z@])"
        r"\s*(?:\[[^\]]*\])?"
        r"|\\(?P<appendix>appendix)(?![A-Za-z@])"
        r"|(?P<counter_set>\\setcounter(?![A-Za-z@])\s*"
        r"\{(?P<counter_set_name>[A-Za-z@]+)\}\s*"
        r"\{(?P<counter_set_value>[+-]?\d+)\})"
        r"|(?P<counter_adjust>\\addtocounter(?![A-Za-z@])\s*"
        r"\{(?P<counter_adjust_name>[A-Za-z@]+)\}\s*"
        r"\{(?P<counter_adjust_value>[+-]?\d+)\})"
        r"|(?P<counter_format>\\(?:newcommand|renewcommand|providecommand)\*?\s*"
        r"(?:\{\s*\\the(?P<counter_format_braced>[A-Za-z@]+)\s*\}"
        r"|\\the(?P<counter_format_bare>[A-Za-z@]+))\s*\{)"
        rf"|\\begin\s*\{{(?P<display_container>{display_containers})\}}"
        rf"|\\begin\s*\{{(?P<environment>{environments})\}}"
        rf"|\\(?P<external_code_command>{external_code_commands})"
        r"(?P<external_code_star>\*)?(?![A-Za-z@])"
        r"|\\(?P<pdf_document>includepdf)(?![A-Za-z@])\s*(?:\[[^\]]*\]\s*)?\{",
        re.DOTALL,
    )


@dataclass(frozen=True, slots=True)
class MacroArgument:
    kind: str
    default: str | None = None


@dataclass(frozen=True, slots=True)
class MacroDefinition:
    name: str
    arguments: tuple[MacroArgument, ...]
    replacement: str

    @property
    def argument_count(self) -> int:
        return len(self.arguments)


# These standard LaTeX commands consume their next token (or braced group) as
# one mandatory argument. A declared author macro is itself one TeX token, so
# textual macro lowering must retain that atomic boundary when such a command
# consumes it. This is syntax metadata for TeX's argument grammar, not a list
# of author-command substitutions.
_STANDARD_UNDELIMITED_ARGUMENT_COMMANDS = frozenset(
    {
        "acute",
        "bar",
        "boldsymbol",
        "breve",
        "check",
        "ddot",
        "dot",
        "grave",
        "hat",
        "hphantom",
        "mathbb",
        "mathbf",
        "mathcal",
        "mathfrak",
        "mathit",
        "mathnormal",
        "mathrm",
        "mathring",
        "mathsf",
        "mathtt",
        "mbox",
        "operatorname",
        "overbrace",
        "overline",
        "phantom",
        "pmb",
        "smash",
        "sqrt",
        "text",
        "tilde",
        "underbrace",
        "underline",
        "vec",
        "vphantom",
        "widehat",
        "widetilde",
    }
)


def _strip_comments_with_offsets(text: str) -> tuple[str, tuple[int, ...]]:
    r"""Apply TeX comment line-splicing and map every retained character to source.

    An unescaped ``%`` discards both the rest of its physical line and that line's end marker.
    Keeping the newline would invent a paragraph inside constructs such as Scientific Word's
    ``$% ... \Omega ... $`` expansions and can split one inline formula into separate blocks.
    The offset map lets the project IR retain accurate original-file locations even though the
    structural stream is shorter than the source.
    """

    output: list[str] = []
    source_offsets: list[int] = []
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if character == "%":
            slash_count = 0
            slash_cursor = cursor - 1
            while slash_cursor >= 0 and text[slash_cursor] == "\\":
                slash_count += 1
                slash_cursor -= 1
            if slash_count % 2 == 0:
                while cursor < len(text) and text[cursor] not in {"\r", "\n"}:
                    cursor += 1
                if cursor < len(text) and text[cursor] == "\r":
                    cursor += 1
                    if cursor < len(text) and text[cursor] == "\n":
                        cursor += 1
                elif cursor < len(text) and text[cursor] == "\n":
                    cursor += 1
                continue
        output.append(character)
        source_offsets.append(cursor)
        cursor += 1
    return "".join(output), tuple(source_offsets)


def _strip_comments(text: str) -> str:
    return _strip_comments_with_offsets(text)[0]


def _mask_comment_bodies_for_tex(text: str) -> str:
    r"""Remove comment contents while retaining TeX's end-of-line suppression.

    Structural parsing uses :func:`_strip_comments`, whose preserved newlines keep source
    locations stable. That representation must not be compiled, however: a comment-only line
    inside an optional argument would become a blank line and therefore a ``\par`` token. For
    renderer input we retain the unescaped ``%`` and every line ending, replacing only the comment
    body with spaces. This keeps both TeX semantics and line numbers without interpreting any
    package-specific syntax.
    """

    characters = list(text)
    cursor = 0
    while cursor < len(characters):
        if characters[cursor] != "%":
            cursor += 1
            continue
        slash_count = 0
        slash_cursor = cursor - 1
        while slash_cursor >= 0 and characters[slash_cursor] == "\\":
            slash_count += 1
            slash_cursor -= 1
        if slash_count % 2:
            cursor += 1
            continue
        end = cursor + 1
        while end < len(characters) and characters[end] not in {"\r", "\n"}:
            characters[end] = " "
            end += 1
        cursor = end
    return "".join(characters)


def _read_argument(text: str, opening_brace: int) -> tuple[str, int]:
    if opening_brace >= len(text) or text[opening_brace] != "{":
        raise LatexParseError("Expected a braced LaTeX argument.")
    depth = 0
    escaped = False
    for index in range(opening_brace, len(text)):
        character = text[index]
        if character == "\\":
            escaped = not escaped
            continue
        if not escaped:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return text[opening_brace + 1 : index], index + 1
        escaped = False
    raise LatexParseError("Unclosed braced LaTeX argument.")


def _read_optional_argument(text: str, opening_bracket: int) -> tuple[str, int]:
    if opening_bracket >= len(text) or text[opening_bracket] != "[":
        raise LatexParseError("Expected a bracketed LaTeX argument.")
    depth = 0
    escaped = False
    for index in range(opening_bracket, len(text)):
        character = text[index]
        if character == "\\":
            escaped = not escaped
            continue
        if not escaped:
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    return text[opening_bracket + 1 : index], index + 1
        escaped = False
    raise LatexParseError("Unclosed bracketed LaTeX argument.")


def _external_code_invocation(
    text: str,
    *,
    start: int,
    command: str,
    command_end: int,
) -> dict[str, Any]:
    """Read one package command whose semantics are to include a file verbatim.

    These commands must not flow through ordinary LaTeX-to-prose cleanup: doing so discards the
    command while leaving its filename (and nearby presentation options) as visible prose.
    ``inputminted`` has an additional language argument; all other supported commands have one
    file argument.  This parser reads syntax only and never executes TeX or accesses a host path.
    """

    cursor = _skip_whitespace(text, command_end)
    options: str | None = None
    if cursor < len(text) and text[cursor] == "[":
        options, cursor = _read_optional_argument(text, cursor)
        cursor = _skip_whitespace(text, cursor)
    if cursor >= len(text) or text[cursor] != "{":
        raise LatexParseError(f"Missing file argument for \\{command}.")
    first_argument, cursor = _read_argument(text, cursor)
    language: str | None = None
    target = first_argument
    if command == "inputminted":
        language = first_argument.strip() or None
        cursor = _skip_whitespace(text, cursor)
        if cursor >= len(text) or text[cursor] != "{":
            raise LatexParseError("Missing file argument for \\inputminted.")
        target, cursor = _read_argument(text, cursor)
    return {
        "start": start,
        "end": cursor,
        "command": command,
        "options": options,
        "language": language,
        "target": target.strip(),
        "raw_latex": text[start:cursor],
    }


def _citation_command_spans(
    value: str,
    start: int = 0,
    *,
    include_nocite: bool = False,
) -> list[dict[str, Any]]:
    """Read citation commands with balanced optional and key arguments.

    Citation packages expose a large command family, so command identity is discovered from
    syntax instead of a fixed list. Commands that merely inspect one field or configure a citation
    style are excluded because their mandatory argument is not a citation cluster.
    """

    spans: list[dict[str, Any]] = []
    cursor = start
    while match := _CITATION_COMMAND_TOKEN_RE.search(value, cursor):
        command = match.group("command")
        normalized_command = command.casefold()
        cursor = match.end()
        if normalized_command in _NON_CITATION_COMMANDS or (
            normalized_command == "nocite" and not include_nocite
        ):
            continue
        arguments_cursor = cursor
        optionals: list[str] = []
        malformed = False
        for _index in range(2):
            while arguments_cursor < len(value) and value[arguments_cursor].isspace():
                arguments_cursor += 1
            if arguments_cursor >= len(value) or value[arguments_cursor] != "[":
                break
            try:
                optional, arguments_cursor = _read_optional_argument(value, arguments_cursor)
            except LatexParseError:
                malformed = True
                break
            optionals.append(optional)
        if malformed:
            continue
        while arguments_cursor < len(value) and value[arguments_cursor].isspace():
            arguments_cursor += 1
        if arguments_cursor >= len(value) or value[arguments_cursor] != "{":
            continue
        try:
            keys_argument, end = _read_argument(value, arguments_cursor)
        except LatexParseError:
            continue
        keys = [key.strip() for key in keys_argument.split(",") if key.strip()]
        if not keys:
            cursor = end
            continue
        # AMSRefs expresses a citation locator after the mandatory key argument, for
        # example ``\cite{key}*{\S 2}``.  It is part of the citation token, not prose.
        # Recognize the balanced syntax rather than enumerating citation commands.
        postnote: str | None = None
        postnote_cursor = end
        while postnote_cursor < len(value) and value[postnote_cursor].isspace():
            postnote_cursor += 1
        if postnote_cursor < len(value) and value[postnote_cursor] == "*":
            argument_cursor = postnote_cursor + 1
            while argument_cursor < len(value) and value[argument_cursor].isspace():
                argument_cursor += 1
            if argument_cursor < len(value) and value[argument_cursor] == "{":
                try:
                    postnote, end = _read_argument(value, argument_cursor)
                except LatexParseError:
                    postnote = None
        spans.append(
            {
                "start": match.start(),
                "end": end,
                "command": command,
                "starred": bool(match.group("star")),
                "options": optionals,
                "keys": keys,
                "postnote": postnote,
                "raw_latex": value[match.start() : end],
            }
        )
        cursor = end
    return spans


def _strip_citation_commands(value: str) -> str:
    spans = _citation_command_spans(value, include_nocite=True)
    if not spans:
        return value
    chunks: list[str] = []
    cursor = 0
    for span in spans:
        chunks.append(value[cursor : int(span["start"])])
        cursor = int(span["end"])
    chunks.append(value[cursor:])
    return "".join(chunks)


def _decode_source(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LatexParseError("A LaTeX source file could not be decoded.")


def discover_root_file(sources: dict[str, str]) -> str:
    if not sources:
        raise LatexParseError("The source manifest contains no LaTeX files.")

    def score(item: tuple[str, str]) -> tuple[int, int, str]:
        path, text = item
        lowered_name = PurePosixPath(path).name.lower()
        value = 0
        value += 100 if re.search(r"\\documentclass(?:\[[^\]]*\])?\s*\{", text) else 0
        value += 50 if r"\begin{document}" in text else 0
        value += 20 if r"\end{document}" in text else 0
        common_names = {"main.tex", "paper.tex", "article.tex", "manuscript.tex"}
        value += 10 if lowered_name in common_names else 0
        return value, -len(PurePosixPath(path).parts), path

    root, _ = max(sources.items(), key=score)
    return root


def _resolve_include(
    current_path: str,
    root_path: str,
    target: str,
    sources: dict[str, str],
) -> str | None:
    target_path = PurePosixPath(target.strip())
    if not target_path.parts or ".." in target_path.parts or target_path.is_absolute():
        return None

    # A root LaTeX document is normally compiled with its directory as the
    # working directory, so nested plain \input paths remain root-relative.
    # Falling back to the including file's directory retains compatibility with
    # archives authored with sibling-relative includes.
    base_dirs = [PurePosixPath(root_path).parent, PurePosixPath(current_path).parent]
    candidates: list[PurePosixPath] = []
    for base_dir in base_dirs:
        candidate = base_dir / target_path
        variants = (
            [candidate.with_suffix(".tex"), candidate]
            if candidate.suffix == ""
            else [candidate]
        )
        for variant in variants:
            if variant not in candidates:
                candidates.append(variant)
    return next((item.as_posix() for item in candidates if item.as_posix() in sources), None)


def _expand_source(
    path: str,
    sources: dict[str, str],
    warnings: list[str],
    root_path: str | None = None,
    stack: tuple[str, ...] = (),
    preserve_tex_comments: bool = False,
) -> str:
    root_path = root_path or path
    if path in stack:
        warnings.append(f"Cyclic include ignored: {' -> '.join((*stack, path))}")
        return ""
    if len(stack) >= MAX_INCLUDE_DEPTH:
        raise LatexParseError(
            "LaTeX include nesting exceeds the safe depth limit "
            f"({MAX_INCLUDE_DEPTH}): {' -> '.join((*stack, path))}"
        )
    text = (
        _mask_comment_bodies_for_tex(sources[path])
        if preserve_tex_comments
        else _strip_comments(sources[path])
    )

    chunks: list[str] = []
    expanded_chars = 0
    cursor = 0
    for match in _INCLUDE_RE.finditer(text):
        prefix = text[cursor : match.start()]
        chunks.append(prefix)
        expanded_chars += len(prefix)
        target = match.group("braced") or match.group("bare") or ""
        included = _resolve_include(path, root_path, target, sources)
        if included is None:
            warnings.append(f"Missing include from {path}: {target}")
            replacement = match.group(0)
        else:
            replacement = _expand_source(
                included,
                sources,
                warnings,
                root_path=root_path,
                stack=(*stack, path),
                preserve_tex_comments=preserve_tex_comments,
            )
        chunks.append(replacement)
        expanded_chars += len(replacement)
        if expanded_chars > MAX_EXPANDED_SOURCE_CHARS:
            raise LatexParseError(
                "Expanded LaTeX source exceeds the safe size limit "
                f"({MAX_EXPANDED_SOURCE_CHARS} characters)."
            )
        cursor = match.end()

    suffix = text[cursor:]
    chunks.append(suffix)
    expanded_chars += len(suffix)
    if expanded_chars > MAX_EXPANDED_SOURCE_CHARS:
        raise LatexParseError(
            "Expanded LaTeX source exceeds the safe size limit "
            f"({MAX_EXPANDED_SOURCE_CHARS} characters)."
        )
    return "".join(chunks)


def _expand_source_with_map(
    path: str,
    sources: dict[str, str],
    warnings: list[str],
    *,
    root_path: str | None = None,
    stack: tuple[str, ...] = (),
    include_edges: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Expand project includes while retaining a deterministic project-level source map.

    The map describes the include-expanded, comment-stripped source before safe macro
    expansion.  That explicit boundary is preferable to pretending that offsets survive macro
    expansion; block locators therefore record the mapping phase they refer to.
    """

    root_path = root_path or path
    include_edges = include_edges if include_edges is not None else []
    if path in stack:
        warnings.append(f"Cyclic include ignored: {' -> '.join((*stack, path))}")
        return "", []
    if len(stack) >= MAX_INCLUDE_DEPTH:
        raise LatexParseError(
            "LaTeX include nesting exceeds the safe depth limit "
            f"({MAX_INCLUDE_DEPTH}): {' -> '.join((*stack, path))}"
        )
    raw_source = sources[path]
    text, source_offsets = _strip_comments_with_offsets(raw_source)
    chunks: list[str] = []
    segments: list[dict[str, Any]] = []
    cursor = 0
    output_offset = 0

    def append_source(value: str, source_start: int) -> None:
        nonlocal output_offset
        if not value:
            return
        chunks.append(value)
        run_start = 0
        for local_index in range(1, len(value) + 1):
            transformed_index = source_start + local_index
            at_gap = (
                local_index < len(value)
                and source_offsets[transformed_index]
                != source_offsets[transformed_index - 1] + 1
            )
            if local_index < len(value) and not at_gap:
                continue
            raw_start = source_offsets[source_start + run_start]
            raw_end = source_offsets[source_start + local_index - 1] + 1
            segments.append(
                {
                    "expanded_start": output_offset + run_start,
                    "expanded_end": output_offset + local_index,
                    "source_file": path,
                    "source_start": raw_start,
                    "source_end": raw_end,
                    "source_line": raw_source.count("\n", 0, raw_start) + 1,
                }
            )
            run_start = local_index
        output_offset += len(value)

    for match in _INCLUDE_RE.finditer(text):
        append_source(text[cursor : match.start()], cursor)
        target = match.group("braced") or match.group("bare") or ""
        included = _resolve_include(path, root_path, target, sources)
        include_edges.append(
            {
                "from": path,
                "target": target,
                "resolved": included,
                "source_line": raw_source.count(
                    "\n",
                    0,
                    source_offsets[match.start()],
                )
                + 1,
            }
        )
        if included is None:
            warnings.append(f"Missing include from {path}: {target}")
            append_source(match.group(0), match.start())
        else:
            replacement, child_segments = _expand_source_with_map(
                included,
                sources,
                warnings,
                root_path=root_path,
                stack=(*stack, path),
                include_edges=include_edges,
            )
            chunks.append(replacement)
            segments.extend(
                {
                    **segment,
                    "expanded_start": int(segment["expanded_start"]) + output_offset,
                    "expanded_end": int(segment["expanded_end"]) + output_offset,
                }
                for segment in child_segments
            )
            output_offset += len(replacement)
        if output_offset > MAX_EXPANDED_SOURCE_CHARS:
            raise LatexParseError(
                "Expanded LaTeX source exceeds the safe size limit "
                f"({MAX_EXPANDED_SOURCE_CHARS} characters)."
            )
        cursor = match.end()
    append_source(text[cursor:], cursor)
    if output_offset > MAX_EXPANDED_SOURCE_CHARS:
        raise LatexParseError(
            "Expanded LaTeX source exceeds the safe size limit "
            f"({MAX_EXPANDED_SOURCE_CHARS} characters)."
        )
    return "".join(chunks), segments


_PACKAGE_DECLARATION_RE = re.compile(
    r"\\(?:usepackage|RequirePackage)(?:\s*\[(?P<options>[^\]]*)\])?\s*"
    r"\{(?P<packages>[^{}]+)\}"
)
_ENVIRONMENT_DECLARATION_RE = re.compile(
    r"\\(?P<kind>newenvironment|renewenvironment|provideenvironment|newtheorem)\*?\s*"
    r"\{(?P<name>[A-Za-z@][A-Za-z0-9@_*-]*)\}"
)


def _expand_local_package_declarations(
    preamble: str,
    *,
    sources: dict[str, str],
    root_file: str,
) -> str:
    r"""Inject safe declarations from project-local packages in TeX load order.

    TeX executes a local ``.sty`` file when ``\usepackage``/``\RequirePackage`` loads it.  The
    parser must not execute that program, but it can feed the file through the same bounded macro
    declaration IR used for the main preamble.  Expanding only manifest-owned package files keeps
    system packages as capability declarations and avoids a growing registry of author commands.
    """

    loaded: set[str] = set()

    def expand(value: str, *, current_path: str, stack: tuple[str, ...]) -> str:
        output: list[str] = []
        cursor = 0
        for match in _PACKAGE_DECLARATION_RE.finditer(value):
            output.append(value[cursor : match.end()])
            for declared in match.group("packages").split(","):
                package = declared.strip()
                if not package:
                    continue
                target = package if package.casefold().endswith(".sty") else f"{package}.sty"
                resolved = _resolve_include(current_path, root_file, target, sources)
                if resolved is None or resolved in loaded or resolved in stack:
                    continue
                loaded.add(resolved)
                local_source = _strip_comments(sources[resolved])
                output.extend(
                    (
                        f"\n% begin local package declarations: {resolved}\n",
                        expand(
                            local_source,
                            current_path=resolved,
                            stack=(*stack, resolved),
                        ),
                        f"\n% end local package declarations: {resolved}\n",
                    )
                )
            cursor = match.end()
        output.append(value[cursor:])
        return "".join(output)

    return expand(preamble, current_path=root_file, stack=(root_file,))


def _resolve_bibliography_file(
    declared: str,
    *,
    root_file: str,
    available_paths: set[str],
) -> str | None:
    candidate = declared.strip()
    if not candidate:
        return None
    if not candidate.casefold().endswith(".bib"):
        candidate += ".bib"
    candidate_path = PurePosixPath(candidate)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        return None
    root_directory = PurePosixPath(root_file).parent
    relative = (root_directory / candidate_path).as_posix()
    for possible in (relative, candidate_path.as_posix()):
        if possible in available_paths:
            return possible
    casefolded = {path.casefold(): path for path in available_paths}
    return casefolded.get(relative.casefold()) or casefolded.get(
        candidate_path.as_posix().casefold()
    )


def _bibliography_print_sites(body: str) -> list[dict[str, Any]]:
    """Record bibliography output commands in document order.

    Bibliography data declarations and bibliography presentation are separate
    concerns in LaTeX.  The structured entries can be compiled once, while the
    exact source position of ``\\bibliography``, ``\\printbibliography`` or an
    inline ``thebibliography`` determines where that compiled list belongs in
    the reading flow.  Keeping every site in the project IR also leaves an
    explicit boundary for future per-refsection bibliography support.
    """

    appendix_offsets = [
        match.start()
        for match in re.finditer(r"\\appendix(?![A-Za-z@])", body)
    ]
    matches: list[tuple[int, str]] = []
    for command, pattern in (
        ("bibliography", r"\\bibliography(?![A-Za-z@])\s*\{"),
        ("printbibliography", r"\\printbibliography(?![A-Za-z@])"),
        ("thebibliography", r"\\begin\s*\{thebibliography\}"),
    ):
        matches.extend((match.start(), command) for match in re.finditer(pattern, body))
    return [
        {
            "sequence": sequence,
            "command": command,
            "expanded_body_offset": offset,
            "document_region": (
                "appendix"
                if appendix_offsets and offset > appendix_offsets[0]
                else "main"
            ),
        }
        for sequence, (offset, command) in enumerate(sorted(matches), start=1)
    ]


def _bibliography_project_index(
    *,
    expanded: str,
    root_file: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    body = _document_body(expanded)
    print_sites = _bibliography_print_sites(body)
    inline_sources: list[dict[str, str]] = []
    inline_cursor = 0
    inline_index = 0
    inline_begin_re = re.compile(r"\\begin\s*\{thebibliography\}")
    while inline_match := inline_begin_re.search(body, inline_cursor):
        try:
            _content, inline_end = _find_environment_end(
                body,
                inline_match.start(),
                "thebibliography",
            )
        except LatexParseError:
            break
        inline_index += 1
        inline_sources.append(
            {
                "source_path": f"{root_file}#thebibliography-{inline_index}",
                "latex": body[inline_match.start() : inline_end],
            }
        )
        inline_cursor = inline_end
    citations = _citation_command_spans(body, include_nocite=True)
    citation_keys = [
        key
        for citation in citations
        for key in citation["keys"]
        if citation["command"].casefold() != "nocite" or key != "*"
    ]
    nocite_all = any(
        citation["command"].casefold() == "nocite" and "*" in citation["keys"]
        for citation in citations
    )
    available_bib_files = {
        str(item.get("path", ""))
        for item in manifest.get("files", [])
        if isinstance(item, dict)
        and PurePosixPath(str(item.get("path", ""))).suffix.casefold() == ".bib"
    }
    available_bbl_files = {
        str(item.get("path", ""))
        for item in manifest.get("files", [])
        if isinstance(item, dict)
        and PurePosixPath(str(item.get("path", ""))).suffix.casefold() == ".bbl"
    }
    declared_files = [
        name.strip()
        for match in _BIBLIOGRAPHY_DATA_RE.finditer(expanded)
        for name in match.group("files").split(",")
        if name.strip()
    ]
    biblatex_files = [
        match.group("file").strip()
        for match in _BIBLATEX_RESOURCE_RE.finditer(expanded)
        if match.group("file").strip()
    ]
    source_files = [
        resolved
        for declared in (*declared_files, *biblatex_files)
        if (
            resolved := _resolve_bibliography_file(
                declared,
                root_file=root_file,
                available_paths=available_bib_files,
            )
        )
    ]
    if not source_files and available_bib_files and (citations or declared_files or biblatex_files):
        # Some publisher templates generate the bibliography command indirectly. Falling back to
        # the manifest's BibTeX files preserves data without guessing any entry content.
        source_files = sorted(available_bib_files)
    compiled_source_files: list[str] = []
    if available_bbl_files and (citations or declared_files or biblatex_files):
        # A submitted BBL is keyed to the TeX job name, not necessarily to the
        # argument of \bibliography.  Prefer the root document companion because
        # multi-document archives may contain several unrelated compiled bibliographies.
        root_bbl = PurePosixPath(root_file).with_suffix(".bbl").as_posix()
        casefolded_bbl = {path.casefold(): path for path in available_bbl_files}
        if matched_root := casefolded_bbl.get(root_bbl.casefold()):
            compiled_source_files = [matched_root]
        else:
            root_directory = PurePosixPath(root_file).parent
            declared_bbl_candidates = [
                (root_directory / PurePosixPath(name).with_suffix(".bbl")).as_posix()
                for name in declared_files
                if name
                and not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts
            ]
            compiled_source_files = [
                resolved
                for candidate in declared_bbl_candidates
                if (resolved := casefolded_bbl.get(candidate.casefold()))
            ]
            if not compiled_source_files and len(available_bbl_files) == 1:
                compiled_source_files = list(available_bbl_files)
    style_matches = list(_BIBLIOGRAPHY_STYLE_RE.finditer(expanded))
    requested_style = style_matches[-1].group("style").strip() if style_matches else None
    biblatex_options = ""
    amsrefs_options = ""
    for package_match in _PACKAGE_DECLARATION_RE.finditer(expanded):
        packages = {name.strip().casefold() for name in package_match.group("packages").split(",")}
        if "biblatex" in packages:
            biblatex_options = (package_match.group("options") or "").strip()
        if "amsrefs" in packages:
            amsrefs_options = (package_match.group("options") or "").strip()
    backend = "biber" if biblatex_files or biblatex_options else "bibtex"
    if backend == "biber" and requested_style is None:
        style_option = re.search(r"(?:^|,)\s*style\s*=\s*([^,]+)", biblatex_options)
        requested_style = style_option.group(1).strip() if style_option else None
    return {
        "backend": backend,
        "requested_style": requested_style,
        "source_files": list(dict.fromkeys(source_files)),
        "compiled_source_files": list(dict.fromkeys(compiled_source_files)),
        "amsrefs_options": amsrefs_options,
        "amsrefs_citation_style": next(
            (
                option
                for option in (part.strip().casefold() for part in amsrefs_options.split(","))
                if option in {"numeric", "alphabetic", "shortalphabetic", "author-year"}
            ),
            "numeric",
        ),
        # ArXiv submissions frequently contain the already-compiled BBL as an inline
        # thebibliography environment and omit the original .bib.  Retaining these bounded
        # compiler-produced items lets Phase 2 preserve citation identity and presentation
        # without inventing missing BibTeX fields.
        "inline_sources": inline_sources,
        "declared_source_names": [*declared_files, *biblatex_files],
        "citation_keys": list(dict.fromkeys(citation_keys)),
        "nocite_all": nocite_all,
        "citation_count": sum(
            citation["command"].casefold() != "nocite" for citation in citations
        ),
        "print_sites": print_sites,
        "has_print_command": bool(print_sites),
    }


def _project_index(
    *,
    manifest: dict[str, Any],
    sources: dict[str, str],
    root_file: str,
    expanded: str,
    source_map: list[dict[str, Any]],
    include_edges: list[dict[str, Any]],
    bibliography: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable Phase-1 project IR used by every downstream renderer."""

    files = []
    manifest_by_path = {
        str(item.get("path", "")): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    for path in sorted(sources):
        item = manifest_by_path.get(path, {})
        encoded = sources[path].encode("utf-8", errors="replace")
        files.append(
            {
                "path": path,
                "suffix": PurePosixPath(path).suffix.lower(),
                "sha256": str(item.get("sha256") or hashlib.sha256(encoded).hexdigest()),
                "byte_size": int(item.get("byte_size") or len(encoded)),
            }
        )

    packages: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    environments: list[dict[str, Any]] = []
    for path, text in sorted(sources.items()):
        for match in _PACKAGE_DECLARATION_RE.finditer(text):
            packages.extend(
                {
                    "name": name.strip(),
                    "options": (match.group("options") or "").strip(),
                    "source_file": path,
                    "source_line": text.count("\n", 0, match.start()) + 1,
                }
                for name in match.group("packages").split(",")
                if name.strip()
            )
        for _offset, syntax, match in _macro_definition_matches(text):
            macros.append(
                {
                    "name": match.groupdict().get("name") or "",
                    "family": syntax,
                    "source_file": path,
                    "source_line": text.count("\n", 0, match.start()) + 1,
                }
            )
        for match in _ENVIRONMENT_DECLARATION_RE.finditer(text):
            environments.append(
                {
                    "name": match.group("name"),
                    "family": match.group("kind"),
                    "source_file": path,
                    "source_line": text.count("\n", 0, match.start()) + 1,
                }
            )

    labels = sorted({match.group("label").strip() for match in _LABEL_RE.finditer(expanded)})
    references = sorted(
        {
            label.strip()
            for match in _REFERENCE_COMMAND_RE.finditer(expanded)
            for label in match.group("labels").split(",")
            if label.strip()
        }
    )
    package_names = {item["name"].casefold() for item in packages}
    engine_evidence: list[str] = []
    if package_names.intersection({"fontspec", "unicode-math"}) or re.search(
        r"\\(?:setmainfont|setsansfont|setmonofont)\b", expanded
    ):
        engine_evidence.append("unicode-font-package-or-command")
        engine_candidates = ["xelatex", "lualatex", "pdflatex", "latex"]
    else:
        engine_candidates = ["latex", "pdflatex", "xelatex", "lualatex"]
    return {
        "version": PROJECT_IR_VERSION,
        "root_file": root_file,
        "files": files,
        "include_graph": include_edges,
        "source_map_phase": "include-expanded-comment-stripped-pre-macro",
        "source_map": source_map,
        "packages": packages,
        "macros": macros,
        "environments": environments,
        "cross_references": {
            "labels": labels,
            "references": references,
            "citations": bibliography.get("citation_keys", []),
        },
        "bibliography": bibliography,
        "engine_candidates": engine_candidates,
        "engine_evidence": engine_evidence,
        "local_support_files": [
            path
            for path in sorted(sources)
            if PurePosixPath(path).suffix.lower() in {".sty", ".cls", ".def", ".ltx"}
        ],
    }


def _document_body(text: str) -> str:
    start_match = re.search(r"\\begin\s*\{document\}", text)
    if start_match is None:
        return text
    end_match = re.search(r"\\end\s*\{document\}", text[start_match.end() :])
    if end_match is None:
        return text[start_match.end() :]
    return text[start_match.end() : start_match.end() + end_match.start()]


def _document_preamble(text: str) -> str:
    start_match = re.search(r"\\begin\s*\{document\}", text)
    return text[: start_match.start()] if start_match is not None else ""


def _mask_ranges_preserving_newlines(
    text: str,
    ranges: list[tuple[int, int]],
) -> str:
    """Hide structural TeX without changing line positions used by source locators."""

    if not ranges:
        return text
    characters = list(text)
    for start, end in ranges:
        for index in range(max(0, start), min(len(characters), end)):
            if characters[index] not in {"\n", "\r"}:
                characters[index] = " "
    return "".join(characters)


def _extract_body_tikz_styles(body: str) -> tuple[str, list[str]]:
    """Move body-level TikZ style declarations into the isolated render preamble.

    ``\\tikzstyle`` changes rendering state but has no readable document content.  It
    is nevertheless commonly declared immediately before a picture in the document
    body.  Keeping the exact declaration lets a later per-block LaTeX render reproduce
    the author's picture while masking it prevents presentation options from leaking
    into the structured reading view.
    """

    ranges: list[tuple[int, int]] = []
    declarations: list[str] = []
    pattern = re.compile(r"\\tikzstyle\s*\{")
    cursor = 0
    while match := pattern.search(body, cursor):
        try:
            _name, end = _read_argument(body, match.end() - 1)
        except LatexParseError:
            cursor = match.end()
            continue
        while end < len(body) and body[end].isspace():
            end += 1
        if end < len(body) and body[end] == "=":
            end += 1
        while end < len(body) and body[end].isspace():
            end += 1
        if end >= len(body) or body[end] != "[":
            cursor = match.end()
            continue
        try:
            _options, declaration_end = _read_optional_argument(body, end)
        except LatexParseError:
            cursor = match.end()
            continue
        declarations.append(body[match.start() : declaration_end].strip())
        ranges.append((match.start(), declaration_end))
        cursor = declaration_end
    return _mask_ranges_preserving_newlines(body, ranges), declarations


def _unwrap_textual_tikz_containers(body: str) -> str:
    """Remove TikZ layout chrome around semantic minipage prose and lists.

    A TikZ node containing a minipage is often just a decorated text box.  Turning
    that whole box into an image would make its prose unselectable and untranslated;
    parsing it as ordinary prose leaks node coordinates and style names.  This pass
    masks only the balanced container commands and leaves their braced human content
    in place.  Genuine drawings (no minipage) remain ``FIGURE`` blocks.
    """

    begin_re = re.compile(r"\\begin\s*\{tikzpicture\}(?:\s*\[[^\]]*\])?", re.DOTALL)
    end_re = re.compile(r"\\end\s*\{tikzpicture\}", re.DOTALL)
    minipage_begin_re = re.compile(r"\\begin\s*\{minipage\}\s*\{", re.DOTALL)
    minipage_end_re = re.compile(r"\\end\s*\{minipage\}", re.DOTALL)
    node_re = re.compile(
        r"\\node\s*(?:\[[^\]]*\]\s*)?(?:\([^)]*\)\s*)?"
        r"(?:at\s*\([^)]*\)\s*)?\{",
        re.DOTALL,
    )
    drawing_command_re = re.compile(
        r"\\(?:draw|path|fill|filldraw|shade|shadedraw|coordinate|matrix|clip|"
        r"useasboundingbox|pgfmath[A-Za-z@]*|def|edef|gdef|xdef|foreach|addplot|graph|"
        r"pic|pattern|spy)(?![A-Za-z@])"
    )
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while match := begin_re.search(body, cursor):
        try:
            content, environment_end = _find_environment_end(
                body, match.start(), "tikzpicture"
            )
        except LatexParseError:
            cursor = match.end()
            continue
        content_start = match.end()
        if minipage_begin_re.search(content) is None:
            cursor = environment_end
            continue
        # A genuine diagram may include one explanatory minipage alongside paths, computed
        # coordinates, or drawing-local definitions.  Unwrapping that mixed visual leaks its
        # TikZ program into prose and invents inline formulas from node labels.  Preserve the
        # entire environment whenever any non-textual TikZ primitive is present.
        if drawing_command_re.search(content) is not None:
            cursor = environment_end
            continue

        ranges.append((match.start(), match.end()))
        closing_matches = list(end_re.finditer(body, content_start, environment_end))
        if closing_matches:
            ranges.append((closing_matches[-1].start(), closing_matches[-1].end()))

        for minipage_match in minipage_begin_re.finditer(content):
            opening_brace = content_start + minipage_match.end() - 1
            try:
                _width, end = _read_argument(body, opening_brace)
            except LatexParseError:
                end = content_start + minipage_match.end()
            ranges.append((content_start + minipage_match.start(), end))
        for minipage_match in minipage_end_re.finditer(content):
            ranges.append(
                (
                    content_start + minipage_match.start(),
                    content_start + minipage_match.end(),
                )
            )

        # A textual box may contain a large content node plus a small title node.
        # Balanced argument parsing is essential here because list items, math, and
        # formatting commands legitimately contain many nested braces.
        node_cursor = 0
        while node_match := node_re.search(content, node_cursor):
            opening_brace = content_start + node_match.end() - 1
            try:
                _node_content, node_end = _read_argument(body, opening_brace)
            except LatexParseError:
                node_cursor = node_match.end()
                continue
            ranges.append((content_start + node_match.start(), opening_brace + 1))
            ranges.append((node_end - 1, node_end))
            semicolon_end = node_end
            while semicolon_end < environment_end and body[semicolon_end].isspace():
                semicolon_end += 1
            if semicolon_end < environment_end and body[semicolon_end] == ";":
                ranges.append((node_end, semicolon_end + 1))
            node_cursor = node_end - content_start
        cursor = environment_end
    return _mask_ranges_preserving_newlines(body, ranges)


def _skip_whitespace(text: str, cursor: int) -> int:
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _read_macro_target(text: str, cursor: int) -> tuple[str, int] | None:
    """Read either ``{\\name}`` or ``\\name`` without accepting arbitrary TeX."""

    cursor = _skip_whitespace(text, cursor)
    if cursor < len(text) and text[cursor] == "{":
        try:
            target, end = _read_argument(text, cursor)
        except LatexParseError:
            return None
        match = re.fullmatch(r"\s*\\([A-Za-z@]+)\s*", target)
        return (match.group(1), end) if match is not None else None
    match = re.match(r"\\([A-Za-z@]+)", text[cursor:])
    return (match.group(1), cursor + match.end()) if match is not None else None


def _parse_document_argument_spec(value: str) -> tuple[MacroArgument, ...] | None:
    """Parse the bounded, declarative xparse argument forms used by paper macros.

    ``m``, ``o``, and ``O{default}`` cover mandatory arguments, optional no-value
    arguments, and optional arguments with defaults.  Long-argument modifiers do
    not change invocation parsing, so a leading ``+`` is accepted.  Other xparse
    processors remain unresolved instead of being guessed or executed.
    """

    arguments: list[MacroArgument] = []
    cursor = 0
    while (cursor := _skip_whitespace(value, cursor)) < len(value):
        if value[cursor] == "+":
            cursor = _skip_whitespace(value, cursor + 1)
            if cursor >= len(value):
                return None
        kind = value[cursor]
        cursor += 1
        if kind == "m":
            arguments.append(MacroArgument("mandatory"))
        elif kind == "o":
            arguments.append(MacroArgument("optional"))
        elif kind == "O":
            cursor = _skip_whitespace(value, cursor)
            if cursor >= len(value) or value[cursor] != "{":
                return None
            try:
                default, cursor = _read_argument(value, cursor)
            except LatexParseError:
                return None
            arguments.append(MacroArgument("optional", default=default))
        else:
            return None
        if len(arguments) > 9:
            return None
    return tuple(arguments)


def _macro_definition_matches(text: str) -> list[tuple[int, str, re.Match[str]]]:
    definitions = [
        (match.start(), "command", match) for match in _MACRO_DEFINITION_RE.finditer(text)
    ]
    definitions.extend(
        (match.start(), "def", match) for match in _DEF_MACRO_DEFINITION_RE.finditer(text)
    )
    definitions.extend(
        (match.start(), "document-command", match)
        for match in _DOCUMENT_COMMAND_DEFINITION_RE.finditer(text)
    )
    definitions.extend(
        (match.start(), "math-operator", match)
        for match in _MATH_OPERATOR_DEFINITION_RE.finditer(text)
    )
    definitions.extend(
        (match.start(), "paired-delimiter", match)
        for match in _PAIRED_DELIMITER_DEFINITION_RE.finditer(text)
    )
    return sorted(definitions, key=lambda item: item[0])


def _parse_macro_definition(
    text: str,
    syntax: str,
    match: re.Match[str],
) -> tuple[str, MacroDefinition, int] | None:
    """Compile one supported declaration family into the shared safe macro IR."""

    cursor = _skip_whitespace(text, match.end())
    arguments: tuple[MacroArgument, ...] = ()

    if syntax in {"document-command", "math-operator", "paired-delimiter"}:
        target = _read_macro_target(text, cursor)
        if target is None:
            return None
        name, cursor = target
    else:
        name = match.group("name")

    if syntax == "def":
        argument_numbers: list[int] = []
        while cursor + 1 < len(text) and text[cursor] == "#" and text[cursor + 1].isdigit():
            argument_numbers.append(int(text[cursor + 1]))
            cursor = _skip_whitespace(text, cursor + 2)
        if argument_numbers and argument_numbers != list(range(1, len(argument_numbers) + 1)):
            return None
        # Delimited TeX parameters such as ``#1,#2`` are not this declaration grammar.
        if cursor >= len(text) or text[cursor] != "{":
            return None
        arguments = tuple(MacroArgument("mandatory") for _ in argument_numbers)
        kind = "def"
    elif syntax == "command":
        argument_count = 0
        if cursor < len(text) and text[cursor] == "[":
            try:
                count_source, cursor = _read_optional_argument(text, cursor)
                argument_count = int(count_source.strip())
            except (LatexParseError, ValueError):
                return None
            cursor = _skip_whitespace(text, cursor)
        if not 0 <= argument_count <= 9:
            return None
        optional_default: str | None = None
        if cursor < len(text) and text[cursor] == "[":
            if argument_count == 0:
                return None
            try:
                optional_default, cursor = _read_optional_argument(text, cursor)
            except LatexParseError:
                return None
            cursor = _skip_whitespace(text, cursor)
        arguments_list = [MacroArgument("mandatory") for _ in range(argument_count)]
        if optional_default is not None:
            arguments_list[0] = MacroArgument("optional", default=optional_default)
        arguments = tuple(arguments_list)
        raw_kind = match.group("kind")
        kind = "newcommand" if raw_kind == "DeclareRobustCommand" else raw_kind
    elif syntax == "document-command":
        cursor = _skip_whitespace(text, cursor)
        if cursor >= len(text) or text[cursor] != "{":
            return None
        try:
            spec, cursor = _read_argument(text, cursor)
        except LatexParseError:
            return None
        parsed_arguments = _parse_document_argument_spec(spec)
        if parsed_arguments is None:
            return None
        arguments = parsed_arguments
        kind = {
            "NewDocumentCommand": "newcommand",
            "RenewDocumentCommand": "renewcommand",
            "ProvideDocumentCommand": "providecommand",
            "DeclareDocumentCommand": "declarecommand",
        }[match.group("kind")]
    elif syntax == "math-operator":
        kind = "newcommand"

    else:
        argument_count = 1
        if match.group("kind") == "DeclarePairedDelimiterX":
            cursor = _skip_whitespace(text, cursor)
            if cursor < len(text) and text[cursor] == "[":
                try:
                    count_source, cursor = _read_optional_argument(text, cursor)
                    argument_count = int(count_source.strip())
                except (LatexParseError, ValueError):
                    return None
            if not 1 <= argument_count <= 7:
                return None
        delimiters: list[str] = []
        for _ in range(2):
            cursor = _skip_whitespace(text, cursor)
            if cursor >= len(text) or text[cursor] != "{":
                return None
            try:
                delimiter, cursor = _read_argument(text, cursor)
            except LatexParseError:
                return None
            delimiters.append(delimiter)
        opening, closing = delimiters
        body = "#3"
        if match.group("kind") == "DeclarePairedDelimiterX":
            cursor = _skip_whitespace(text, cursor)
            if cursor >= len(text) or text[cursor] != "{":
                return None
            try:
                body, cursor = _read_argument(text, cursor)
            except LatexParseError:
                return None
            body = re.sub(
                r"#([1-7])",
                lambda placeholder: f"#{int(placeholder.group(1)) + 2}",
                body,
            )
        else:
            cursor = _skip_whitespace(text, cursor)
        plain_body = re.sub(r"\\delimsize(?![A-Za-z@])", "", body)
        sized_body = re.sub(r"\\delimsize(?![A-Za-z@])", "#2", body)
        starred_body = re.sub(r"\\delimsize(?![A-Za-z@])", r"\\middle", body)
        # ``mathtools`` implements paired delimiters as one math atom.  Keep that
        # atomic boundary when serializing the declarative IR back to TeX.  Bare
        # delimiters are observable syntax in contexts such as ``tikzcd`` matrix
        # cells, so emitting ``| body |`` here can change a valid node into TikZ
        # options even though the same text looks equivalent in ordinary math.
        starred_replacement = rf"{{\left{opening} {starred_body}\right{closing}}}"
        sized_replacement = rf"{{{{#2 {opening}}}{sized_body}{{#2 {closing}}}}}"
        plain_replacement = rf"{{{opening} {plain_body} {closing}}}"
        replacement = (
            rf"\IfBooleanTF{{#1}}{{{starred_replacement}}}"
            rf"{{\IfValueTF{{#2}}{{{sized_replacement}}}{{{plain_replacement}}}}}"
        )
        arguments = (
            MacroArgument("star"),
            MacroArgument("optional"),
            *(MacroArgument("mandatory") for _ in range(argument_count)),
        )
        unsafe = re.search(
            r"\\(?:begin|end|input|include|includegraphics|write|openout|read|usepackage|node|tcb)"
            r"|\\[A-Za-z]*@",
            replacement,
        )
        if len(replacement) > 1024 or unsafe is not None:
            return None
        return "newcommand", MacroDefinition(name, arguments, replacement), cursor

    cursor = _skip_whitespace(text, cursor)
    if cursor >= len(text) or text[cursor] != "{":
        return None
    try:
        replacement, definition_end = _read_argument(text, cursor)
    except LatexParseError:
        return None
    if syntax == "math-operator":
        # ``\DeclareMathOperator`` does not expand to ``\operatorname{body}`` for an
        # arbitrary body.  In particular, author declarations such as
        # ``\DeclareMathOperator{\Shape}{\widehat{\Pi}}`` compile through amsmath's
        # operator machinery, while the naively serialized
        # ``\operatorname{\widehat{\Pi}}`` corrupts TeX's argument scan.  Plain textual
        # names can use KaTeX's exact operator spelling; structured mathematical bodies retain
        # their math list and the declaration's limits policy explicitly.
        if re.fullmatch(r"[A-Za-z0-9 .,:;+'`-]+", replacement.strip()):
            operator_command = (
                r"\operatorname*" if match.group("star") else r"\operatorname"
            )
            replacement = f"{operator_command}{{{replacement}}}"
        else:
            limits_command = r"\limits" if match.group("star") else r"\nolimits"
            replacement = rf"\mathop{{{replacement}}}{limits_command}"

    unsafe = re.search(
        r"\\(?:begin|end|input|include|includegraphics|write|openout|read|usepackage|node|tcb)"
        r"|\\[A-Za-z]*@",
        replacement,
    )
    if len(replacement) > 1024 or unsafe is not None:
        return None
    return kind, MacroDefinition(name, arguments, replacement.strip()), definition_end


def _extract_macros(text: str) -> dict[str, MacroDefinition]:
    """Build a declarative macro registry without executing a TeX program.

    Only declarations in the current top-level TeX scope are globally visible.  A
    textual regex over a package file also sees declarations inside another macro's
    replacement text.  Registering those eagerly changes TeX semantics: for example,
    a drawing helper containing ``\\def\\width{0pt}`` must not overwrite the package's
    global length token until that helper is actually invoked.  Track braces, explicit
    groups, and environments while walking declarations, and jump across every parsed
    replacement body so nested implementation declarations never leak into the global
    safe-expansion registry.
    """

    macros: dict[str, MacroDefinition] = {}
    scan_cursor = 0
    brace_depth = 0
    explicit_group_depth = 0
    environment_stack: list[str] = []
    # Even when a declaration is too dynamic or implementation-heavy for the safe macro IR,
    # its argument body is still one syntactic preamble unit.  Do not interpret literal
    # ``\begingroup`` or ``\begin{tabular}`` tokens inside that opaque replacement as active
    # outer scope.  This lets the scanner recover at the next declaration while continuing to
    # reject execution of the unsupported unit itself.
    opaque_units = tuple(extract_preamble_units(text))
    opaque_index = 0

    def scan_scope(until: int) -> None:
        nonlocal scan_cursor, brace_depth, explicit_group_depth, opaque_index
        while scan_cursor < until:
            while (
                opaque_index < len(opaque_units)
                and opaque_units[opaque_index].end <= scan_cursor
            ):
                opaque_index += 1
            if opaque_index < len(opaque_units):
                unit = opaque_units[opaque_index]
                if unit.start <= scan_cursor < unit.end:
                    scan_cursor = unit.end
                    opaque_index += 1
                    continue
            if text[scan_cursor] == "%" and not _is_escaped(text, scan_cursor):
                newline = text.find("\n", scan_cursor + 1, until)
                scan_cursor = until if newline == -1 else newline + 1
                continue
            environment = _ENVIRONMENT_SCOPE_RE.match(text, scan_cursor)
            if environment is not None and environment.end() <= until:
                name = environment.group("environment").strip()
                if environment.group("kind") == "begin":
                    environment_stack.append(name)
                elif environment_stack:
                    if environment_stack[-1] == name:
                        environment_stack.pop()
                    elif name in environment_stack:
                        reverse_index = environment_stack[::-1].index(name)
                        del environment_stack[len(environment_stack) - reverse_index - 1 :]
                scan_cursor = environment.end()
                continue
            if text.startswith(r"\begingroup", scan_cursor):
                explicit_group_depth += 1
                scan_cursor += len(r"\begingroup")
                continue
            if text.startswith(r"\endgroup", scan_cursor):
                explicit_group_depth = max(0, explicit_group_depth - 1)
                scan_cursor += len(r"\endgroup")
                continue
            character = text[scan_cursor]
            if character == "{" and not _is_escaped(text, scan_cursor):
                brace_depth += 1
            elif character == "}" and not _is_escaped(text, scan_cursor):
                brace_depth = max(0, brace_depth - 1)
            scan_cursor += 1

    for start, syntax, match in _macro_definition_matches(text):
        if start < scan_cursor:
            continue
        scan_scope(start)
        if brace_depth or explicit_group_depth or environment_stack:
            continue
        parsed = _parse_macro_definition(text, syntax, match)
        if parsed is None:
            continue
        kind, macro, definition_end = parsed
        _apply_body_macro_definition(macros, kind, macro)
        scan_cursor = definition_end
    return macros


def _apply_body_macro_definition(
    macros: dict[str, MacroDefinition],
    kind: str,
    macro: MacroDefinition,
) -> bool:
    if macro.name in _STRUCTURAL_COMMAND_NAMES or macro.name in _COUNTER_REPRESENTATION_NAMES:
        return False
    if kind == "renewcommand" and macro.name not in macros:
        return False
    if kind == "providecommand" and macro.name in macros:
        return False
    macros[macro.name] = macro
    return True


def _body_macro_definition_matches(body: str) -> list[tuple[int, str, re.Match[str]]]:
    return _macro_definition_matches(body)


def _expand_top_level_body_macros(
    body: str,
    preamble_macros: dict[str, MacroDefinition],
) -> str:
    """Expand document-body macros from declaration point without leaking local scope."""

    macros = dict(preamble_macros)
    definitions = _body_macro_definition_matches(body)
    output: list[str] = []
    output_cursor = 0
    scan_cursor = 0
    brace_depth = 0
    explicit_group_depth = 0
    environment_stack: list[str] = []

    def scan_scope(until: int) -> None:
        nonlocal scan_cursor, brace_depth, explicit_group_depth
        while scan_cursor < until:
            if body[scan_cursor] == "%" and not _is_escaped(body, scan_cursor):
                newline = body.find("\n", scan_cursor + 1, until)
                scan_cursor = until if newline == -1 else newline + 1
                continue
            environment = _ENVIRONMENT_SCOPE_RE.match(body, scan_cursor)
            if environment is not None and environment.end() <= until:
                name = environment.group("environment").strip()
                if environment.group("kind") == "begin":
                    environment_stack.append(name)
                elif environment_stack:
                    if environment_stack[-1] == name:
                        environment_stack.pop()
                    elif name in environment_stack:
                        reverse_index = environment_stack[::-1].index(name)
                        del environment_stack[len(environment_stack) - reverse_index - 1 :]
                scan_cursor = environment.end()
                continue
            if body.startswith(r"\begingroup", scan_cursor):
                explicit_group_depth += 1
                scan_cursor += len(r"\begingroup")
                continue
            if body.startswith(r"\endgroup", scan_cursor):
                explicit_group_depth = max(0, explicit_group_depth - 1)
                scan_cursor += len(r"\endgroup")
                continue
            character = body[scan_cursor]
            if character == "{" and not _is_escaped(body, scan_cursor):
                brace_depth += 1
            elif character == "}" and not _is_escaped(body, scan_cursor):
                brace_depth = max(0, brace_depth - 1)
            scan_cursor += 1

    definition_index = 0
    while definition_index < len(definitions):
        start, syntax, match = definitions[definition_index]
        if start < scan_cursor:
            definition_index += 1
            continue
        scan_scope(start)
        top_level = brace_depth == 0 and explicit_group_depth == 0 and not environment_stack
        parsed = _parse_macro_definition(body, syntax, match) if top_level else None
        if parsed is None:
            definition_index += 1
            continue
        kind, macro, definition_end = parsed
        output.append(_expand_safe_macros(body[output_cursor:start], macros))
        definition_source = body[start:definition_end]
        if macro.name in _COUNTER_REPRESENTATION_NAMES:
            # Counter representations are executable document structure.  Preserve the exact
            # declaration so block parsing can apply it at the correct lexical point; treating
            # ``\thesection`` as an author text macro loses resets such as supplementary ``S1``.
            output.append(definition_source)
        else:
            output.append(
                "".join("\n" if char == "\n" else " " for char in definition_source)
            )
            _apply_body_macro_definition(macros, kind, macro)
        output_cursor = definition_end
        scan_cursor = definition_end
        definition_index += 1

    output.append(_expand_safe_macros(body[output_cursor:], macros))
    return "".join(output)


_ISOLATED_RENDER_ENVIRONMENTS = frozenset(
    environment
    for environment, block_type in ENVIRONMENT_TYPES.items()
    if block_type in {"EQUATION", "FIGURE", "TABLE"}
)
_ISOLATED_RENDER_ENVIRONMENT_RE = re.compile(
    r"\\begin\s*\{(?P<environment>"
    + "|".join(
        re.escape(environment)
        for environment in sorted(_ISOLATED_RENDER_ENVIRONMENTS, key=len, reverse=True)
    )
    + r")\}"
)
_EXPLICIT_GROUP_RE = re.compile(r"\\(?P<kind>begin|end)group\b")
_BODY_RENDER_DECLARATION_KINDS = frozenset(
    {
        "DeclareDocumentCommand",
        "DeclareMathAlphabet",
        "DeclareMathOperator",
        "DeclarePairedDelimiter",
        "DeclarePairedDelimiterX",
        "DeclareRobustCommand",
        "NewDocumentCommand",
        "ProvideDocumentCommand",
        "RenewDocumentCommand",
        "addtolength",
        "colorlet",
        "def",
        "definecolor",
        "edef",
        "gdef",
        "let",
        "newcolumntype",
        "newcommand",
        "newenvironment",
        "newlength",
        "newsavebox",
        "providecolor",
        "providecommand",
        "renewcommand",
        "renewenvironment",
        "setlength",
        "tikzset",
        "tikzstyle",
        "xdef",
    }
)
_BODY_RENDER_STATE_KINDS = frozenset(
    {
        "addtolength",
        "colorlet",
        "definecolor",
        "newcolumntype",
        "newlength",
        "newsavebox",
        "providecolor",
        "setlength",
        "tikzset",
        "tikzstyle",
    }
)
_ENVIRONMENT_REQUIRED_ARGUMENTS = {
    "wrapfigure": 2,
    "wraptable": 2,
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "longtable": 1,
}


def _explicit_group_ranges(body: str) -> list[tuple[int, int]]:
    masked = _mask_comment_bodies_for_tex(body)
    stack: list[int] = []
    ranges: list[tuple[int, int]] = []
    for match in _EXPLICIT_GROUP_RE.finditer(masked):
        if match.group("kind") == "begin":
            stack.append(match.end())
        elif stack:
            ranges.append((stack.pop(), match.start()))
    return ranges


def _render_environment_open_end(body: str, match: re.Match[str]) -> int:
    environment = match.group("environment")
    cursor = _skip_whitespace(body, match.end())
    if cursor < len(body) and body[cursor] == "[":
        try:
            _options, cursor = _read_optional_argument(body, cursor)
        except LatexParseError:
            return match.end()
    for _ in range(_ENVIRONMENT_REQUIRED_ARGUMENTS.get(environment, 0)):
        cursor = _skip_whitespace(body, cursor)
        if cursor >= len(body) or body[cursor] != "{":
            return match.end()
        try:
            _argument, cursor = _read_argument(body, cursor)
        except LatexParseError:
            return match.end()
    return cursor


def _inject_body_render_declarations(body: str) -> str:
    r"""Copy lexically active body declarations into independently rendered blocks.

    arXiv sources often define a complex figure helper or table column immediately before its
    use, sometimes inside ``\\begingroup``. Such declarations are valid TeX but are outside the
    document preamble, so a per-block compiler cannot otherwise see them. This pass derives a
    declaration dependency closure from the source itself, respects declaration order and
    explicit group scope, and injects exact source rather than inventing equivalent commands.
    """

    target_ranges: list[tuple[re.Match[str], int]] = []
    cursor = 0
    while match := _ISOLATED_RENDER_ENVIRONMENT_RE.search(body, cursor):
        environment = match.group("environment")
        try:
            _content, end = _find_environment_end(body, match.start(), environment)
        except LatexParseError:
            cursor = match.end()
            continue
        target_ranges.append((match, end))
        cursor = end
    if not target_ranges:
        return body

    scopes = [(0, len(body)), *_explicit_group_ranges(body)]
    scoped_units: list[tuple[int, int, Any]] = []
    mask_ranges: list[tuple[int, int]] = []
    for scope_start, scope_end in scopes:
        source = body[scope_start:scope_end]
        for unit in extract_preamble_units(source):
            if unit.kind not in _BODY_RENDER_DECLARATION_KINDS or unit.unsafe_commands:
                continue
            absolute_start = scope_start + unit.start
            absolute_end = scope_start + unit.end
            if any(
                target.start() < absolute_start < target_end
                for target, target_end in target_ranges
            ):
                # The declaration already belongs to the canonical block and retains the TeX
                # environment's own scope. Never mask it or leak it into a later sibling block.
                continue
            absolute_unit = replace(unit, start=absolute_start, end=absolute_end)
            scoped_units.append((scope_start, scope_end, absolute_unit))
            if not any(
                provided in _COUNTER_REPRESENTATION_NAMES
                for provided in absolute_unit.provides
            ):
                mask_ranges.append((absolute_start, absolute_end))

    insertions: list[tuple[int, str]] = []
    for target, target_end in target_ranges:
        target_start = target.start()
        target_block_type = ENVIRONMENT_TYPES.get(target.group("environment"))
        candidates = [
            unit
            for scope_start, scope_end, unit in scoped_units
            if scope_start <= target_start < scope_end and unit.end <= target_start
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda unit: unit.start)
        latest: dict[str, Any] = {}
        for unit in candidates:
            for provided in unit.provides:
                latest[provided] = unit
        target_source = body[target_start:target_end]
        needed = {
            reference.group(1)
            for reference in re.finditer(r"\\([A-Za-z@]+|.)", target_source)
        }
        needed.update(
            environment.group(1).strip()
            for environment in re.finditer(
                r"\\(?:begin|end)\s*\{([^{}]+)\}", target_source
            )
        )
        selected: dict[int, Any] = {}
        for unit in candidates:
            if unit.kind not in _BODY_RENDER_STATE_KINDS:
                continue
            # Figures and tables depend on ambient layout state (lengths, color palettes,
            # column types and TikZ styles), so retain the active state exactly. Mathematical
            # environments do not: copying every earlier ``\setlength``/``\definecolor`` into
            # their body turns unrelated document layout into invalid KaTeX source. For an
            # equation, include a state declaration only when its declared symbol is actually
            # referenced by the target; ordinary macro dependencies are handled by the closure
            # below. This is dependency selection, not a command-name exception.
            if target_block_type == "EQUATION" and not any(
                provided in needed for provided in unit.provides
            ):
                continue
            if unit.provides and any(latest.get(name) is not unit for name in unit.provides):
                continue
            selected[unit.start] = unit
            needed.update(unit.references)
        changed = True
        while changed:
            changed = False
            for name in tuple(needed):
                unit = latest.get(name)
                if unit is None or unit.start in selected:
                    continue
                selected[unit.start] = unit
                needed.update(unit.references)
                changed = True
        if selected:
            declarations = "\n".join(
                unit.source.strip()
                for unit in sorted(selected.values(), key=lambda item: item.start)
            )
            insertions.append(
                (_render_environment_open_end(body, target), f"\n{declarations}\n")
            )

    if not insertions:
        return body
    rendered = _mask_ranges_preserving_newlines(body, mask_ranges)
    for position, declaration in sorted(insertions, reverse=True):
        rendered = f"{rendered[:position]}{declaration}{rendered[position:]}"
    return rendered


def _extract_theorem_environments(text: str) -> dict[str, str]:
    """Collect safe theorem aliases declared in the document preamble."""
    labels: dict[str, str] = {}
    for match in _NEW_THEOREM_RE.finditer(text):
        environment = match.group("environment")
        if environment in ENVIRONMENT_TYPES:
            continue
        label = _latex_to_text(match.group("label"))
        labels[environment] = label or environment.replace("_", " ").title()
    return labels


def _extract_unnumbered_theorem_environments(text: str) -> set[str]:
    return {
        match.group("environment")
        for match in _NEW_THEOREM_RE.finditer(text)
        if match.group("star")
    }


# These are layout programs rather than semantic prose wrappers. The distinction is made from
# the definition body, never from an author-chosen environment name. A paper can therefore call
# its box ``swchatleft``, ``resultcard`` or anything else without requiring a parser release.
_NATIVE_VISUAL_ENVIRONMENT_PRIMITIVE_RE = re.compile(
    r"\\begin\s*\{(?:tcolorbox|mdframed|framed|shaded|snugshade|tikzpicture|tikzcd|"
    r"pspicture|picture)\}"
    r"|\\(?:tcolorboxenvironment|tcbox|colorbox|fcolorbox|framebox|shadowbox|"
    r"includegraphics|tikz|pspicture)\b",
    re.IGNORECASE,
)
_NATIVE_VISUAL_ENVIRONMENT_FACTORY_RE = re.compile(
    r"\\(?:newtcolorbox|renewtcolorbox|NewTColorBox|DeclareTColorBox)"
    r"(?:\s*\[[^\]]*\])?\s*\{(?P<name>[A-Za-z@][A-Za-z0-9@_*-]*)\}",
    re.DOTALL,
)


def _extract_native_visual_environments(preamble: str) -> set[str]:
    """Discover author-defined environments whose definition is a visual layout program.

    ``extract_preamble_units`` performs balanced declaration parsing, including nested braces in
    ``newenvironment`` bodies. Package-native tcolorbox factories are declarative but have a
    different grammar, so their target names are collected separately.
    """

    environments = {
        name
        for unit in extract_preamble_units(preamble)
        if unit.kind in {"newenvironment", "renewenvironment"}
        and _NATIVE_VISUAL_ENVIRONMENT_PRIMITIVE_RE.search(unit.source) is not None
        for name in unit.provides
        if name
    }
    environments.update(
        match.group("name")
        for match in _NATIVE_VISUAL_ENVIRONMENT_FACTORY_RE.finditer(preamble)
    )
    return environments


_MISSING_OPTIONAL_ARGUMENT = "\ue000ACADEMATEPAPER_NO_VALUE\ue001"
_XPARSE_VALUE_CONDITIONAL_RE = re.compile(
    r"\\(?P<test>IfValue|IfNoValue|IfBoolean)(?P<branch>TF|T|F)"
    r"(?![A-Za-z@])\s*\{"
)


def _resolve_xparse_value_conditionals(
    replacement: str,
    arguments: list[str | None],
) -> str:
    """Evaluate value-presence branches using the parsed optional-argument state."""

    resolved = replacement
    for _ in range(9):
        output: list[str] = []
        cursor = 0
        changed = False
        for match in _XPARSE_VALUE_CONDITIONAL_RE.finditer(resolved):
            if match.start() < cursor:
                continue
            try:
                selector, end = _read_argument(resolved, match.end() - 1)
            except LatexParseError:
                continue
            selector_match = re.fullmatch(r"\s*#([1-9])\s*", selector)
            if selector_match is None:
                continue
            argument_index = int(selector_match.group(1)) - 1
            if argument_index >= len(arguments):
                continue
            try:
                first_branch_start = _skip_whitespace(resolved, end)
                first_branch, end = _read_argument(resolved, first_branch_start)
                second_branch = ""
                if match.group("branch") == "TF":
                    second_branch_start = _skip_whitespace(resolved, end)
                    second_branch, end = _read_argument(resolved, second_branch_start)
            except LatexParseError:
                continue
            has_value = arguments[argument_index] is not None
            predicate = has_value if match.group("test") != "IfNoValue" else not has_value
            branch_kind = match.group("branch")
            if branch_kind == "T":
                selected = first_branch if predicate else ""
            elif branch_kind == "F":
                selected = first_branch if not predicate else ""
            else:
                selected = first_branch if predicate else second_branch
            output.append(resolved[cursor : match.start()])
            output.append(selected)
            cursor = end
            changed = True
        if not changed:
            break
        output.append(resolved[cursor:])
        resolved = "".join(output)
    return resolved


def _read_macro_invocation_arguments(
    text: str,
    cursor: int,
    specifications: tuple[MacroArgument, ...],
) -> tuple[list[str | None], int] | None:
    arguments: list[str | None] = []
    end = cursor
    for specification in specifications:
        end = _skip_whitespace(text, end)
        if specification.kind == "star":
            if end < len(text) and text[end] == "*":
                arguments.append("*")
                end += 1
            else:
                arguments.append(None)
            continue
        if specification.kind == "optional":
            if end < len(text) and text[end] == "[":
                try:
                    argument, end = _read_optional_argument(text, end)
                except LatexParseError:
                    return None
                arguments.append(
                    None if argument == _MISSING_OPTIONAL_ARGUMENT else argument
                )
            else:
                arguments.append(specification.default)
            continue
        if end >= len(text):
            return None
        if text[end] == "{":
            try:
                argument, end = _read_argument(text, end)
            except LatexParseError:
                return None
        elif text[end] == "\\":
            token = re.match(r"\\(?:[A-Za-z@]+|.)", text[end:])
            if token is None:
                return None
            argument = token.group(0)
            end += token.end()
        else:
            argument = text[end]
            end += 1
        arguments.append(argument)
    return arguments, end


def _is_macro_definition_target(prefix: str) -> bool:
    return re.search(
        r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand|"
        r"NewDocumentCommand|RenewDocumentCommand|ProvideDocumentCommand|"
        r"DeclareDocumentCommand|DeclareMathOperator|DeclarePairedDelimiterX?)"
        r"\*?\s*(?:\{\s*)?$|"
        r"\\def\s*$",
        prefix,
    ) is not None


def _is_one_tex_argument_token(value: str) -> bool:
    """Return whether serialized replacement text is already one TeX argument token."""

    stripped = value.strip()
    if not stripped:
        return True
    if stripped.startswith("{"):
        try:
            _argument, end = _read_argument(stripped, 0)
        except LatexParseError:
            return False
        return end == len(stripped)
    if stripped.startswith("\\"):
        return re.fullmatch(r"\\(?:[A-Za-z@]+|.)", stripped, re.DOTALL) is not None
    return len(stripped) == 1


def _preserve_undelimited_macro_argument(
    output: list[str],
    replacement: str,
) -> str:
    """Keep a lowered macro atomic when a standard command consumes one token.

    TeX scans an undelimited argument before expanding it. For example, in
    ``\\overline\\SP`` the token ``\\SP`` is the complete argument even when
    its replacement is ``\\operatorname{SP}``. Plain string substitution
    otherwise produces the invalid ``\\overline\\operatorname{SP}``. Grouping
    only this proven argument position preserves TeX semantics without changing
    the math class of macro expansions in ordinary positions.
    """

    if _is_one_tex_argument_token(replacement):
        return replacement
    output_tail = "".join(output[-64:])
    consumer = re.search(r"\\([A-Za-z@]+)\s*$", output_tail)
    if (
        consumer is None
        or consumer.group(1) not in _STANDARD_UNDELIMITED_ARGUMENT_COMMANDS
    ):
        return replacement
    return f"{{{replacement}}}"


def _substitute_macro_arguments(
    replacement: str,
    arguments: list[str | None],
) -> str:
    r"""Serialize TeX parameter tokens without merging adjacent control words.

    Macro replacement text is tokenized when the definition is read.  Thus the
    ``#1`` in ``\scriptsize#1`` is distinct from the preceding ``\scriptsize``
    token even when the argument begins with letters.  Plain string replacement
    loses that boundary and invents a different command such as
    ``\scriptsizest``.  The same issue occurs on the other side when an argument
    ends in a control word and the replacement continues with letters.

    Insert a TeX-ignored delimiter only at those two proven lexical boundaries.
    Braces are deliberately not added because grouping every argument would
    change the semantics of otherwise valid author macros.
    """

    output: list[str] = []
    cursor = 0
    parameter = re.compile(r"#(?P<index>[1-9])")
    for match in parameter.finditer(replacement):
        index = int(match.group("index")) - 1
        if index >= len(arguments):
            continue
        output.append(replacement[cursor : match.start()])
        argument = (
            _MISSING_OPTIONAL_ARGUMENT
            if arguments[index] is None
            else str(arguments[index])
        )
        output_tail = "".join(output[-64:])
        if argument and re.search(r"\\[A-Za-z@]+$", output_tail) and re.match(
            r"[A-Za-z@]", argument
        ):
            argument = " " + argument
        if (
            argument
            and re.search(r"\\[A-Za-z@]+$", argument)
            and match.end() < len(replacement)
            and re.match(r"[A-Za-z@]", replacement[match.end()])
        ):
            argument += " "
        output.append(argument)
        cursor = match.end()
    output.append(replacement[cursor:])
    return "".join(output)


def _expand_macro_stream(
    text: str,
    macros: dict[str, MacroDefinition],
    expansion_stack: tuple[str, ...],
) -> str:
    """Resolve the transitive dependency closure of each invocation, with cycle bounds."""

    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        match = re.match(r"\\([A-Za-z@]+)", text[cursor:])
        if match is None or match.group(1) not in macros:
            output.append(text[cursor])
            cursor += 1
            continue
        name = match.group(1)
        if name in expansion_stack or len(expansion_stack) >= 24:
            output.append(match.group(0))
            cursor += match.end()
            continue
        if _is_macro_definition_target(text[:cursor]):
            output.append(match.group(0))
            cursor += match.end()
            continue
        macro = macros[name]
        invocation = _read_macro_invocation_arguments(
            text,
            cursor + match.end(),
            macro.arguments,
        )
        if invocation is None:
            output.append(match.group(0))
            cursor += match.end()
            continue
        arguments, end = invocation
        replacement = _resolve_xparse_value_conditionals(macro.replacement, arguments)
        replacement = _substitute_macro_arguments(replacement, arguments)
        replacement = _expand_macro_stream(
            replacement,
            macros,
            (*expansion_stack, name),
        )
        if _MISSING_OPTIONAL_ARGUMENT in replacement:
            # The safe evaluator could not prove how a no-value marker is consumed.
            # Preserve the author's complete invocation for a later deterministic path.
            output.append(text[cursor:end])
            cursor = end
            continue

        replacement = _preserve_undelimited_macro_argument(output, replacement)

        # TeX tokenizes control words before expanding macros. Serializing the
        # expanded token stream back to text must preserve those token boundaries.
        output_tail = "".join(output[-64:])
        if replacement and re.search(r"\\[A-Za-z@]+$", output_tail) and re.match(
            r"[A-Za-z@]", replacement
        ):
            replacement = " " + replacement
        if (
            replacement
            and re.search(r"\\[A-Za-z@]+$", replacement)
            and end < len(text)
            and re.match(r"[A-Za-z@]", text[end])
        ):
            replacement += " "
        output.append(replacement)
        cursor = end
    return "".join(output)


def _expand_safe_macros(text: str, macros: dict[str, MacroDefinition]) -> str:
    """Expand a safe declaration IR recursively without running arbitrary TeX."""

    return re.sub(r"\\xspace\b", " ", _expand_macro_stream(text, macros, ()))


def normalize_formula_with_preamble(latex: str, renderer_preamble: str) -> str:
    """Normalize one formula using the same bounded macros as body parsing.

    Metadata fields such as the arXiv abstract are stored separately from the
    parsed document body, but authors routinely use preamble-defined notation in
    both.  Sharing this entry point prevents metadata and body formulas from
    drifting into two different LaTeX dialects while retaining the parser's
    existing limits on macro size, arguments, recursion, and unsafe commands.
    """

    macros = _extract_macros(renderer_preamble)
    return normalize_katex_latex(
        _expand_safe_macros(latex, macros),
        declared_packages=extract_declared_packages(renderer_preamble),
    )


def _normalize_render_payload_value(
    value: Any,
    macros: dict[str, MacroDefinition],
    declared_packages: tuple[str, ...],
    *,
    block_type: str | None = None,
) -> Any:
    def normalize_value(item: Any) -> Any:
        if isinstance(item, list):
            return [normalize_value(child) for child in item]
        if not isinstance(item, dict):
            return item
        normalized = {key: normalize_value(child) for key, child in item.items()}
        if normalized.get("type") in {"math", "equation"} and isinstance(
            normalized.get("latex"), str
        ):
            normalized["latex"] = normalize_katex_latex(
                _expand_safe_macros(normalized["latex"], macros),
                environment=(
                    str(normalized["environment"])
                    if normalized.get("type") == "equation"
                    and isinstance(normalized.get("environment"), str)
                    else None
                ),
                declared_packages=declared_packages,
            )
        return normalized

    normalized_value = normalize_value(value)
    if (
        block_type == "EQUATION"
        and isinstance(normalized_value, dict)
        and isinstance(normalized_value.get("latex"), str)
    ):
        normalized_value["latex"] = normalize_katex_latex(
            _expand_safe_macros(normalized_value["latex"], macros),
            environment=(
                str(normalized_value["environment"])
                if isinstance(normalized_value.get("environment"), str)
                else None
            ),
            declared_packages=declared_packages,
        )
    return normalized_value


def normalize_render_payload_with_preamble(
    value: Any,
    renderer_preamble: str,
    *,
    block_type: str | None = None,
) -> Any:
    """Rebuild typed math nodes without retranslating or rewriting canonical source fields."""

    return _normalize_render_payload_value(
        value,
        _extract_macros(renderer_preamble),
        extract_declared_packages(renderer_preamble),
        block_type=block_type,
    )


def _apply_declared_katex_capabilities(
    blocks: list[ParsedBlock],
    renderer_preamble: str,
) -> list[ParsedBlock]:
    """Normalize only render-target math after structural parsing.

    Most render payloads also retain canonical block LaTeX for source inspection or local TeX
    rendering.  A recursive pass therefore targets typed math/equation nodes, plus the one
    top-level equation payload shape, and never rewrites those canonical source fields.
    """

    macros = _extract_macros(renderer_preamble)
    declared_packages = extract_declared_packages(renderer_preamble)
    return [
        replace(
            block,
            render_payload=_normalize_render_payload_value(
                block.render_payload,
                macros,
                declared_packages,
                block_type=block.block_type,
            ),
        )
        for block in blocks
    ]


def _apply_math_render_contracts(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """Assign stable identities and an explicit KaTeX-to-TeX fallback contract.

    This phase never claims that a formula is KaTeX-compatible.  The browser is the
    authoritative probe because it runs the exact KaTeX version used for presentation.
    """

    updated: list[ParsedBlock] = []
    for block in blocks:
        nodes: list[dict[str, Any]] = []

        def visit(
            value: Any,
            *,
            path: tuple[str | int, ...],
            target_block: ParsedBlock = block,
            target_nodes: list[dict[str, Any]] = nodes,
        ) -> None:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, path=(*path, index))
                return
            if not isinstance(value, dict):
                return
            is_math = value.get("type") in {"math", "equation"}
            if target_block.block_type == "EQUATION" and not path and isinstance(
                value.get("latex"), str
            ):
                is_math = True
            if is_math and isinstance(value.get("latex"), str):
                latex = str(value["latex"]).strip()
                identity_material = "\0".join(
                    (target_block.content_hash, "/".join(str(part) for part in path), latex)
                )
                target_nodes.append(
                    {
                        "math_node_id": hashlib.sha256(
                            identity_material.encode("utf-8")
                        ).hexdigest()[:32],
                        "path": list(path),
                        "canonical_latex": latex,
                        "katex_latex": latex,
                        "display": bool(
                            value.get("display") or value.get("type") == "equation"
                        ),
                        "status": "probe-required",
                    }
                )
            for key, item in value.items():
                if key not in {"math_render_plan", "latex_render"}:
                    visit(item, path=(*path, key))

        visit(block.render_payload, path=())
        if not nodes:
            updated.append(block)
            continue
        payload = dict(block.render_payload)
        payload["math_render_plan"] = {
            "version": MATH_RENDER_CONTRACT_VERSION,
            "preferred_renderer": "katex",
            "probe": "browser-katex-strict",
            "fallback_renderer": "texlive-svg",
            "fallback_activation": "user-requested",
            "nodes": nodes,
        }
        updated.append(replace(block, render_payload=payload))
    return updated


_LATEX_TEXT_ACCENTS = {
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
_LATEX_TEXT_ACCENT_RE = re.compile(
    r'''\\(?P<accent>['`^"~=.]|[uvHcdbrk](?![A-Za-z@]))\s*'''
    r"(?:\{\s*(?P<braced>\\[ij]|[A-Za-z])\s*\}|(?P<bare>\\[ij]|[A-Za-z]))"
)
_LATEX_TEXT_SYMBOLS = {
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
    "i": "ı",
    "j": "ȷ",
}
_LATEX_TEXT_SYMBOL_RE = re.compile(
    r"\\(?P<symbol>ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j)(?![A-Za-z@])"
)


def _decode_latex_text_characters(value: str) -> str:
    def accent(match: re.Match[str]) -> str:
        base = match.group("braced") or match.group("bare") or ""
        base = {r"\i": "i", r"\j": "j"}.get(base, base)
        return unicodedata.normalize("NFC", base + _LATEX_TEXT_ACCENTS[match.group("accent")])

    text = _LATEX_TEXT_ACCENT_RE.sub(accent, value)
    return _LATEX_TEXT_SYMBOL_RE.sub(
        lambda match: _LATEX_TEXT_SYMBOLS[match.group("symbol")],
        text,
    )


def _latex_to_text(value: str) -> str:
    text = _strip_citation_commands(value)
    text = _decode_latex_text_characters(text)
    text = text.replace(r"\{", "__PM_LBRACE__").replace(r"\}", "__PM_RBRACE__")
    text = _REFERENCE_RE.sub("", text)
    text = _BIBLIOGRAPHY_COMMAND_RE.sub("", text)
    text = _BIBLIOGRAPHY_CONTROL_RE.sub("", text)
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:label|index|hypertarget)\s*\{[^{}]*\}", "", text)
    # Package-level listing configuration changes presentation only.  If it appears in the
    # document body (commonly in a local group before ``\VerbatimInput``), none of its key/value
    # payload is paper prose.  Keep this as a semantic command family instead of patching option
    # names or individual documents.
    text = re.sub(
        r"\\(?:fvset|lstset)(?![A-Za-z@])\s*\{(?:[^{}]|\{[^{}]*\})*\}",
        " ",
        text,
        flags=re.DOTALL,
    )
    for _ in range(3):
        text = re.sub(
            r"\\(?:newgeometry|geometry|setlength|addtolength|setstretch|color|"
            r"vspace|hspace|raisebox)\*?\s*(?:\{[^{}]*\}){1,3}",
            " ",
            text,
        )
    text = re.sub(r"\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\url\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\textcolor\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(
        r"\\(?:multicolumn|multirow)\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{([^{}]*)\}",
        r"\1",
        text,
    )
    for _ in range(4):
        text = re.sub(
            r"\\(?:textbf|textit|textsc|textsf|emph|mathrm|mathbf|mathit|textrm|texttt|"
            r"underline|mbox|operatorname|footnote|path)\s*\{([^{}]*)\}",
            r"\1",
            text,
        )
    text = text.replace(r"\LaTeX", "LaTeX").replace(r"\TeX", "TeX")
    replacements = {
        r"\ie": "i.e.",
        r"\eg": "e.g.",
        r"\etc": "etc.",
        r"\etal": "et al.",
        r"\S": "§",
        r"\%": "%",
        r"\_": "_",
        r"\&": "&",
        r"\#": "#",
        r"\$": "$",
        r"\textbackslash": "",
    }
    for source, target in replacements.items():
        if source[-1].isalpha():
            text = re.sub(rf"{re.escape(source)}(?![A-Za-z@])", target, text)
        else:
            text = text.replace(source, target)
    text = re.sub(r"\\(?:\s+|[,!;:])", " ", text)
    text = re.sub(
        r"\\(?:noindent|centering|raggedright|small|large|Large|LARGE|huge|Huge|"
        r"footnotesize|scriptsize|tiny|normalsize|itshape|bfseries|sffamily|rmfamily)\b",
        "",
        text,
    )
    text = re.sub(r"\\(?:vskip|hskip|vspace|hspace)\*?\s*\{?[^\s{}]+\}?", " ", text)
    text = re.sub(r"\\(?:par|newline|linebreak)\b|\\\\", " ", text)
    # Unknown presentation commands are removed; their braced human-readable content remains.
    text = re.sub(r"\\[A-Za-z@]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("---", "—").replace("--", "–")
    text = text.replace("``", "“").replace("''", "”")
    text = text.replace("__PM_LBRACE__", "{").replace("__PM_RBRACE__", "}")
    text = text.replace("~", " ")
    return re.sub(r"\s+", " ", text).strip()


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _next_math_span(value: str, start: int) -> tuple[int, int, str, bool] | None:
    """Return the next balanced math span without splitting nested text-mode math."""

    search_from = start
    while (opening := _MATH_START_RE.search(value, search_from)) is not None:
        token = opening.group(0)
        if _is_escaped(value, opening.start()):
            search_from = opening.end()
            continue
        closing = {"$": "$", "$$": "$$", r"\(": r"\)", r"\[": r"\]"}[token]
        cursor = opening.end()
        brace_depth = 0
        while cursor < len(value):
            if brace_depth == 0 and value.startswith(r"\begin", cursor):
                nested_environment = re.match(
                    r"\\begin\s*\{(?P<environment>[A-Za-z@][A-Za-z0-9@_*-]*)\}",
                    value[cursor:],
                )
                if nested_environment is not None:
                    environment = nested_environment.group("environment")
                    try:
                        _nested_content, nested_end = _find_environment_end(
                            value,
                            cursor,
                            environment,
                        )
                    except LatexParseError:
                        pass
                    else:
                        # Environments such as ``tabular`` temporarily enter text mode inside an
                        # outer formula and may therefore contain their own ``$...$`` cells. Those
                        # delimiters cannot close the surrounding formula.
                        cursor = nested_end
                        continue
            if value[cursor] == "{" and not _is_escaped(value, cursor):
                brace_depth += 1
                cursor += 1
                continue
            if value[cursor] == "}" and not _is_escaped(value, cursor):
                brace_depth = max(0, brace_depth - 1)
                cursor += 1
                continue
            if brace_depth == 0 and value.startswith(closing, cursor):
                if token.startswith("$") and _is_escaped(value, cursor):
                    cursor += len(closing)
                    continue
                return (
                    opening.start(),
                    cursor + len(closing),
                    value[opening.end() : cursor],
                    token in {"$$", r"\["},
                )
            cursor += 1
        # A malformed opening delimiter must not consume every later formula.
        search_from = opening.end()
    return None


def _rich_text_segments(
    value: str,
    inherited_marks: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    value = normalize_legacy_bold_math(value)
    segments: list[dict[str, Any]] = []

    def append(segment: dict[str, Any]) -> None:
        if inherited_marks:
            segment["marks"] = list(inherited_marks)
        if (
            segment.get("type") == "text"
            and segments
            and segments[-1].get("type") == "text"
            and segments[-1].get("marks", []) == segment.get("marks", [])
        ):
            segments[-1]["text"] = f"{segments[-1]['text']} {segment['text']}".strip()
            return
        segments.append(segment)

    def append_plain(raw: str) -> None:
        text = _latex_to_text(raw)
        if text:
            append({"type": "text", "text": text})

    cursor = 0
    while cursor < len(value):
        math_span = _next_math_span(value, cursor)
        mark_match = _TEXT_MARK_COMMAND_RE.search(value, cursor)
        wrapper_match = _TRANSPARENT_TEXT_WRAPPER_RE.search(value, cursor)
        reference_match = _REFERENCE_COMMAND_RE.search(value, cursor)
        citation_spans = _citation_command_spans(value, cursor)
        citation_span = citation_spans[0] if citation_spans else None
        candidates: list[tuple[int, str, object]] = []
        if math_span is not None:
            candidates.append((math_span[0], "math", math_span))
        if mark_match is not None:
            candidates.append((mark_match.start(), "mark", mark_match))
        if wrapper_match is not None:
            candidates.append((wrapper_match.start(), "wrapper", wrapper_match))
        if reference_match is not None:
            candidates.append((reference_match.start(), "reference", reference_match))
        if citation_span is not None:
            candidates.append((int(citation_span["start"]), "citation", citation_span))
        if not candidates:
            append_plain(value[cursor:])
            break
        match_start, match_kind, match_value = min(candidates, key=lambda candidate: candidate[0])
        append_plain(value[cursor:match_start])
        if match_kind == "math":
            _span_start, span_end, raw_latex, display = match_value  # type: ignore[misc]
            latex = normalize_katex_latex(raw_latex)
            if latex:
                append(
                    {
                        "type": "math",
                        "latex": latex,
                        "display": display,
                    }
                )
            cursor = span_end
            continue
        match = match_value
        if match_kind == "citation":
            options = list(match["options"])
            prefix = options[0] if len(options) == 2 else None
            suffix = match.get("postnote")
            if suffix is None:
                suffix = options[-1] if options else None
            append(
                {
                    "type": "citation",
                    "command": match["command"],
                    "starred": match["starred"],
                    "keys": list(match["keys"]),
                    "prefix": _latex_to_text(prefix) if prefix is not None else None,
                    "suffix": _latex_to_text(suffix) if suffix is not None else None,
                    "raw_latex": match["raw_latex"],
                    "text": "[?]",
                    "status": "PENDING",
                }
            )
            cursor = int(match["end"])
            continue
        if match_kind == "reference":
            labels = [label.strip() for label in match.group("labels").split(",") if label.strip()]
            for label_index, label in enumerate(labels):
                if label_index:
                    append({"type": "text", "text": ", "})
                append(
                    {
                        "type": "reference",
                        "command": match.group("command"),
                        "label": label,
                        "text": "??",
                        "status": "PENDING",
                    }
                )
            cursor = match.end()
            continue
        if match_kind == "wrapper":
            argument_cursor = match.end() - 1
            arguments: list[str] = []
            wrapper_end = match.end()
            try:
                for argument_index in range(
                    _TRANSPARENT_TEXT_WRAPPER_ARGUMENTS[match.group("command")]
                ):
                    argument, wrapper_end = _read_argument(value, argument_cursor)
                    arguments.append(argument)
                    if argument_index + 1 < _TRANSPARENT_TEXT_WRAPPER_ARGUMENTS[
                        match.group("command")
                    ]:
                        argument_cursor = wrapper_end
                        while argument_cursor < len(value) and value[argument_cursor].isspace():
                            argument_cursor += 1
                        if argument_cursor >= len(value) or value[argument_cursor] != "{":
                            raise LatexParseError(
                                f"Missing content argument for \\{match.group('command')}"
                            )
            except LatexParseError:
                append_plain(value[match.start() : match.end()])
                cursor = match.end()
                continue
            segments.extend(_rich_text_segments(arguments[-1], inherited_marks))
            cursor = wrapper_end
            continue
        try:
            argument, end = _read_argument(value, match.end() - 1)
        except LatexParseError:
            append_plain(value[match.start() : match.end()])
            cursor = match.end()
            continue
        mark = _TEXT_MARKS[match.group("command")]
        nested_marks = (*inherited_marks, mark) if mark not in inherited_marks else inherited_marks
        segments.extend(_rich_text_segments(argument, nested_marks))
        cursor = end
    return segments


def _text_render_payload(value: str, **extra: Any) -> tuple[str | None, dict[str, Any]]:
    segments = _rich_text_segments(value)
    # Preserve the historical canonical-text contract: references are semantic
    # navigation elements, not selector text.  Their visible number lives in the
    # structured segment and is restored separately in translations.
    plain_parts = [
        str(segment.get("text") or segment.get("latex") or "")
        for segment in segments
        if segment.get("type") not in {"citation", "reference"}
    ]
    text = re.sub(r"\s+", " ", " ".join(plain_parts)).strip()
    text = re.sub(r"\s+([,.;:!?%)\]}])", r"\1", text)
    text = re.sub(r"([{\[])\s+", r"\1", text)
    payload: dict[str, Any] = {"text": text, "segments": segments}
    payload.update(extra)
    return text or None, payload


def render_latex_prose(value: str) -> tuple[str | None, dict[str, Any]]:
    """Lower compiler-produced bibliography prose into the reader's safe rich-text contract."""

    normalized = re.sub(r"\\newblock\b", " ", value)
    return _text_render_payload(normalized)


def _find_environment_end(text: str, start: int, environment: str) -> tuple[str, int]:
    token_re = re.compile(
        rf"\\(?P<kind>begin|end)\s*\{{{re.escape(environment)}\}}",
        re.DOTALL,
    )
    depth = 0
    content_start: int | None = None
    for match in token_re.finditer(text, start):
        if match.group("kind") == "begin":
            depth += 1
            if content_start is None:
                content_start = match.end()
        else:
            depth -= 1
            if depth == 0 and content_start is not None:
                return text[content_start : match.start()], match.end()
    raise LatexParseError(f"Unclosed LaTeX environment: {environment}")


def _command_argument(text: str, command: str) -> str | None:
    match = re.search(rf"\\{command}\s*\{{", text)
    if match is None:
        return None
    try:
        argument, _ = _read_argument(text, match.end() - 1)
    except LatexParseError:
        return None
    return argument


def _caption_command(text: str) -> tuple[str, int, int] | None:
    matches = [
        match
        for pattern in (
            r"\\caption\*?\s*(?:\[[^\]]*\]\s*)?\{",
            r"\\captionof\*?\s*\{[^{}]+\}\s*(?:\[[^\]]*\]\s*)?\{",
        )
        if (match := re.search(pattern, text)) is not None
    ]
    if not matches:
        return None
    match = min(matches, key=lambda item: item.start())
    try:
        argument, end = _read_argument(text, match.end() - 1)
    except LatexParseError:
        return None
    return argument, match.start(), end


def _graphic_paths(text: str) -> list[str]:
    match = re.search(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})+)\s*\}", text)
    if match is None:
        return []
    return [
        PurePosixPath(item.strip()).as_posix().rstrip("/")
        for item in re.findall(r"\{([^{}]*)\}", match.group(1))
        if item.strip()
    ]


def _asset_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("path", "")): item
        for item in manifest.get("files", [])
        if str(item.get("path", "")).lower().endswith(_IMAGE_EXTENSIONS)
    }


def _project_file_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("path", "")): item
        for item in manifest.get("files", [])
        if str(item.get("path", "")) and str(item.get("storage_key", ""))
    }


def _resolve_project_file(
    target: str,
    *,
    root_file: str,
    files: dict[str, dict[str, Any]],
) -> str | None:
    """Resolve a project-local path using TeX's root working directory semantics.

    Resolution is manifest-only: absolute paths and traversal outside the project are rejected,
    and case-insensitive recovery is accepted only when it identifies one unique archive member.
    No extension is invented because verbatim/listing commands address arbitrary file types.
    """

    cleaned = target.strip().replace("\\", "/")
    candidate = PurePosixPath(cleaned)
    if not candidate.parts or candidate.is_absolute():
        return None

    def bounded_path(path: PurePosixPath) -> str | None:
        parts: list[str] = []
        for part in path.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
                continue
            parts.append(part)
        return PurePosixPath(*parts).as_posix() if parts else None

    # The root document's directory is TeX's ordinary compilation working directory.  A direct
    # candidate also covers archives that wrap every source file in one top-level folder while
    # retaining paths authored relative to that folder.
    candidates = [
        path
        for path in (
            bounded_path(PurePosixPath(root_file).parent / candidate),
            bounded_path(candidate),
        )
        if path is not None
    ]
    resolved = next((path for path in candidates if path in files), None)
    if resolved is not None:
        return resolved
    for path in candidates:
        matches = [item for item in files if item.casefold() == path.casefold()]
        if len(matches) == 1:
            return matches[0]
    # A uniquely wrapped archive is safe to recover, but ambiguous basenames never bind.
    suffix = cleaned.casefold().lstrip("/")
    matches = [
        path
        for path in files
        if path.casefold() == suffix or path.casefold().endswith(f"/{suffix}")
    ]
    return matches[0] if len(matches) == 1 else None


def _external_code_inputs(
    body: str,
    *,
    root_file: str,
    manifest: dict[str, Any],
    storage: Storage,
    warnings: list[str],
) -> dict[int, dict[str, Any]]:
    """Load only literal project files explicitly referenced by supported code commands."""

    files = _project_file_index(manifest)
    loaded: dict[str, tuple[str, str]] = {}
    inputs: dict[int, dict[str, Any]] = {}
    for token in _EXTERNAL_CODE_COMMAND_TOKEN_RE.finditer(body):
        if _is_escaped(body, token.start()):
            continue
        try:
            invocation = _external_code_invocation(
                body,
                start=token.start(),
                command=token.group("command"),
                command_end=token.end(),
            )
        except LatexParseError:
            continue
        target = str(invocation["target"])
        source_path = _resolve_project_file(
            target,
            root_file=root_file,
            files=files,
        )
        if source_path is None:
            invocation.update(
                {
                    "status": "MISSING",
                    "source_path": None,
                    "code": "",
                    "error": f"The source manifest does not contain {target}",
                }
            )
            warnings.append(
                f"Missing external code input for \\{invocation['command']}: {target}"
            )
            inputs[token.start()] = invocation
            continue
        if source_path not in loaded:
            item = files[source_path]
            with storage.get(str(item["storage_key"])) as stream:
                data = stream.read()
            if b"\x00" in data:
                invocation.update(
                    {
                        "status": "INVALID",
                        "source_path": source_path,
                        "code": "",
                        "error": "The referenced project file is binary rather than text.",
                    }
                )
                warnings.append(f"Binary external code input ignored: {source_path}")
                inputs[token.start()] = invocation
                continue
            loaded[source_path] = (_decode_source(data), hashlib.sha256(data).hexdigest())
        code, digest = loaded[source_path]
        invocation.update(
            {
                "status": "RESOLVED",
                "source_path": source_path,
                "code": code,
                "sha256": digest,
                "mime_type": files[source_path].get("mime_type") or "text/plain",
                "byte_size": files[source_path].get("byte_size"),
            }
        )
        inputs[token.start()] = invocation
    return inputs


def _resolve_asset(
    target: str,
    *,
    root_file: str,
    assets: dict[str, dict[str, Any]],
    graphic_paths: list[str],
) -> dict[str, Any] | None:
    cleaned = target.strip().replace("\\", "/")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute():
        return None

    def bounded_path(path: PurePosixPath) -> str | None:
        parts: list[str] = []
        for part in path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
                continue
            parts.append(part)
        return PurePosixPath(*parts).as_posix() if parts else None

    bases = [PurePosixPath(root_file).parent, *(PurePosixPath(path) for path in graphic_paths)]
    candidates: list[str] = []
    for base in bases:
        joined = bounded_path(base / candidate)
        if joined is None:
            continue
        candidates.append(joined)
        if candidate.suffix == "":
            candidates.extend(f"{joined}{extension}" for extension in _IMAGE_EXTENSIONS)
    direct = bounded_path(candidate)
    if direct is not None:
        candidates.append(direct)
        if candidate.suffix == "":
            candidates.extend(f"{direct}{extension}" for extension in _IMAGE_EXTENSIONS)
    resolved_path = next((path for path in candidates if path in assets), None)
    if resolved_path is None:
        # Some arXiv sources were authored or packaged on case-insensitive file
        # systems and therefore contain references whose casing differs from the
        # archived asset (for example ``dockq.pdf`` versus ``DockQ.pdf``).  Linux
        # containers correctly treat those as different paths.  Recover only a
        # unique case-folded match so an ambiguous archive can never bind the
        # figure to the wrong source asset.
        for path in candidates:
            casefold_matches = [
                asset_path
                for asset_path in assets
                if asset_path.casefold() == path.casefold()
            ]
            if len(casefold_matches) == 1:
                resolved_path = casefold_matches[0]
                break
    if resolved_path is None:
        suffix_matches = [
            path
            for path in assets
            if path == cleaned
            or path.endswith(f"/{cleaned}")
            or (
                candidate.suffix == ""
                and any(path.endswith(f"/{cleaned}{ext}") for ext in _IMAGE_EXTENSIONS)
            )
        ]
        if len(suffix_matches) == 1:
            resolved_path = suffix_matches[0]
    if resolved_path is None:
        cleaned_casefold = cleaned.casefold()
        suffix_matches = [
            path
            for path in assets
            if path.casefold() == cleaned_casefold
            or path.casefold().endswith(f"/{cleaned_casefold}")
            or (
                candidate.suffix == ""
                and any(
                    path.casefold().endswith(f"/{cleaned_casefold}{ext}")
                    for ext in _IMAGE_EXTENSIONS
                )
            )
        ]
        if len(suffix_matches) == 1:
            resolved_path = suffix_matches[0]
    if resolved_path is None:
        return None
    item = assets[resolved_path]
    return {
        "path": resolved_path,
        "mime_type": item.get("mime_type") or "application/octet-stream",
        "byte_size": item.get("byte_size"),
    }


def _figure_assets(
    content: str,
    *,
    root_file: str,
    assets: dict[str, dict[str, Any]],
    graphic_paths: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for match in re.finditer(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", content):
        target = match.group(1)
        asset = _resolve_asset(
            target,
            root_file=root_file,
            assets=assets,
            graphic_paths=graphic_paths,
        )
        if asset is None:
            missing.append(target)
        elif asset not in resolved:
            resolved.append(asset)
    return resolved, missing


def prepare_includegraphics_assets(
    content: str,
    *,
    root_file: str,
    manifest: dict[str, Any],
    preamble: str = "",
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Resolve and rewrite includegraphics targets against the source manifest.

    Rendering happens in an isolated temporary directory rather than the expanded
    source tree. Rewriting each safe target to its canonical manifest path lets
    the caller stage only the referenced files while preserving graphicspath,
    extension-less references, and nested source-file layouts.
    """
    assets = _asset_index(manifest)
    graphic_paths = _graphic_paths(preamble)
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        asset = _resolve_asset(
            target,
            root_file=root_file,
            assets=assets,
            graphic_paths=graphic_paths,
        )
        if asset is None:
            missing.append(target)
            return match.group(0)
        manifest_item = assets[asset["path"]]
        render_item = {
            **asset,
            "storage_key": manifest_item.get("storage_key"),
            "requested_path": target,
        }
        if not any(item["path"] == render_item["path"] for item in resolved):
            resolved.append(render_item)
        command = match.group(0)
        argument_start = command.rfind("{")
        return f"{command[: argument_start + 1]}{asset['path']}}}"

    pattern = re.compile(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
    return pattern.sub(replace, content), resolved, missing


def _split_latex(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if separator == r"\\" and value[index : index + 2] == r"\\":
                parts.append(value[start:index])
                index += 2
                start = index
                continue
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
        elif separator == "&" and character == "&" and depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts


def _table_rows(content: str) -> tuple[list[list[str]], list[list[list[dict[str, Any]]]]]:
    match = re.search(
        r"\\begin\s*\{(?:tabular\*?|tabularx|longtable)\}(?:\s*\{[^{}]*\}){1,2}(.*?)"
        r"\\end\s*\{(?:tabular\*?|tabularx|longtable)\}",
        content,
        re.DOTALL,
    )
    table = match.group(1) if match else content
    caption_command = _caption_command(table)
    if caption_command is not None:
        _caption, start, end = caption_command
        table = f"{table[:start]} {table[end:]}"
    table = re.sub(
        r"\\(?:toprule|midrule|bottomrule|hline|addlinespace|endhead|endfirsthead|endfoot|endlastfoot)\b",
        lambda _match: r"\\",
        table,
    )
    table = re.sub(
        r"\\(?:cmidrule|cline)\s*(?:\([^)]*\))?\s*\{[^{}]*\}",
        lambda _match: r"\\",
        table,
    )
    rows: list[list[str]] = []
    row_segments: list[list[list[dict[str, Any]]]] = []
    for raw_row in _split_latex(table, r"\\"):
        parsed_cells = [_text_render_payload(cell) for cell in _split_latex(raw_row, "&")]
        visible_cells = [(text, payload) for text, payload in parsed_cells if text]
        if visible_cells:
            rows.append([str(text) for text, _payload in visible_cells])
            row_segments.append([payload["segments"] for _text, payload in visible_cells])
    return rows, row_segments


def _split_tabbing_cells(value: str) -> list[str]:
    """Split one ``tabbing`` row at top-level ``\\>`` tab movements.

    Tabbing entries are not ampersand-delimited table cells.  Keeping this scanner separate
    from ``_split_latex`` prevents a broad replacement of ``\\>`` in prose or math while still
    producing searchable fallback text for layout blocks.
    """

    cells: list[str] = []
    start = 0
    depth = 0
    index = 0
    while index < len(value):
        if value.startswith(r"\>", index) and depth == 0:
            cells.append(value[start:index])
            index += 2
            start = index
            continue
        character = value[index]
        if character == "\\":
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
        index += 1
    cells.append(value[start:])
    return cells


def _tabbing_render_payload(content: str) -> tuple[str | None, dict[str, Any]]:
    """Build a readable fallback while leaving visual layout to the TeX engine.

    A row ending in ``\\kill`` exists only to establish tab stops and must never be exposed as
    paper content.  Visible rows retain empty leading cells so continuation lines keep their
    semantic column position in the structured fallback.
    """

    rows: list[list[str]] = []
    row_segments: list[list[list[dict[str, Any]]]] = []
    for raw_row in _split_latex(content, r"\\"):
        raw_row = re.sub(r"^\s*\[[^\]]*\]", "", raw_row)
        # ``\kill`` itself terminates the non-printing measurement row; authors normally do
        # not follow it with ``\\``.  Discard only the prefix through the last ``\kill`` so the
        # first visible entry after it is not lost from the same splitter chunk.
        kill_matches = list(re.finditer(r"\\kill(?![A-Za-z@])", raw_row))
        if kill_matches:
            raw_row = raw_row[kill_matches[-1].end() :]
        parsed_cells = [_text_render_payload(cell) for cell in _split_tabbing_cells(raw_row)]
        texts = [str(text or "") for text, _payload in parsed_cells]
        segments = [payload["segments"] for _text, payload in parsed_cells]
        while texts and not texts[-1]:
            texts.pop()
            segments.pop()
        if not any(texts):
            continue
        rows.append(texts)
        row_segments.append(segments)

    lines = [" — ".join(cell for cell in row if cell) for row in rows]
    text = "\n".join(line for line in lines if line).strip() or None
    return text, {
        "text": text,
        "segments": ([{"type": "text", "text": text}] if text else []),
        "rows": rows,
        "row_segments": row_segments,
        "environment": "tabbing",
        "layout_kind": "tabbing",
        "presentation": "native-latex-visual",
        "latex": content.strip(),
    }


def _list_render_payload(environment: str, content: str) -> tuple[str | None, dict[str, Any]]:
    raw_items = re.split(r"\\item(?:\[[^\]]*\])?", content)[1:]
    item_payloads = [_text_render_payload(item)[1] for item in raw_items]
    visible_items = [item for item in item_payloads if item["text"]]
    items = [str(item["text"]) for item in visible_items]
    return "\n".join(items) or None, {
        "items": items,
        "item_segments": [item["segments"] for item in visible_items],
        "environment": environment,
    }


def _statement_content(content: str) -> tuple[str | None, str]:
    cursor = 0
    while cursor < len(content) and content[cursor].isspace():
        cursor += 1
    if cursor >= len(content) or content[cursor] != "[":
        return None, content
    try:
        title, end = _read_optional_argument(content, cursor)
    except LatexParseError:
        return None, content
    return title, content[end:]


def _statement_parts(content: str) -> list[dict[str, Any]]:
    nested_types = {
        name: block_type
        for name, block_type in ENVIRONMENT_TYPES.items()
        if block_type in {"EQUATION", "LIST"}
    }
    nested_names = "|".join(re.escape(name) for name in sorted(nested_types, key=len, reverse=True))
    nested_re = re.compile(rf"\\begin\s*\{{(?P<environment>{nested_names})\}}", re.DOTALL)
    parts: list[dict[str, Any]] = []

    def add_prose(raw: str) -> None:
        for paragraph in re.split(r"\n\s*\n", raw):
            rendered, payload = _text_render_payload(paragraph)
            if rendered:
                parts.append(
                    {
                        "type": "prose",
                        "text": rendered,
                        "segments": payload["segments"],
                    }
                )

    cursor = 0
    while match := nested_re.search(content, cursor):
        environment = match.group("environment")
        add_prose(content[cursor : match.start()])
        nested, end = _find_environment_end(content, match.start(), environment)
        block_type = nested_types[environment]
        if block_type == "EQUATION":
            labels = [item.group("label").strip() for item in _LABEL_RE.finditer(nested)]
            tag_match = _TAG_RE.search(nested)
            latex = normalize_katex_latex(nested)
            if latex:
                part: dict[str, Any] = {
                    "type": "equation",
                    "latex": latex,
                    "environment": environment,
                }
                if labels:
                    part["labels"] = labels
                if tag_match is not None:
                    part["explicit_tag"] = _latex_to_text(tag_match.group("tag"))
                parts.append(part)
        else:
            _text, payload = _list_render_payload(environment, nested)
            if payload["items"]:
                parts.append(
                    {
                        "type": "list",
                        "environment": environment,
                        "items": payload["items"],
                        "item_segments": payload["item_segments"],
                    }
                )
        cursor = end
    add_prose(content[cursor:])
    return parts


def _statement_render_payload(
    environment: str,
    content: str,
    display_name: str,
) -> tuple[str | None, dict[str, Any]]:
    raw_title, body = _statement_content(content)
    rendered_title, title_payload = (
        _text_render_payload(raw_title) if raw_title else (None, {"segments": []})
    )
    parts = _statement_parts(body)
    rendered_parts = [str(part.get("text", "")) for part in parts if part["type"] == "prose"]
    rendered_body = " ".join(part for part in rendered_parts if part).strip() or None
    source_text = " ".join(part for part in (rendered_title, rendered_body) if part).strip() or None
    return source_text, {
        "text": rendered_body,
        "segments": [
            segment for part in parts if part["type"] == "prose" for segment in part["segments"]
        ],
        "parts": parts,
        "title": rendered_title,
        "title_segments": title_payload["segments"],
        "display_name": display_name,
        "environment": environment,
        "latex": content.strip(),
    }


def _environment_payload(
    environment: str,
    content: str,
    *,
    root_file: str,
    assets: dict[str, dict[str, Any]],
    graphic_paths: list[str],
    theorem_labels: dict[str, str],
    unnumbered_theorems: set[str],
    block_type: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    block_type = block_type or ENVIRONMENT_TYPES.get(environment, "THEOREM")
    if block_type == "PARAGRAPH":
        return _text_render_payload(content, environment=environment)
    if block_type == "LIST":
        return _list_render_payload(environment, content)
    if block_type == "CODE":
        code = content.strip()
        return code or None, {"code": code, "environment": environment}
    if block_type == "EQUATION":
        tag_match = _TAG_RE.search(content)
        latex = normalize_katex_latex(content, environment=environment)
        payload = {
            "latex": latex,
            "display": True,
            "environment": environment,
        }
        if tag_match is not None:
            payload["explicit_tag"] = _latex_to_text(tag_match.group("tag"))
        return latex or None, payload
    if block_type == "THEOREM":
        source_text, payload = _statement_render_payload(
            environment,
            content,
            theorem_labels.get(environment, environment.replace("_", " ").title()),
        )
        payload["numbered"] = environment not in unnumbered_theorems
        return source_text, payload
    if block_type == "ALGORITHM":
        caption_command = _caption_command(content)
        caption = caption_command[0] if caption_command else None
        body = (
            f"{content[: caption_command[1]]} {content[caption_command[2] :]}"
            if caption_command
            else content
        )
        # Float placement and line-number options remain canonical render metadata, but they are
        # not readable algorithm content and must not appear as literal ``[htbp] [1]`` tokens.
        body = re.sub(r"^\s*\[[^\]]*\]", "", body, count=1)
        body = re.sub(
            r"(\\begin\s*\{algorithmic\})\s*\[[^\]]*\]",
            r"\1",
            body,
        )
        rendered_body, payload = _text_render_payload(body, environment=environment)
        rendered_caption, caption_payload = (
            _text_render_payload(caption) if caption else (None, {"segments": []})
        )
        payload.update(
            {
                "caption": rendered_caption,
                "caption_segments": caption_payload["segments"],
                "latex": content.strip(),
            }
        )
        source_text = (
            " ".join(part for part in (rendered_caption, rendered_body) if part).strip() or None
        )
        return source_text, payload
    if block_type == "LATEX_BLOCK":
        if environment == "tabbing":
            return _tabbing_render_payload(content)
        source_text, payload = _text_render_payload(content, environment=environment)
        payload.update(
            {
                "environment": environment,
                "latex": content.strip(),
                "presentation": "native-latex-visual",
            }
        )
        return source_text, payload
    caption_command = _caption_command(content)
    caption = caption_command[0] if caption_command else None
    rendered_caption, caption_payload = (
        _text_render_payload(caption) if caption else (None, {"segments": []})
    )
    payload: dict[str, Any] = {
        "latex": content.strip(),
        "caption": rendered_caption,
        "caption_segments": caption_payload["segments"],
        "environment": environment,
    }
    if block_type == "FIGURE":
        resolved, missing = _figure_assets(
            content,
            root_file=root_file,
            assets=assets,
            graphic_paths=graphic_paths,
        )
        payload.update({"assets": resolved, "missing_assets": missing})
        if environment == "tikzcd":
            payload.update(
                {
                    "embedded_diagram": True,
                    "semantic_kind": "diagram",
                    "numbered": False,
                }
            )
    elif block_type == "TABLE":
        rows, row_segments = _table_rows(content)
        resolved, missing = _figure_assets(
            content,
            root_file=root_file,
            assets=assets,
            graphic_paths=graphic_paths,
        )
        payload.update(
            {
                "rows": rows,
                "row_segments": row_segments,
                "structured": bool(rows),
                "assets": resolved,
                "missing_assets": missing,
            }
        )
    return rendered_caption, payload


def _content_hash(block_type: str, source: str) -> str:
    return hashlib.sha256(f"{block_type}\0{source.strip()}".encode()).hexdigest()


def _parse_blocks(
    body: str,
    root_file: str,
    *,
    assets: dict[str, dict[str, Any]],
    graphic_paths: list[str],
    environment_types: dict[str, str],
    theorem_labels: dict[str, str],
    unnumbered_theorems: set[str],
    external_code_inputs: dict[int, dict[str, Any]],
) -> list[ParsedBlock]:
    pending: list[dict[str, Any]] = []
    section_stack: list[tuple[int, str]] = []
    next_base_ordinal = 0
    bibliography_boundary_count = 0
    appendix_mode = False
    pending_counter_directives: list[dict[str, Any]] = []

    def bibliography_boundary_ordinal() -> str:
        """Insert a source marker without renumbering later content blocks.

        Parser-derived block IDs include ``ordinal_path``.  A bibliography is a
        structural boundary rather than translated paper prose, so placing it
        in the lexical gap after the preceding block preserves IDs, annotations
        and completed translations for every following appendix block.
        """

        nonlocal bibliography_boundary_count
        bibliography_boundary_count += 1
        if next_base_ordinal == 0:
            return f"!bibliography.{bibliography_boundary_count:08d}"
        return (
            f"{next_base_ordinal - 1:08d}.bibliography."
            f"{bibliography_boundary_count:08d}"
        )

    def add(
        block_type: str,
        raw: str,
        source_text: str | None,
        render_payload: dict[str, Any],
        offset: int,
        *,
        heading_level: int | None = None,
        environment: str | None = None,
        ordinal_path: str | None = None,
        source_end_offset: int | None = None,
        source_payload_extra: dict[str, Any] | None = None,
        content_hash_source: str | None = None,
    ) -> None:
        nonlocal next_base_ordinal
        stripped = raw.strip()
        if not stripped:
            return
        if ordinal_path is None:
            ordinal_path = f"{next_base_ordinal:08d}"
            next_base_ordinal += 1
        parent = section_stack[-1][1] if section_stack else None
        source_payload: dict[str, Any] = {"latex": stripped}
        if environment:
            source_payload["environment"] = environment
        if source_payload_extra:
            source_payload.update(source_payload_extra)
        payload = dict(render_payload)
        if pending_counter_directives:
            # Semantic controls have no visible block of their own. Attach them to the next
            # readable block so the numbering pass encounters them in exact source order.
            payload["_counter_directives"] = deepcopy(pending_counter_directives)
            pending_counter_directives.clear()
        pending.append(
            {
                "block_type": block_type,
                "ordinal_path": ordinal_path,
                "source_text": source_text,
                "source_payload": source_payload,
                "render_payload": payload,
                "source_locator": {
                    "root_file": root_file,
                    "expanded_line": body.count("\n", 0, offset) + 1,
                    "expanded_offset_start": offset,
                    "expanded_offset_end": (
                        source_end_offset if source_end_offset is not None else offset + len(raw)
                    ),
                },
                "content_hash": _content_hash(
                    block_type,
                    content_hash_source if content_hash_source is not None else stripped,
                ),
                "heading_level": heading_level,
                "parent_ordinal_path": parent,
            }
        )
        if block_type == "SECTION" and heading_level is not None:
            while section_stack and section_stack[-1][0] >= heading_level:
                section_stack.pop()
            pending[-1]["parent_ordinal_path"] = section_stack[-1][1] if section_stack else None
            section_stack.append((heading_level, ordinal_path))

    def add_paragraph(raw: str, offset: int) -> None:
        for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", raw, re.DOTALL):
            paragraph = match.group(0).strip()
            rendered, render_payload = _text_render_payload(paragraph)
            segments = render_payload.get("segments", [])
            has_semantic_node = any(
                isinstance(segment, dict)
                and segment.get("type") in {"citation", "math", "reference"}
                for segment in segments
            )
            if not rendered or (not has_semantic_node and re.search(r"\w", rendered) is None):
                # A display container around a table often leaves only its trailing ``.`` or
                # ``,`` after the nested table becomes a typed block. That punctuation belongs to
                # the display and must not become a standalone reading/translation block.
                continue
            if re.fullmatch(
                r"\\begin\s*\{center\}[\s\S]*\\end\s*\{center\}",
                paragraph,
            ):
                render_payload["presentation"] = "center"
                centered_segments = render_payload.get("segments", [])
                substantive_segments = [
                    segment
                    for segment in centered_segments
                    if not (
                        isinstance(segment, dict)
                        and segment.get("type") == "text"
                        and re.fullmatch(
                            r"[\s,.;:!?]*",
                            str(segment.get("text", "")),
                        )
                    )
                ]
                # A center environment is display presentation even when legacy source uses
                # single-dollar math. Promote only a single substantive formula; centered prose
                # or several adjacent inline formulas keep their original inline semantics.
                if (
                    len(substantive_segments) == 1
                    and isinstance(substantive_segments[0], dict)
                    and substantive_segments[0].get("type") == "math"
                ):
                    formula_segment = substantive_segments[0]
                    formula_segment["latex"] = "".join(
                        str(segment.get("latex", ""))
                        if segment is formula_segment
                        else str(segment.get("text", ""))
                        for segment in centered_segments
                        if isinstance(segment, dict)
                    )
                    formula_segment["display"] = True
                    render_payload["segments"] = [formula_segment]
            add("PARAGRAPH", paragraph, rendered, render_payload, offset + match.start())

    theorem_environments = {
        name for name, block_type in environment_types.items() if block_type == "THEOREM"
    }
    nested_environment_re = re.compile(
        r"\\begin\s*\{(?P<environment>[A-Za-z@][A-Za-z0-9@_*-]*)\}",
        re.DOTALL,
    )

    def nested_lift_spans(content: str) -> list[dict[str, Any]]:
        """Find direct nested statements and typed LaTeX-native figures.

        Ordinary nested environments remain opaque so a theorem inside a list keeps its
        original ownership. Direct environments already classified as ``FIGURE`` are lifted so
        their source is never flattened into prose and the normal asset/TeX rendering pipeline can
        handle them. ``tikzcd`` inside an equation wrapper retains its equation semantics. Nested
        statements are handled recursively and discover their own direct figures.
        """

        spans: list[dict[str, Any]] = []
        cursor = 0
        while match := nested_environment_re.search(content, cursor):
            environment = match.group("environment")
            try:
                nested_content, end = _find_environment_end(
                    content, match.start(), environment
                )
            except LatexParseError:
                # A literal or malformed begin token must not turn an otherwise
                # readable outer statement into a hard parse failure.
                cursor = match.end()
                continue
            if environment in theorem_environments:
                spans.append(
                    {
                        "kind": "nested",
                        "start": match.start(),
                        "content_start": match.end(),
                        "end": end,
                        "environment": environment,
                        "content": nested_content,
                    }
                )
            elif environment == "tikzcd":
                span_start = match.start()
                span_end = end
                prefix = re.search(r"\\\[\s*$", content[:span_start], re.DOTALL)
                suffix = re.match(r"\s*\\\]", content[span_end:], re.DOTALL)
                if prefix is not None and suffix is not None:
                    span_start = prefix.start()
                    span_end += suffix.end()
                spans.append(
                    {
                        "kind": "visual",
                        "start": span_start,
                        "end": span_end,
                        "environment": "tikzcd",
                        "source": content[match.start() : end],
                        "payload_content": nested_content,
                        "wrapper_environment": "displaymath" if prefix is not None else None,
                        "numbered": False,
                    }
                )
            elif environment_types.get(environment) == "FIGURE":
                spans.append(
                    {
                        "kind": "visual",
                        "start": match.start(),
                        "end": end,
                        "environment": environment,
                        "source": content[match.start() : end],
                        "payload_content": nested_content,
                        "wrapper_environment": None,
                        "numbered": False,
                    }
                )
            elif environment in {"equation", "equation*", "displaymath"}:
                diagram_match = re.search(r"\\begin\s*\{tikzcd\}", nested_content)
                if diagram_match is not None:
                    try:
                        _diagram_content, diagram_end = _find_environment_end(
                            nested_content,
                            diagram_match.start(),
                            "tikzcd",
                        )
                    except LatexParseError:
                        cursor = end
                        continue
                    tag_match = _TAG_RE.search(nested_content)
                    spans.append(
                        {
                            "kind": "visual",
                            "start": match.start(),
                            "end": end,
                            "environment": "tikzcd",
                            "source": nested_content[diagram_match.start() : diagram_end],
                            "payload_content": nested_content[
                                diagram_match.end() : diagram_end
                                - len(r"\end{tikzcd}")
                            ],
                            "wrapper_environment": environment,
                            "numbered": environment == "equation",
                            "explicit_tag": (
                                _latex_to_text(tag_match.group("tag"))
                                if tag_match is not None
                                else None
                            ),
                        }
                    )
            # Skip every balanced non-theorem environment as an opaque unit.
            # This makes lifting direct-only and protects equations, lists,
            # tables, code listings, and package-specific wrappers.
            cursor = end
        return spans

    def emit_theorem_tree(
        *,
        environment: str,
        content: str,
        content_offset: int,
        source_start: int,
        source_end: int,
        base_ordinal_path: str,
        original_parent_environment: str | None = None,
    ) -> None:
        spans = nested_lift_spans(content)
        if not spans:
            source_text, render_payload = _environment_payload(
                environment,
                content,
                root_file=root_file,
                assets=assets,
                graphic_paths=graphic_paths,
                theorem_labels=theorem_labels,
                unnumbered_theorems=unnumbered_theorems,
            )
            if original_parent_environment is not None:
                render_payload["nested_normalization"] = {
                    "original_parent_environment": original_parent_environment,
                }
            add(
                "THEOREM",
                rf"\begin{{{environment}}}{content}\end{{{environment}}}",
                source_text,
                render_payload,
                source_start,
                environment=environment,
                ordinal_path=base_ordinal_path,
                source_end_offset=source_end,
            )
            return

        pieces: list[dict[str, Any]] = []
        cursor = 0
        for span in spans:
            before = content[cursor : span["start"]]
            if before.strip():
                pieces.append(
                    {
                        "kind": "outer",
                        "content": before,
                        "start": cursor,
                        "end": span["start"],
                    }
                )
            pieces.append({"kind": "nested", **span})
            cursor = int(span["end"])
        tail = content[cursor:]
        if tail.strip():
            pieces.append(
                {
                    "kind": "outer",
                    "content": tail,
                    "start": cursor,
                    "end": len(content),
                }
            )

        outer_fragment_index = 0
        for piece_index, piece in enumerate(pieces):
            ordinal_path = (
                base_ordinal_path
                if piece_index == 0
                else f"{base_ordinal_path}.{piece_index:08d}"
            )
            piece_start = content_offset + int(piece["start"])
            piece_end = content_offset + int(piece["end"])
            if piece["kind"] == "nested":
                emit_theorem_tree(
                    environment=str(piece["environment"]),
                    content=str(piece["content"]),
                    content_offset=content_offset + int(piece["content_start"]),
                    source_start=piece_start,
                    source_end=piece_end,
                    base_ordinal_path=ordinal_path,
                    original_parent_environment=environment,
                )
                continue

            if piece["kind"] == "visual":
                visual_environment = str(piece["environment"])
                visual_source = str(piece["source"])
                visual_text, render_payload = _environment_payload(
                    visual_environment,
                    str(piece.get("payload_content", visual_source)),
                    root_file=root_file,
                    assets=assets,
                    graphic_paths=graphic_paths,
                    theorem_labels=theorem_labels,
                    unnumbered_theorems=unnumbered_theorems,
                )
                render_payload["embedded_in_environment"] = environment
                if visual_environment == "tikzcd":
                    render_payload.update(
                        {
                            "semantic_kind": (
                                "equation" if piece.get("numbered") else "diagram"
                            ),
                            "numbered": bool(piece.get("numbered")),
                            "wrapper_environment": piece.get("wrapper_environment"),
                        }
                    )
                    explicit_tag = piece.get("explicit_tag")
                    if isinstance(explicit_tag, str) and explicit_tag.strip():
                        render_payload["explicit_tag"] = explicit_tag
                        render_payload["semantic_kind"] = "equation"
                        render_payload["numbered"] = True
                add(
                    "FIGURE",
                    visual_source,
                    visual_text,
                    render_payload,
                    piece_start,
                    environment=visual_environment,
                    ordinal_path=ordinal_path,
                    source_end_offset=piece_end,
                )
                continue

            source_text, render_payload = _environment_payload(
                environment,
                str(piece["content"]),
                root_file=root_file,
                assets=assets,
                graphic_paths=graphic_paths,
                theorem_labels=theorem_labels,
                unnumbered_theorems=unnumbered_theorems,
            )
            render_payload["semantic_group_ordinal"] = base_ordinal_path
            render_payload["continuation"] = outer_fragment_index > 0
            render_payload["nested_environment_count"] = len(spans)
            add(
                "THEOREM",
                rf"\begin{{{environment}}}{piece['content']}\end{{{environment}}}",
                source_text,
                render_payload,
                piece_start,
                environment=environment,
                ordinal_path=ordinal_path,
                source_end_offset=piece_end,
            )
            outer_fragment_index += 1

    special_re = _special_re(environment_types)
    math_ranges: list[tuple[int, int]] = []
    math_cursor = 0
    while math_span := _next_math_span(body, math_cursor):
        math_ranges.append((math_span[0], math_span[1]))
        math_cursor = math_span[1]

    math_range_index = 0

    def inside_math(offset: int) -> bool:
        nonlocal math_range_index
        while (
            math_range_index < len(math_ranges)
            and math_ranges[math_range_index][1] <= offset
        ):
            math_range_index += 1
        return (
            math_range_index < len(math_ranges)
            and math_ranges[math_range_index][0] < offset < math_ranges[math_range_index][1]
        )

    cursor = 0
    search_cursor = 0
    while match := special_re.search(body, search_cursor):
        if inside_math(match.start()):
            search_cursor = match.end()
            continue
        display_container = match.group("display_container")
        display_container_content: str | None = None
        display_container_end: int | None = None
        display_caption_kind: str | None = None
        if display_container is not None:
            try:
                display_container_content, display_container_end = _find_environment_end(
                    body,
                    match.start(),
                    display_container,
                )
            except LatexParseError:
                search_cursor = match.end()
                continue
            captionof_match = re.search(
                r"\\captionof\*?\s*\{(?P<kind>figure|table)\}",
                display_container_content,
                re.IGNORECASE,
            )
            display_caption_kind = (
                captionof_match.group("kind").casefold()
                if captionof_match is not None
                else None
            )
            if (
                re.search(r"\\includegraphics(?![A-Za-z@])", display_container_content)
                is None
                and display_caption_kind is None
            ):
                # Ordinary centered prose/math and centered nested environments retain the
                # existing parser path. Advancing only the search cursor lets a nested table or
                # equation remain discoverable without consuming or duplicating its wrapper.
                search_cursor = match.end()
                continue
        add_paragraph(body[cursor : match.start()], cursor)
        if section_name := match.group("section"):
            title, end = _read_argument(body, match.end() - 1)
            rendered_title, title_payload = _text_render_payload(title)
            add(
                "SECTION",
                body[match.start() : end],
                rendered_title,
                {
                    "text": rendered_title or "",
                    "segments": title_payload["segments"],
                    "command": section_name,
                    "numbered": match.group("section_star") is None,
                    "document_region": "appendix" if appendix_mode else "main",
                },
                match.start(),
                heading_level=SECTION_LEVELS[section_name],
            )
            cursor = end
        elif match.group("bibliography"):
            declared_files, end = _read_argument(body, match.end() - 1)
            raw = body[match.start() : end]
            section_stack.clear()
            add(
                "BIBLIOGRAPHY",
                raw,
                "References",
                {
                    "text": "References",
                    "segments": [{"type": "text", "text": "References"}],
                    "command": "bibliography",
                    "declared_source_names": [
                        name.strip()
                        for name in declared_files.split(",")
                        if name.strip()
                    ],
                    "document_region": "appendix" if appendix_mode else "main",
                },
                match.start(),
                heading_level=2,
                ordinal_path=bibliography_boundary_ordinal(),
                source_end_offset=end,
            )
            cursor = end
        elif match.group("printbibliography"):
            end = match.end()
            raw = body[match.start() : end]
            section_stack.clear()
            add(
                "BIBLIOGRAPHY",
                raw,
                "References",
                {
                    "text": "References",
                    "segments": [{"type": "text", "text": "References"}],
                    "command": "printbibliography",
                    "document_region": "appendix" if appendix_mode else "main",
                },
                match.start(),
                heading_level=2,
                ordinal_path=bibliography_boundary_ordinal(),
                source_end_offset=end,
            )
            cursor = end
        elif match.group("appendix"):
            appendix_mode = True
            section_stack.clear()
            cursor = match.end()
        elif match.group("counter_set"):
            pending_counter_directives.append(
                {
                    "operation": "set",
                    "counter": str(match.group("counter_set_name")),
                    "value": int(str(match.group("counter_set_value"))),
                }
            )
            cursor = match.end()
        elif match.group("counter_adjust"):
            pending_counter_directives.append(
                {
                    "operation": "add",
                    "counter": str(match.group("counter_adjust_name")),
                    "value": int(str(match.group("counter_adjust_value"))),
                }
            )
            cursor = match.end()
        elif match.group("counter_format"):
            replacement, end = _read_argument(body, match.end() - 1)
            pending_counter_directives.append(
                {
                    "operation": "format",
                    "counter": str(
                        match.group("counter_format_braced")
                        or match.group("counter_format_bare")
                    ),
                    "template": replacement.strip(),
                }
            )
            cursor = end
        elif display_container is not None:
            assert display_container_content is not None
            assert display_container_end is not None
            block_type = "TABLE" if display_caption_kind == "table" else "FIGURE"
            source_text, render_payload = _environment_payload(
                display_container,
                display_container_content,
                root_file=root_file,
                assets=assets,
                graphic_paths=graphic_paths,
                theorem_labels=theorem_labels,
                unnumbered_theorems=unnumbered_theorems,
                block_type=block_type,
            )
            render_payload.update(
                {
                    "display_container": display_container,
                    "caption_command": (
                        f"captionof{{{display_caption_kind}}}"
                        if display_caption_kind is not None
                        else None
                    ),
                }
            )
            add(
                block_type,
                body[match.start() : display_container_end],
                source_text,
                render_payload,
                match.start(),
                environment=display_container,
                source_end_offset=display_container_end,
            )
            cursor = display_container_end
        elif external_command := match.group("external_code_command"):
            invocation = external_code_inputs.get(match.start())
            if invocation is None:
                # A malformed invocation remains visible to the ordinary prose parser instead
                # of aborting the whole paper.  Well-formed commands are indexed and resolved
                # before block parsing, so this path is diagnostic-only.
                search_cursor = match.end()
                continue
            end = int(invocation["end"])
            raw = body[match.start() : end]
            status = str(invocation.get("status") or "MISSING")
            code = str(invocation.get("code") or "")
            source_path = invocation.get("source_path")
            render_payload: dict[str, Any] = {
                "code": code,
                "environment": external_command,
                "command": external_command,
                "requested_path": str(invocation.get("target") or ""),
                "source_path": source_path,
                "external_source_status": status,
            }
            for key in ("options", "language", "mime_type", "byte_size", "error"):
                value = invocation.get(key)
                if value is not None:
                    render_payload[key] = value
            digest = str(invocation.get("sha256") or "")
            add(
                "CODE",
                raw,
                code or None,
                render_payload,
                match.start(),
                environment=external_command,
                source_end_offset=end,
                source_payload_extra={
                    "external_source": {
                        "requested_path": str(invocation.get("target") or ""),
                        "resolved_path": source_path,
                        "sha256": digest or None,
                        "status": status,
                    }
                },
                content_hash_source=f"{raw}\0{digest}\0{status}",
            )
            cursor = end
        elif match.group("pdf_document"):
            target, end = _read_argument(body, match.end() - 1)
            raw = body[match.start() : end]
            option_match = re.search(r"\[([^\]]*)\]", raw)
            options = option_match.group(1).strip() if option_match else ""
            page_spec = next(
                (
                    value.strip()
                    for item in options.split(",")
                    if "=" in item
                    for key, value in [item.split("=", 1)]
                    if key.strip().lower() == "pages"
                ),
                "1",
            )
            asset = _resolve_asset(
                target,
                root_file=root_file,
                assets=assets,
                graphic_paths=graphic_paths,
            )
            render_payload: dict[str, Any] = {
                "asset": asset,
                "requested_path": target.strip(),
                "page_spec": page_spec,
                "options": options,
            }
            if asset is None:
                render_payload["missing_asset"] = target.strip()
            add(
                "PDF_DOCUMENT",
                raw,
                asset["path"] if asset else target.strip(),
                render_payload,
                match.start(),
            )
            cursor = end
        else:
            environment = match.group("environment")
            content, end = _find_environment_end(body, match.start(), environment)
            block_type = environment_types[environment]
            if block_type == "BIBLIOGRAPHY":
                section_stack.clear()
                add(
                    "BIBLIOGRAPHY",
                    body[match.start() : match.end()],
                    "References",
                    {
                        "text": "References",
                        "segments": [{"type": "text", "text": "References"}],
                        "command": "thebibliography",
                        "document_region": "appendix" if appendix_mode else "main",
                    },
                    match.start(),
                    heading_level=2,
                    environment=environment,
                    ordinal_path=bibliography_boundary_ordinal(),
                    source_end_offset=end,
                )
            else:
                if block_type == "THEOREM" and nested_lift_spans(content):
                    base_ordinal_path = f"{next_base_ordinal:08d}"
                    next_base_ordinal += 1
                    emit_theorem_tree(
                        environment=environment,
                        content=content,
                        content_offset=match.end(),
                        source_start=match.start(),
                        source_end=end,
                        base_ordinal_path=base_ordinal_path,
                    )
                    cursor = end
                    search_cursor = cursor
                    continue
                if block_type == "EQUATION":
                    diagram_match = re.search(r"\\begin\s*\{tikzcd\}", content)
                    if diagram_match is not None:
                        try:
                            _diagram_content, diagram_end = _find_environment_end(
                                content,
                                diagram_match.start(),
                                "tikzcd",
                            )
                        except LatexParseError:
                            diagram_end = 0
                        if diagram_end:
                            diagram_source = content[diagram_match.start() : diagram_end]
                            _source_text, render_payload = _environment_payload(
                                "tikzcd",
                                diagram_source,
                                root_file=root_file,
                                assets=assets,
                                graphic_paths=graphic_paths,
                                theorem_labels=theorem_labels,
                                unnumbered_theorems=unnumbered_theorems,
                            )
                            tag_match = _TAG_RE.search(content)
                            numbered = environment == "equation"
                            render_payload.update(
                                {
                                    "semantic_kind": "equation" if numbered else "diagram",
                                    "numbered": numbered,
                                    "wrapper_environment": environment,
                                }
                            )
                            if tag_match is not None:
                                render_payload["explicit_tag"] = _latex_to_text(
                                    tag_match.group("tag")
                                )
                                render_payload["semantic_kind"] = "equation"
                                render_payload["numbered"] = True
                            add(
                                "FIGURE",
                                diagram_source,
                                None,
                                render_payload,
                                match.start(),
                                environment="tikzcd",
                                source_end_offset=end,
                            )
                            cursor = end
                            search_cursor = cursor
                            continue
                source_text, render_payload = _environment_payload(
                    environment,
                    content,
                    root_file=root_file,
                    assets=assets,
                    graphic_paths=graphic_paths,
                    theorem_labels=theorem_labels,
                    unnumbered_theorems=unnumbered_theorems,
                    block_type=block_type,
                )
                add(
                    block_type,
                    body[match.start() : end],
                    source_text,
                    render_payload,
                    match.start(),
                    environment=environment,
                )
            cursor = end
        search_cursor = cursor
    add_paragraph(body[cursor:], cursor)
    return [ParsedBlock(**item) for item in pending]


def _alphabetic_counter(value: int) -> str:
    """Return LaTeX-style upper-case alphabetic appendix counters."""

    result = ""
    current = value
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _roman_counter(value: int) -> str:
    if value <= 0:
        return str(value)
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    remaining = value
    output: list[str] = []
    for amount, symbol in numerals:
        while remaining >= amount:
            output.append(symbol)
            remaining -= amount
    return "".join(output)


def _styled_counter_value(style: str, value: int) -> str:
    if style == "arabic":
        return str(value)
    if style == "Alph":
        return _alphabetic_counter(value)
    if style == "alph":
        return _alphabetic_counter(value).lower()
    if style == "Roman":
        return _roman_counter(value)
    if style == "roman":
        return _roman_counter(value).lower()
    return str(value)


_COUNTER_VALUE_COMMAND_RE = re.compile(
    r"\\(?P<style>arabic|Alph|alph|Roman|roman)\s*"
    r"\{(?P<counter>[A-Za-z@]+)\}"
)
_COUNTER_REFERENCE_COMMAND_RE = re.compile(r"\\the(?P<counter>[A-Za-z@]+)")


def _render_counter_representation(
    counter: str,
    *,
    values: dict[str, int],
    formats: dict[str, str],
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """Evaluate the safe, display-only subset of LaTeX counter formatting.

    This intentionally interprets only standard counter projections and references to other
    ``\the...`` representations. Unknown executable commands cause a fallback to application
    numbering instead of being guessed or rewritten.
    """

    if counter in seen:
        return None
    template = formats.get(counter)
    if template is None:
        return None
    active_seen = seen | {counter}

    def replace_value(match: re.Match[str]) -> str:
        target = match.group("counter")
        return _styled_counter_value(match.group("style"), values.get(target, 0))

    def replace_reference(match: re.Match[str]) -> str:
        target = match.group("counter")
        nested = _render_counter_representation(
            target,
            values=values,
            formats=formats,
            seen=active_seen,
        )
        return nested if nested is not None else str(values.get(target, 0))

    rendered = _COUNTER_VALUE_COMMAND_RE.sub(replace_value, template)
    rendered = _COUNTER_REFERENCE_COMMAND_RE.sub(replace_reference, rendered)
    rendered = re.sub(r"\\(?:protect|relax)(?![A-Za-z@])", "", rendered)
    rendered = rendered.replace("{", "").replace("}", "").strip()
    if "\\" in rendered or not rendered:
        return None
    return rendered


def _apply_document_reference_index(
    blocks: list[ParsedBlock],
    body: str,
) -> list[ParsedBlock]:
    """Assign deterministic application numbering and resolve rich-text references.

    This is deliberately a semantic pass over the complete expanded document.  Isolated
    KaTeX/LaTeX renderers may strip reference-control commands only after this pass has captured
    their meaning; they never synthesize document counter state independently.
    """

    payloads = {block.ordinal_path: deepcopy(block.render_payload) for block in blocks}
    block_by_ordinal = {block.ordinal_path: block for block in blocks}
    section_numbers: dict[str, str | None] = {}
    section_child_counts: dict[tuple[str, str | None], int] = {}
    kind_counts: dict[str, int] = {}
    statement_counts: dict[str | None, int] = {}
    statement_groups: dict[str, tuple[str, str | None]] = {}
    equation_count = 0
    counter_values = {counter: 0 for counter in _DISPLAY_COUNTER_NAMES}
    counter_formats: dict[str, str] = {}
    active_document_region = "main"
    targets: list[dict[str, Any]] = []
    nested_targets: dict[tuple[str, str], dict[str, Any]] = {}

    def nearest_section(ordinal: str | None) -> str | None:
        cursor = ordinal
        while cursor is not None:
            if cursor in section_numbers:
                return cursor
            parent = block_by_ordinal.get(cursor)
            cursor = parent.parent_ordinal_path if parent is not None else None
        return None

    def add_target(
        block: ParsedBlock,
        *,
        kind: str,
        display_name: str,
        display_number: str | None,
        path: list[str | int] | None = None,
        numbering: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = {
            "ordinal_path": block.ordinal_path,
            "target_path": path or [],
            "kind": kind,
            "display_name": display_name,
            "display_number": display_number,
            "labels": [],
        }
        targets.append(target)
        if numbering is None:
            numbering = {
                "kind": kind,
                "display_name": display_name,
                "display_number": display_number,
                "labels": target["labels"],
                "status": "NUMBERED" if display_number else "UNNUMBERED",
            }
            payloads[block.ordinal_path]["numbering"] = numbering
        else:
            numbering.update(
                {
                    "kind": kind,
                    "display_name": display_name,
                    "display_number": display_number,
                    "labels": target["labels"],
                    "status": "NUMBERED" if display_number else "UNNUMBERED",
                }
            )
        return target

    def apply_counter_directives(block: ParsedBlock, payload: dict[str, Any]) -> None:
        nonlocal equation_count
        directives = payload.pop("_counter_directives", [])
        if not isinstance(directives, list):
            return
        for directive in directives:
            if not isinstance(directive, dict):
                continue
            counter = str(directive.get("counter") or "")
            if counter not in _DISPLAY_COUNTER_NAMES:
                continue
            operation = directive.get("operation")
            if operation == "format":
                template = directive.get("template")
                if isinstance(template, str) and template.strip():
                    counter_formats[counter] = template.strip()
                continue
            value = directive.get("value")
            if not isinstance(value, int):
                continue
            current = counter_values.get(counter, 0)
            updated = value if operation == "set" else current + value
            counter_values[counter] = updated
            if counter in {"figure", "table", "algorithm"}:
                kind_counts[counter] = updated
            elif counter == "equation":
                equation_count = updated
            elif counter in SECTION_LEVELS:
                parent = (
                    None
                    if counter in {"part", "chapter", "section"}
                    else nearest_section(block.parent_ordinal_path)
                )
                section_child_counts[(active_document_region, parent)] = updated

    for block in blocks:
        payload = payloads[block.ordinal_path]
        if block.block_type == "SECTION":
            active_document_region = str(payload.get("document_region") or "main")
        apply_counter_directives(block, payload)
        if block.block_type == "SECTION":
            parent_section = nearest_section(block.parent_ordinal_path)
            document_region = str(payload.get("document_region") or "main")
            numbered = payload.get("numbered") is not False
            counter_key = (document_region, parent_section)
            if numbered:
                section_child_counts[counter_key] = section_child_counts.get(counter_key, 0) + 1
                own_index = section_child_counts[counter_key]
                command = str(payload.get("command") or "section")
                counter_values[command] = own_index
                for descendant, descendant_level in SECTION_LEVELS.items():
                    if descendant_level > SECTION_LEVELS.get(command, block.heading_level or 2):
                        counter_values[descendant] = 0
                explicit_number = _render_counter_representation(
                    command,
                    values=counter_values,
                    formats=counter_formats,
                )
                parent_number = section_numbers.get(parent_section)
                if explicit_number is not None:
                    number = explicit_number
                else:
                    own = (
                        _alphabetic_counter(own_index)
                        if document_region == "appendix" and parent_section is None
                        else str(own_index)
                    )
                    number = f"{parent_number}.{own}" if parent_number else own
            else:
                number = None
            section_numbers[block.ordinal_path] = number
            is_appendix_root = document_region == "appendix" and parent_section is None
            add_target(
                block,
                kind="appendix" if is_appendix_root else "section",
                display_name="Appendix" if is_appendix_root else "Section",
                display_number=number,
            )
        elif block.block_type == "FIGURE" and payload.get("embedded_diagram") is True:
            semantic_kind = str(payload.get("semantic_kind") or "diagram")
            explicit = payload.get("explicit_tag")
            if semantic_kind == "equation":
                has_explicit_tag = isinstance(explicit, str) and bool(explicit.strip())
                if has_explicit_tag or payload.get("numbered") is True:
                    equation_count += 1
                    counter_values["equation"] = equation_count
                    formatted_equation = _render_counter_representation(
                        "equation",
                        values=counter_values,
                        formats=counter_formats,
                    )
                    number = (
                        str(explicit)
                        if has_explicit_tag
                        else formatted_equation or str(equation_count)
                    )
                else:
                    number = None
                numbering: dict[str, Any] = {}
                add_target(
                    block,
                    kind="equation",
                    display_name="Equation",
                    display_number=number,
                    numbering=numbering,
                )
                payload["numbering"] = numbering
                if has_explicit_tag:
                    numbering["status"] = "EXPLICIT_TAG"
            else:
                add_target(
                    block,
                    kind="diagram",
                    display_name="Diagram",
                    display_number=None,
                )
        elif block.block_type in {"FIGURE", "TABLE", "ALGORITHM"}:
            kind = block.block_type.lower()
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            counter_values[kind] = kind_counts[kind]
            formatted_number = _render_counter_representation(
                kind,
                values=counter_values,
                formats=counter_formats,
            )
            add_target(
                block,
                kind=kind,
                display_name=kind.title(),
                display_number=formatted_number or str(kind_counts[kind]),
            )
        elif block.block_type == "EQUATION":
            explicit = payload.get("explicit_tag")
            is_unnumbered = str(payload.get("environment") or "").endswith("*")
            if is_unnumbered and not (isinstance(explicit, str) and explicit.strip()):
                number = None
            else:
                equation_count += 1
                counter_values["equation"] = equation_count
                formatted_equation = _render_counter_representation(
                    "equation",
                    values=counter_values,
                    formats=counter_formats,
                )
                number = (
                    str(explicit)
                    if isinstance(explicit, str) and explicit.strip()
                    else formatted_equation or str(equation_count)
                )
            numbering: dict[str, Any] = {}
            add_target(
                block,
                kind="equation",
                display_name="Equation",
                display_number=number,
                numbering=numbering,
            )
            payload["numbering"] = numbering
            if isinstance(explicit, str) and explicit.strip():
                numbering["status"] = "EXPLICIT_TAG"
        elif block.block_type == "THEOREM":
            environment = str(payload.get("environment") or "statement")
            display_name = str(payload.get("display_name") or environment.replace("_", " ").title())
            is_unnumbered = environment == "proof" or payload.get("numbered") is False
            section = nearest_section(block.parent_ordinal_path)
            semantic_group = str(payload.get("semantic_group_ordinal") or block.ordinal_path)
            existing_group = statement_groups.get(semantic_group)
            if existing_group is not None:
                display_name, number = existing_group
            elif is_unnumbered:
                number = None
                statement_groups[semantic_group] = (display_name, number)
            else:
                statement_counts[section] = statement_counts.get(section, 0) + 1
                local = str(statement_counts[section])
                section_number = section_numbers.get(section)
                number = f"{section_number}.{local}" if section_number else local
                statement_groups[semantic_group] = (display_name, number)
            add_target(
                block,
                kind="statement",
                display_name=display_name,
                display_number=number,
            )
            parts = payload.get("parts")
            if isinstance(parts, list):
                for part_index, part in enumerate(parts):
                    if not isinstance(part, dict) or part.get("type") != "equation":
                        continue
                    explicit = part.get("explicit_tag")
                    part_is_unnumbered = str(part.get("environment") or "").endswith("*")
                    if part_is_unnumbered and not (
                        isinstance(explicit, str) and explicit.strip()
                    ):
                        number = None
                    else:
                        equation_count += 1
                        counter_values["equation"] = equation_count
                        formatted_equation = _render_counter_representation(
                            "equation",
                            values=counter_values,
                            formats=counter_formats,
                        )
                        number = (
                            str(explicit)
                            if isinstance(explicit, str) and explicit.strip()
                            else formatted_equation or str(equation_count)
                        )
                    numbering = {}
                    target = add_target(
                        block,
                        kind="equation",
                        display_name="Equation",
                        display_number=number,
                        path=["parts", part_index],
                        numbering=numbering,
                    )
                    if isinstance(explicit, str) and explicit.strip():
                        numbering["status"] = "EXPLICIT_TAG"
                    part["numbering"] = numbering
                    labels = part.get("labels")
                    if isinstance(labels, list):
                        for label in labels:
                            if isinstance(label, str) and label.strip():
                                nested_targets[(block.ordinal_path, label.strip())] = target

    target_ranges = [
        (
            int(block.source_locator.get("expanded_offset_start", 0)),
            int(block.source_locator.get("expanded_offset_end", 0)),
            target,
        )
        for target in targets
        if not target["target_path"]
        for block in [block_by_ordinal[target["ordinal_path"]]]
    ]
    label_targets: dict[str, list[dict[str, Any]]] = {}
    for label_match in _LABEL_RE.finditer(body):
        label = label_match.group("label").strip()
        if not label:
            continue
        position = label_match.start()
        containing = [
            item for item in target_ranges if item[0] <= position <= item[1]
        ]
        target = containing[-1][2] if containing else None
        if target is None:
            preceding = [item for item in target_ranges if item[0] <= position]
            target = preceding[-1][2] if preceding else None
        if target is None:
            continue
        nested = nested_targets.get((target["ordinal_path"], label))
        if nested is not None:
            target = nested
        occurrences = label_targets.setdefault(label, [])
        identity = (target["ordinal_path"], tuple(target["target_path"]))
        if not any(
            (item["ordinal_path"], tuple(item["target_path"])) == identity
            for item in occurrences
        ):
            occurrences.append(target)
        if label not in target["labels"]:
            target["labels"].append(label)

    def resolved_segment(segment: dict[str, Any]) -> dict[str, Any]:
        if segment.get("type") != "reference":
            return segment
        result = dict(segment)
        label = str(result.get("label") or "").strip()
        command = str(result.get("command") or "ref")
        if command == "pageref":
            result.update({"status": "UNSUPPORTED", "text": "??", "reason": "PAGE_LAYOUT"})
            return result
        matches = label_targets.get(label, [])
        if not matches:
            result.update({"status": "UNRESOLVED", "text": "??"})
            return result
        if len(matches) != 1:
            result.update({"status": "AMBIGUOUS", "text": "??"})
            return result
        target = matches[0]
        number = target.get("display_number")
        if not isinstance(number, str) or not number:
            result.update({"status": "UNNUMBERED", "text": "??"})
            return result
        name = str(target.get("display_name") or target.get("kind") or "Reference")
        if command == "eqref":
            text = f"({number})"
        elif command == "autoref":
            text = f"{name} {number}"
        elif command == "cref":
            text = f"{name.lower()} {number}"
        elif command == "Cref":
            text = f"{name} {number}"
        else:
            text = number
        result.update(
            {
                "status": "RESOLVED",
                "text": text,
                "target_ordinal_path": target["ordinal_path"],
                "target_path": list(target["target_path"]),
                "target_kind": target["kind"],
                "display_name": name,
                "display_number": number,
            }
        )
        return result

    def resolve_value(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve_value(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") == "reference":
                return resolved_segment(value)
            result = {key: resolve_value(item) for key, item in value.items()}
            if value.get("type") in {"math", "equation"} and isinstance(
                value.get("latex"), str
            ):

                def replace_math_reference(match: re.Match[str]) -> str:
                    command = match.group("command")
                    labels = [
                        label.strip()
                        for label in match.group("labels").split(",")
                        if label.strip()
                    ]
                    rendered: list[str] = []
                    for label in labels:
                        reference = resolved_segment(
                            {"type": "reference", "command": command, "label": label}
                        )
                        text = str(reference.get("text") or "??")
                        named = re.fullmatch(r"(?P<name>[A-Za-z][A-Za-z ]*) (?P<number>\S+)", text)
                        if named is not None:
                            rendered.append(
                                rf"\text{{{named.group('name')} }}{named.group('number')}"
                            )
                        else:
                            rendered.append(text)
                    return ", ".join(rendered) or "??"

                result["latex"] = _REFERENCE_COMMAND_RE.sub(
                    replace_math_reference,
                    str(value["latex"]),
                )
            return result
        return value

    return [
        replace(block, render_payload=resolve_value(payloads[block.ordinal_path]))
        for block in blocks
    ]


def parse_latex_project(manifest: dict[str, Any], storage: Storage) -> ParsedDocument:
    sources: dict[str, str] = {}
    for item in manifest.get("files", []):
        path = str(item.get("path", ""))
        key = str(item.get("storage_key", ""))
        if PurePosixPath(path).suffix.lower() not in {
            ".tex",
            ".ltx",
            ".sty",
            ".cls",
            ".def",
        } or not key:
            continue
        with storage.get(key) as stream:
            sources[path] = _decode_source(stream.read())
    document_sources = {
        path: text
        for path, text in sources.items()
        if PurePosixPath(path).suffix.lower() in {".tex", ".ltx"}
    }
    root_file = discover_root_file(document_sources)
    warnings: list[str] = []
    include_edges: list[dict[str, Any]] = []
    expanded, source_map = _expand_source_with_map(
        root_file,
        sources,
        warnings,
        include_edges=include_edges,
    )
    renderer_expanded = _expand_source(
        root_file,
        sources,
        [],
        preserve_tex_comments=True,
    )
    bibliography_index = _bibliography_project_index(
        expanded=expanded,
        root_file=root_file,
        manifest=manifest,
    )
    structural_preamble = _expand_local_package_declarations(
        _document_preamble(expanded),
        sources=sources,
        root_file=root_file,
    )
    renderer_preamble = _document_preamble(renderer_expanded)
    native_visual_environments = _extract_native_visual_environments(structural_preamble)
    project_index = _project_index(
        manifest=manifest,
        sources=sources,
        root_file=root_file,
        expanded=expanded,
        source_map=source_map,
        include_edges=include_edges,
        bibliography=bibliography_index,
    )
    for environment in project_index.get("environments", []):
        if (
            isinstance(environment, dict)
            and environment.get("name") in native_visual_environments
        ):
            environment["render_strategy"] = "native-latex-block"
    # Only preamble macros are globally visible for structural parsing. Commands
    # declared inside a table or another local group must retain their TeX scope;
    # globally expanding them corrupts unrelated blocks and macro definitions.
    macros = _extract_macros(structural_preamble)
    custom_theorem_labels = _extract_theorem_environments(expanded)
    unnumbered_theorems = _extract_unnumbered_theorem_environments(expanded)
    theorem_labels = {**THEOREM_ENVIRONMENT_LABELS, **custom_theorem_labels}
    environment_types = {
        **ENVIRONMENT_TYPES,
        **{
            name: "LATEX_BLOCK"
            for name in native_visual_environments
            if name not in custom_theorem_labels
        },
        **{name: "THEOREM" for name in custom_theorem_labels},
    }
    body = _expand_top_level_body_macros(_document_body(expanded), macros)
    body = _inject_body_render_declarations(body)
    body, body_tikz_styles = _extract_body_tikz_styles(body)
    if body_tikz_styles:
        renderer_preamble = "\n".join(
            (renderer_preamble.rstrip(), *body_tikz_styles, "")
        )
    body = _unwrap_textual_tikz_containers(body)
    external_code_inputs = _external_code_inputs(
        body,
        root_file=root_file,
        manifest=manifest,
        storage=storage,
        warnings=warnings,
    )
    blocks = _parse_blocks(
        body,
        root_file,
        assets=_asset_index(manifest),
        graphic_paths=_graphic_paths(expanded),
        environment_types=environment_types,
        theorem_labels=theorem_labels,
        unnumbered_theorems=unnumbered_theorems,
        external_code_inputs=external_code_inputs,
    )
    blocks = _apply_document_reference_index(blocks, body)
    blocks = _apply_declared_katex_capabilities(blocks, renderer_preamble)
    blocks = _apply_math_render_contracts(blocks)
    if not blocks:
        raise LatexParseError("No readable content blocks were found in the LaTeX project.")
    return ParsedDocument(
        root_file=root_file,
        blocks=blocks,
        warnings=warnings,
        renderer_preamble=renderer_preamble,
        project_index=project_index,
    )
