from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from app.infrastructure.latex.render_context import (
    RenderContext,
    build_render_context,
    contains_unescaped_tex_command,
    split_leading_render_declarations,
    without_tex_distribution_shadows,
)

TABLE_RENDERER_VERSION = "latex-image-v2.17"
LATEX_BLOCK_RENDERER_VERSION = "latex-block-image-v1"
TABLE_RENDER_MAX_ASSET_COUNT = 32
TABLE_RENDER_MAX_ASSET_BYTES = 25_000_000
TABLE_RASTER_DPI = 300

# These generations can contain a visible, content-addressed asset that is still incompatible
# with the current output contract.  v2.16 allowed paper-level review rulers to become part of a
# table SVG, so a geometry-presence check alone cannot safely preserve it during startup recovery.
INCOMPATIBLE_TABLE_RENDERER_VERSIONS = frozenset({"latex-image-v2.16"})

_COMPLEX_FEATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spanning cells", re.compile(r"\\(?:multirow|multicolumn)\b")),
    ("partial rules", re.compile(r"\\(?:cline|cmidrule)\b")),
    ("sizing wrappers", re.compile(r"\\(?:resizebox|adjustbox|rotatebox)\b")),
    (
        "advanced table environment",
        re.compile(r"\\begin\s*\{(?:tabularx|longtable|sidewaystable\*?|wraptable)\}"),
    ),
    ("advanced cells", re.compile(r"\\(?:makecell|shortstack|newcolumntype)\b")),
    (
        "advanced column specification",
        re.compile(
            r"\\begin\s*\{tabular\*?\}\s*(?:\{[^{}]*\})?\s*\{[^{}]*(?:@\{|!\{|>\{|<\{|[pmb]\{)"
        ),
    ),
)

_FORBIDDEN_SOURCE = re.compile(
    r"\\(?:documentclass|usepackage|RequirePackage|begin\s*\{document\}|"
    r"end\s*\{document\}|input|include|write18|write|openin|openout|read|"
    r"catcode|loop|csname)\b",
    re.IGNORECASE,
)
_FORBIDDEN_PREAMBLE = re.compile(
    r"\\(?:write18|write|openin|openout|read|catcode|loop)\b",
    re.IGNORECASE,
)
_OUTER_TABLE_BEGIN = re.compile(
    r"\\begin\s*\{(?P<environment>table\*?|sidewaystable\*?|wraptable)\}"
    r"(?:\s*\[[^\]]*\])?"
)
_OUTER_TABLE_END = re.compile(r"\\end\s*\{(?:table\*?|sidewaystable\*?|wraptable)\}")
_OUTER_ALGORITHM_BEGIN = re.compile(
    r"\\begin\s*\{algorithm\*?\}(?:\s*\[[^\]]*\])?"
)
_OUTER_ALGORITHM_END = re.compile(r"\\end\s*\{algorithm\*?\}")
_SUBTABLE_BEGIN = re.compile(r"\\begin\s*\{subtable\}")
_CITATION_RE = re.compile(
    r"(?:~|\s)*\\(?:cite|citep|citet|citealp|citeauthor|citeyear|nocite)\*?"
    r"(?:\s*\[[^\]]*\]){0,2}\s*\{[^{}]*\}"
)
_REFERENCE_RE = re.compile(
    r"(?:~|\s)*(?:\\S\s*)?\\(?:ref|pageref|eqref|autoref|cref|Cref)\*?\s*\{[^{}]*\}"
)
_MISSING_STYLE_RE = re.compile(r"File [`'](?P<package>[^`']+)\.sty['`] not found", re.I)
_PDF_GRAPHICS_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".pdf"})
_DETERMINISTIC_CONTEXT_FAILURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"Environment \S+ undefined", re.I),
    re.compile(r"Undefined control sequence", re.I),
    re.compile(r"Command \\?\S+ undefined", re.I),
    re.compile(r"Package array Error: Illegal pream-token", re.I),
    re.compile(r"Package xcolor Error: Undefined color", re.I),
)

_FALLBACK_PREAMBLE = r"""
\documentclass{article}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb}
\usepackage{array,booktabs,multirow,tabularx,longtable,changepage}
\usepackage{graphicx,xcolor,colortbl,makecell,dcolumn,rotating,pifont}
\usepackage{siunitx}
\usepackage{url}
""".strip()


class TableRenderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        repair_eligible: bool = True,
        repair_ineligible_reason: str | None = None,
        error_code: str = "LATEX_TABLE_RENDER_FAILED",
        missing_package: str | None = None,
        attempts: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.repair_eligible = repair_eligible
        self.repair_ineligible_reason = repair_ineligible_reason
        self.error_code = error_code
        self.missing_package = missing_package
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class TableRenderArtifact:
    content: bytes
    mime_type: str
    extension: str
    compiler: str
    render_context: dict[str, Any] = field(default_factory=dict)


_VISIBLE_SVG_ELEMENTS = {
    "circle",
    "ellipse",
    "image",
    "line",
    "path",
    "polygon",
    "polyline",
    "rect",
    "text",
    "use",
}


def _positive_svg_length(value: str | None) -> bool:
    if not value:
        return False
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:[a-zA-Z%]*)\s*",
        value,
    )
    return match is not None and float(match.group(1)) > 0


def table_svg_has_visible_geometry(content: bytes) -> bool:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return False
    if not _positive_svg_length(root.attrib.get("width")):
        return False
    if not _positive_svg_length(root.attrib.get("height")):
        return False
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) != 4:
        return False
    try:
        if float(view_box[2]) <= 0 or float(view_box[3]) <= 0:
            return False
    except ValueError:
        return False
    return any(
        element.tag.rsplit("}", 1)[-1] in _VISIBLE_SVG_ELEMENTS
        for element in root.iter()
        if element is not root
    )


def _combine_svg_pages(pages: list[bytes]) -> bytes:
    """Stack compiler-produced SVG pages into one safe, vertically flowing asset."""

    if len(pages) == 1:
        return pages[0]
    parsed: list[tuple[ET.Element, tuple[float, float, float, float]]] = []
    for content in pages:
        root = ET.fromstring(content)
        values = tuple(float(value) for value in root.attrib["viewBox"].replace(",", " ").split())
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            raise TableRenderError("A rendered table SVG page has invalid geometry.")
        parsed.append((root, values))  # type: ignore[arg-type]

    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    width = max(view_box[2] for _root, view_box in parsed)
    height = sum(view_box[3] for _root, view_box in parsed)
    combined = ET.Element(
        f"{{{namespace}}}svg",
        {
            "width": f"{width:g}pt",
            "height": f"{height:g}pt",
            "viewBox": f"0 0 {width:g} {height:g}",
        },
    )
    offset = 0.0
    for page_index, (root, view_box) in enumerate(parsed, start=1):
        id_map: dict[str, str] = {}
        for element in root.iter():
            identifier = element.attrib.get("id")
            if identifier:
                id_map[identifier] = f"page{page_index}-{identifier}"
                element.attrib["id"] = id_map[identifier]
        for element in root.iter():
            for attribute, value in tuple(element.attrib.items()):
                rewritten = value
                for old, new in id_map.items():
                    rewritten = rewritten.replace(f"url(#{old})", f"url(#{new})")
                    if rewritten == f"#{old}":
                        rewritten = f"#{new}"
                element.attrib[attribute] = rewritten
        nested = ET.SubElement(
            combined,
            f"{{{namespace}}}svg",
            {
                "x": "0",
                "y": f"{offset:g}",
                "width": f"{view_box[2]:g}",
                "height": f"{view_box[3]:g}",
                "viewBox": " ".join(f"{value:g}" for value in view_box),
                "overflow": "visible",
            },
        )
        nested.extend(list(root))
        offset += view_box[3]
    return ET.tostring(combined, encoding="utf-8")


def table_png_has_visible_geometry(content: bytes) -> bool:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    if content[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", content[16:24])
    return width > 0 and height > 0


def table_complexity_reasons(source_latex: str) -> list[str]:
    reasons = [label for label, pattern in _COMPLEX_FEATURES if pattern.search(source_latex)]
    tabular_count = len(
        re.findall(r"\\begin\s*\{(?:tabular\*?|tabularx|longtable)\}", source_latex)
    )
    if tabular_count > 1:
        reasons.append("nested tables")
    return reasons


def _matching_brace(value: str, opening: int) -> int | None:
    depth = 0
    escaped = False
    for index in range(opening, len(value)):
        character = value[index]
        if character == "\\" and not escaped:
            escaped = True
            continue
        if not escaped:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return index + 1
        escaped = False
    return None


def _remove_braced_command(value: str, command: str, *, optional: bool = False) -> str:
    pattern = re.compile(rf"\\{command}\*?\s*")
    output = value
    cursor = 0
    while (match := pattern.search(output, cursor)) is not None:
        start = match.start()
        argument_start = match.end()
        if optional and argument_start < len(output) and output[argument_start] == "[":
            optional_end = output.find("]", argument_start + 1)
            if optional_end == -1:
                cursor = match.end()
                continue
            argument_start = optional_end + 1
            while argument_start < len(output) and output[argument_start].isspace():
                argument_start += 1
        if argument_start >= len(output) or output[argument_start] != "{":
            cursor = match.end()
            continue
        end = _matching_brace(output, argument_start)
        if end is None:
            cursor = match.end()
            continue
        output = f"{output[:start]} {output[end:]}"
        cursor = start + 1
    return output


def _consume_required_arguments(value: str, cursor: int, count: int) -> int | None:
    for _ in range(count):
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor >= len(value) or value[cursor] != "{":
            return None
        end = _matching_brace(value, cursor)
        if end is None:
            return None
        cursor = end
    return cursor


def _outer_table_content_start(body: str, begin: re.Match[str]) -> int:
    if begin.group("environment") != "wraptable":
        return begin.end()
    return _consume_required_arguments(body, begin.end(), 2) or begin.end()


def _hoist_alignment_declarations(body: str) -> tuple[str, str]:
    match = re.match(r"\s*\\begin\s*\{(?P<environment>tabular\*?|tabularx|longtable)\}", body)
    if match is None:
        return "", body
    cursor = match.end()
    while cursor < len(body) and body[cursor].isspace():
        cursor += 1
    if cursor < len(body) and body[cursor] == "[":
        optional_end = body.find("]", cursor + 1)
        if optional_end == -1:
            return "", body
        cursor = optional_end + 1
    required = 2 if match.group("environment") in {"tabular*", "tabularx"} else 1
    open_end = _consume_required_arguments(body, cursor, required)
    if open_end is None:
        return "", body
    declarations, remainder = split_leading_render_declarations(body[open_end:])
    if not declarations:
        return "", body
    return declarations, f"{body[:open_end]}\n{remainder}"


def normalize_table_latex_for_render(source_latex: str) -> str:
    """Return the idempotent, document-safe table body used by every render attempt."""
    body = source_latex.strip()
    begin = _OUTER_TABLE_BEGIN.search(body)
    end_matches = list(_OUTER_TABLE_END.finditer(body))
    if (
        begin is not None
        and end_matches
        and end_matches[-1].start() > begin.end()
        and _SUBTABLE_BEGIN.search(body) is None
    ):
        body = body[_outer_table_content_start(body, begin) : end_matches[-1].start()]
    declarations, table_body = split_leading_render_declarations(body)
    alignment_declarations, table_body = _hoist_alignment_declarations(table_body)
    declarations = "\n".join(
        part for part in (declarations, alignment_declarations) if part
    )
    table_body = _remove_braced_command(table_body, "caption", optional=True)
    table_body = _remove_braced_command(table_body, "label")
    table_body = _CITATION_RE.sub("", table_body)
    table_body = _REFERENCE_RE.sub("", table_body)
    return "\n".join(part for part in (declarations, table_body.strip()) if part)


def normalize_latex_block_for_render(
    source_latex: str,
    *,
    render_kind: Literal["algorithm", "visual"],
) -> str:
    """Return a source-faithful body for the generic native-LaTeX block renderer.

    Visual custom environments are preserved byte-for-byte (apart from surrounding whitespace).
    Algorithms shed only their floating shell, caption and label because those document-level
    semantics are rendered as accessible HTML by the reader. The algorithmic program itself is
    never flattened or translated into another package dialect.
    """

    body = source_latex.strip()
    if render_kind == "algorithm":
        begin = _OUTER_ALGORITHM_BEGIN.search(body)
        end_matches = list(_OUTER_ALGORITHM_END.finditer(body))
        if begin is not None and end_matches and end_matches[-1].start() > begin.end():
            body = body[begin.end() : end_matches[-1].start()]
        body = _remove_braced_command(body, "caption", optional=True)
        body = _remove_braced_command(body, "label")
    declarations, block_body = split_leading_render_declarations(body)
    return "\n".join(part for part in (declarations, block_body.strip()) if part)


def _compiler_error(log: str) -> TableRenderError:
    concise = " ".join(log.split())[-1800:]
    missing_style = _MISSING_STYLE_RE.search(concise)
    if missing_style is not None:
        package = missing_style.group("package")
        return TableRenderError(
            f"LaTeX table compilation failed: {concise}",
            repair_eligible=False,
            repair_ineligible_reason=(
                f"The deterministic LaTeX runtime is missing the required package {package}.sty; "
                "AI repair must not remove or replace package-dependent content."
            ),
            error_code="MISSING_TEX_PACKAGE",
            missing_package=package,
        )
    if any(pattern.search(concise) is not None for pattern in _DETERMINISTIC_CONTEXT_FAILURES):
        return TableRenderError(
            f"LaTeX table compilation failed: {concise}",
            repair_eligible=False,
            repair_ineligible_reason=(
                "The table depends on deterministic paper-local LaTeX context. The parser must "
                "resolve that context before AI repair can be considered."
            ),
            error_code="LATEX_CONTEXT_UNRESOLVED",
        )
    return TableRenderError(f"LaTeX table compilation failed: {concise}")


def _attempt_record(
    strategy: str,
    error: TableRenderError,
    *,
    context: RenderContext | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "strategy": strategy,
        "status": "failed",
        "error_code": error.error_code,
        "error": str(error)[:2000],
        "repair_eligible": error.repair_eligible,
    }
    if error.missing_package:
        record["missing_package"] = error.missing_package
    if context is not None:
        record["context_hash"] = context.context_hash
        record["skipped_missing_packages"] = list(context.skipped_missing_packages)
        record["skipped_unsafe_commands"] = list(context.skipped_unsafe_commands)
    return record


class LatexTableRenderer:
    def __init__(
        self,
        *,
        latex_command: str = "latex",
        pdf_latex_command: str = "pdflatex",
        dvisvgm_command: str = "dvisvgm",
        pdf_to_svg_command: str = "pdftocairo",
        pdf_info_command: str = "pdfinfo",
        kpsewhich_command: str = "kpsewhich",
        timeout_seconds: int = 20,
        max_source_chars: int = 100_000,
        max_output_bytes: int = 5_000_000,
        max_asset_count: int = TABLE_RENDER_MAX_ASSET_COUNT,
        max_asset_bytes: int = TABLE_RENDER_MAX_ASSET_BYTES,
    ) -> None:
        self.latex_command = latex_command
        self.pdf_latex_command = pdf_latex_command
        self.dvisvgm_command = dvisvgm_command
        self.pdf_to_svg_command = pdf_to_svg_command
        self.pdf_info_command = pdf_info_command
        self.kpsewhich_command = kpsewhich_command
        self.timeout_seconds = timeout_seconds
        self.max_source_chars = max_source_chars
        self.max_output_bytes = max_output_bytes
        self.max_asset_count = max_asset_count
        self.max_asset_bytes = max_asset_bytes

    @property
    def available(self) -> bool:
        return bool(
            shutil.which(self.latex_command)
            and shutil.which(self.pdf_latex_command)
            and shutil.which(self.dvisvgm_command)
            and shutil.which(self.pdf_to_svg_command)
            and shutil.which(self.pdf_info_command)
        )

    def _document(
        self,
        source_latex: str,
        preamble: str,
        *,
        graphics_driver: str = "dvisvgm",
        tight_page: bool = False,
        content_kind: Literal["table", "algorithm", "visual"] = "table",
    ) -> str:
        if len(source_latex) > self.max_source_chars or len(preamble) > self.max_source_chars:
            raise TableRenderError(
                "The LaTeX table or preamble exceeds the render limit.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "The deterministic renderer safety limit was exceeded; AI repair must not "
                    "truncate canonical paper content."
                ),
                error_code="LATEX_RENDER_LIMIT_EXCEEDED",
            )
        if contains_unescaped_tex_command(source_latex, _FORBIDDEN_SOURCE):
            raise TableRenderError(
                "The LaTeX table contains a command forbidden by the renderer.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "Unsafe LaTeX source commands require deterministic isolation and must not "
                    "be rewritten by AI repair."
                ),
                error_code="UNSAFE_LATEX_SOURCE",
            )
        if contains_unescaped_tex_command(preamble, _FORBIDDEN_PREAMBLE):
            raise TableRenderError(
                "The LaTeX preamble contains a command forbidden by the renderer.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "The full paper preamble contains a restricted command. Safe declarations "
                    "must be isolated deterministically before rendering."
                ),
                error_code="UNSAFE_PREAMBLE_CONTEXT",
            )
        clean_preamble = preamble.strip() or _FALLBACK_PREAMBLE
        clean_preamble = re.split(r"\\begin\s*\{document\}", clean_preamble, maxsplit=1)[0]
        if not re.search(r"\\documentclass(?:\[[^\]]*\])?\s*\{", clean_preamble):
            clean_preamble = f"{_FALLBACK_PREAMBLE}\n{clean_preamble}"
        parts = [
            f"\\PassOptionsToPackage{{{graphics_driver}}}{{graphicx}}\n",
            f"{clean_preamble}\n",
        ]
        if tight_page:
            parts.extend(
                [
                    "\\usepackage[active,tightpage]{preview}\n",
                    "\\setlength\\PreviewBorder{0pt}\n",
                ]
            )
        parts.append("\\begin{document}\n")
        if content_kind == "table":
            # An isolated table must not inherit review decorations from the paper's page
            # template. Conference styles commonly install persistent eso-pic shipout rulers
            # (rather than using the lineno package), so an empty page style alone does not
            # remove them and dvisvgm quite correctly includes the ruler in its minimum bounding
            # box. Run this after begin-document hooks, then clear both current eso-pic hooks and
            # its legacy background alias.
            parts.append(
                "\\makeatletter\n"
                "\\@ifundefined{nolinenumbers}{}{\\nolinenumbers}\n"
                "\\@ifundefined{ClearShipoutPictureBG}{}{\\ClearShipoutPictureBG}\n"
                "\\@ifundefined{ClearShipoutPictureFG}{}{\\ClearShipoutPictureFG}\n"
                "\\@ifundefined{ClearShipoutPicture}{}{\\ClearShipoutPicture}\n"
                "\\makeatother\n"
            )
        parts.extend(
            [
                "\\pagestyle{empty}\\thispagestyle{empty}\n",
                "\\noindent\n",
            ]
        )
        if tight_page:
            parts.append("\\begin{preview}\n")
        normalized_source = (
            normalize_table_latex_for_render(source_latex)
            if content_kind == "table"
            else normalize_latex_block_for_render(
                source_latex,
                render_kind=content_kind,
            )
        )
        parts.append(f"{normalized_source}\n")
        if tight_page:
            parts.append("\\end{preview}\n")
        # Ship pending floats but bypass paper-level end-document hooks.  Those hooks commonly
        # print affiliations or require front-matter state and do not belong to an isolated table.
        parts.append("\\clearpage\n\\makeatletter\\@@end\n")
        return "".join(parts)

    def _stage_assets(self, workdir: Path, assets: Mapping[str, bytes]) -> None:
        if len(assets) > self.max_asset_count:
            raise TableRenderError(
                "The table references too many image assets.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "Image asset staging is a deterministic renderer responsibility and "
                    "cannot be repaired safely by the language model."
                ),
            )
        total_bytes = sum(len(content) for content in assets.values())
        if total_bytes > self.max_asset_bytes:
            raise TableRenderError(
                "The table image assets exceed the render size limit.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "Image asset staging is a deterministic renderer responsibility and "
                    "cannot be repaired safely by the language model."
                ),
            )
        for relative_path, content in assets.items():
            candidate = PurePosixPath(relative_path)
            if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
                raise TableRenderError(
                    "The table references an unsafe image asset path.",
                    repair_eligible=False,
                    repair_ineligible_reason=(
                        "Unsafe or unresolved image paths must not be rewritten by AI repair."
                    ),
                )
            destination = (workdir / Path(*candidate.parts)).resolve()
            if not destination.is_relative_to(workdir.resolve()):
                raise TableRenderError(
                    "The table image asset escapes the render directory.",
                    repair_eligible=False,
                    repair_ineligible_reason=(
                        "Unsafe or unresolved image paths must not be rewritten by AI repair."
                    ),
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

    def _compile_document(self, document: str, assets: Mapping[str, bytes]) -> bytes:
        environment = os.environ.copy()
        environment.update(
            {
                "openin_any": "p",
                "openout_any": "p",
                "shell_escape": "f",
                "SOURCE_DATE_EPOCH": "0",
            }
        )
        try:
            with tempfile.TemporaryDirectory(prefix="latex-table-") as temporary:
                workdir = Path(temporary)
                self._stage_assets(workdir, assets)
                tex_path = workdir / "table.tex"
                tex_path.write_text(document, encoding="utf-8")
                # Paper-local .sty/.cls/data files are compiler dependencies, not
                # evidence that the table needs a PDF graphics pipeline.  Conflating
                # the two made otherwise ordinary DVI tables take a lossy raster path.
                uses_pdf = any(
                    PurePosixPath(path).suffix.lower() in _PDF_GRAPHICS_SUFFIXES
                    for path in assets
                )
                compiler = self.pdf_latex_command if uses_pdf else self.latex_command
                output_path = workdir / ("table.pdf" if uses_pdf else "table.dvi")
                compile_result = subprocess.run(
                    [
                        compiler,
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-file-line-error",
                        "-no-shell-escape",
                        tex_path.name,
                    ],
                    cwd=workdir,
                    env=environment,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
                if compile_result.returncode != 0 or not output_path.is_file():
                    log = (compile_result.stdout + compile_result.stderr).decode(
                        "utf-8", errors="replace"
                    )
                    raise _compiler_error(log)
                if uses_pdf:
                    info_result = subprocess.run(
                        [self.pdf_info_command, output_path.name],
                        cwd=workdir,
                        env=environment,
                        capture_output=True,
                        check=False,
                        timeout=self.timeout_seconds,
                    )
                    info_text = (info_result.stdout + info_result.stderr).decode(
                        "utf-8", errors="replace"
                    )
                    page_match = re.search(r"^Pages:\s*(\d+)\s*$", info_text, re.MULTILINE)
                    if info_result.returncode != 0 or page_match is None:
                        concise_info = " ".join(info_text.split())[-1800:]
                        raise TableRenderError(
                            f"LaTeX table PDF inspection failed: {concise_info}"
                        )
                    if int(page_match.group(1)) != 1:
                        raise TableRenderError(
                            "The rendered table spans multiple visible pages and cannot "
                            "be represented safely by the single-asset table contract."
                        )
                    raster_path = workdir / "table.png"
                    conversion_result = subprocess.run(
                        [
                            self.pdf_to_svg_command,
                            "-png",
                            "-singlefile",
                            "-r",
                            str(TABLE_RASTER_DPI),
                            output_path.name,
                            "table",
                        ],
                        cwd=workdir,
                        env=environment,
                        capture_output=True,
                        check=False,
                        timeout=self.timeout_seconds,
                    )
                    if conversion_result.returncode != 0 or not raster_path.is_file():
                        log = (conversion_result.stdout + conversion_result.stderr).decode(
                            "utf-8", errors="replace"
                        )
                        raise TableRenderError(
                            "LaTeX table PNG conversion failed: "
                            f"{' '.join(log.split())[-1800:]}"
                        )
                    content = raster_path.read_bytes()
                    if not table_png_has_visible_geometry(content):
                        raise TableRenderError(
                            "LaTeX table PNG conversion produced no visible geometry."
                        )
                else:
                    conversion_result = subprocess.run(
                        [
                            self.dvisvgm_command,
                            "--bbox=min",
                            "--no-fonts",
                            "--page=1-",
                            "--output=table-%p.svg",
                            output_path.name,
                        ],
                        cwd=workdir,
                        env=environment,
                        capture_output=True,
                        check=False,
                        timeout=self.timeout_seconds,
                    )
                    svg_paths = sorted(
                        workdir.glob("table-*.svg"),
                        key=lambda path: int(path.stem.rsplit("-", 1)[-1]),
                    )
                    if conversion_result.returncode != 0 or not svg_paths:
                        log = (conversion_result.stdout + conversion_result.stderr).decode(
                            "utf-8", errors="replace"
                        )
                        raise TableRenderError(
                            "LaTeX table SVG conversion failed: "
                            f"{' '.join(log.split())[-1800:]}"
                        )
                    visible_pages = [
                        page_content
                        for svg_path in svg_paths
                        if table_svg_has_visible_geometry(
                            page_content := svg_path.read_bytes()
                        )
                    ]
                    if not visible_pages:
                        raise TableRenderError(
                            "LaTeX table SVG conversion produced no visible geometry."
                        )
                    content = _combine_svg_pages(visible_pages)
        except subprocess.TimeoutExpired as error:
            raise TableRenderError(
                f"LaTeX table rendering exceeded {self.timeout_seconds} seconds."
            ) from error
        return content

    def render(
        self,
        *,
        source_latex: str,
        preamble: str = "",
        assets: Mapping[str, bytes] | None = None,
        content_kind: Literal["table", "algorithm", "visual"] = "table",
    ) -> TableRenderArtifact:
        if not self.available:
            raise TableRenderError(
                "The local LaTeX table renderer is not installed.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "A missing renderer dependency is an environment failure, not a LaTeX "
                    "source defect."
                ),
                error_code="LATEX_RENDERER_UNAVAILABLE",
            )
        attempt_records: list[dict[str, Any]] = []
        failures: list[TableRenderError] = []
        content = b""
        selected_strategy = "fallback-preamble"
        selected_context: RenderContext | None = None
        exact_assets = dict(assets or {})
        texlive_assets = without_tex_distribution_shadows(
            exact_assets,
            kpsewhich_command=self.kpsewhich_command,
        )

        def compile_candidate(
            candidate: str,
            *,
            strategy: str,
            context: RenderContext | None = None,
            candidate_assets: Mapping[str, bytes] | None = None,
        ) -> TableRenderError | None:
            nonlocal content, selected_strategy, selected_context
            active_assets = exact_assets if candidate_assets is None else candidate_assets
            try:
                uses_pdf = any(
                    PurePosixPath(path).suffix.lower() in _PDF_GRAPHICS_SUFFIXES
                    for path in active_assets
                )
                content = self._compile_document(
                    self._document(
                        source_latex,
                        candidate,
                        graphics_driver="pdftex" if uses_pdf else "dvisvgm",
                        tight_page=uses_pdf,
                        content_kind=content_kind,
                    ),
                    active_assets,
                )
                selected_strategy = strategy
                selected_context = context
                return None
            except TableRenderError as error:
                failures.append(error)
                attempt_records.append(_attempt_record(strategy, error, context=context))
                return error

        def compile_context(
            candidate: str,
            *,
            strategy: str,
            context: RenderContext | None = None,
        ) -> TableRenderError | None:
            error = compile_candidate(candidate, strategy=strategy, context=context)
            if error is not None and texlive_assets != exact_assets:
                error = compile_candidate(
                    candidate,
                    strategy=f"{strategy}-texlive-unshadowed",
                    context=context,
                    candidate_assets=texlive_assets,
                )
            return error

        if preamble.strip():
            excluded_packages: set[str] = set()
            # A missing declared package may be unrelated to the target block. Rebuild the
            # dependency-focused context after each proven missing package, while keeping the
            # retry count bounded and never inventing replacement LaTeX.
            for _ in range(8):
                context = build_render_context(
                    source_latex,
                    preamble,
                    excluded_packages=excluded_packages,
                )
                if not context.preamble.strip():
                    break
                error = compile_context(
                    context.preamble,
                    strategy="focused-paper-context",
                    context=context,
                )
                if error is None:
                    break
                missing_package = error.missing_package
                if (
                    not missing_package
                    or missing_package not in context.declared_packages
                    or missing_package in excluded_packages
                ):
                    break
                excluded_packages.add(missing_package)

        # Prefer the minimal declaration closure so document-class front/back-matter hooks
        # cannot leak unrelated author metadata into the rendered table page.
        if not content and preamble.strip():
            compile_context(preamble, strategy="source-preamble")

        if not content:
            compile_candidate(
                "",
                strategy="fallback-preamble",
                candidate_assets=texlive_assets,
            )

        if not content:
            detail = str(failures[-1]) if failures else "Unknown local compiler error."
            strategies = ", ".join(
                f"{record['strategy']}={record['error_code']}" for record in attempt_records
            )
            if strategies:
                detail = f"All deterministic table render contexts failed ({strategies}). {detail}"
            eligibility_failures = failures
            eligibility_failures = [
                error
                for error, record in zip(failures, attempt_records, strict=True)
                if not (
                    record["strategy"] == "source-preamble"
                    and error.error_code == "UNSAFE_PREAMBLE_CONTEXT"
                )
            ]
            repair_eligible = all(error.repair_eligible for error in eligibility_failures)
            repair_ineligible_reason = next(
                (
                    error.repair_ineligible_reason
                    for error in eligibility_failures
                    if not error.repair_eligible and error.repair_ineligible_reason
                ),
                None,
            )
            raise TableRenderError(
                detail,
                repair_eligible=repair_eligible,
                repair_ineligible_reason=repair_ineligible_reason,
                error_code=(
                    failures[-1].error_code
                    if failures
                    else "LATEX_TABLE_RENDER_FAILED"
                ),
                missing_package=(failures[-1].missing_package if failures else None),
                attempts=tuple(attempt_records),
            )
        if len(content) > self.max_output_bytes:
            raise TableRenderError("The rendered table image exceeds the output size limit.")
        uses_pdf_output = any(
            PurePosixPath(path).suffix.lower() in _PDF_GRAPHICS_SUFFIXES
            for path in (assets or {})
        )
        if uses_pdf_output:
            if not table_png_has_visible_geometry(content):
                raise TableRenderError("The rendered table did not produce a valid PNG image.")
        else:
            lowered = content[:4096].lower()
            if b"<svg" not in lowered or b"<script" in lowered or b"<foreignobject" in lowered:
                raise TableRenderError("The rendered table did not produce a safe SVG document.")
            if re.search(rb"(?:href|xlink:href)\s*=\s*['\"](?!#|data:)", content):
                raise TableRenderError("The rendered table SVG references an external asset.")
        latex_label = Path(self.latex_command).name
        pdf_latex_label = Path(self.pdf_latex_command).name
        dvisvgm_label = Path(self.dvisvgm_command).name
        pdf_converter_label = Path(self.pdf_to_svg_command).name
        return TableRenderArtifact(
            content=content,
            mime_type="image/png" if uses_pdf_output else "image/svg+xml",
            extension="png" if uses_pdf_output else "svg",
            compiler=(
                f"{pdf_latex_label}+{pdf_converter_label}-png@{TABLE_RASTER_DPI}dpi"
                if uses_pdf_output
                else f"{latex_label}+{dvisvgm_label}"
            ),
            render_context={
                "strategy": selected_strategy,
                "content_kind": content_kind,
                **(
                    selected_context.metadata()
                    if selected_context is not None
                    else {}
                ),
                "attempts": attempt_records,
            },
        )
