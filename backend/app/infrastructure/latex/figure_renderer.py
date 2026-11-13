from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.infrastructure.latex.render_context import (
    RenderContext,
    build_render_context,
    contains_unescaped_tex_command,
    split_leading_render_declarations,
    without_tex_distribution_shadows,
)

FIGURE_RENDERER_VERSION = "latex-figure-v2.19"
FIGURE_RENDER_MAX_ASSET_COUNT = 32
FIGURE_RENDER_MAX_ASSET_BYTES = 25_000_000
FIGURE_RENDER_RASTER_DPI = 180
_PDF_GRAPHICS_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

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
_OUTER_FIGURE_BEGIN = re.compile(
    r"\\begin\s*\{(?P<environment>figure\*?|sidewaysfigure\*?|wrapfigure)\}"
    r"(?:\s*\[[^\]]*\])?"
)
_OUTER_FIGURE_END = re.compile(
    r"\\end\s*\{(?:figure\*?|sidewaysfigure\*?|wrapfigure)\}"
)
_SUBFIGURE_BEGIN = re.compile(r"\\begin\s*\{subfigure\}")
_DIRECT_MATRIX_FIGURE_BEGIN = re.compile(
    r"^\s*\\begin\s*\{tikzcd\}(?:\s*\[[^\]]*\])?"
)
_NATIVE_VISUAL_END = re.compile(
    r"\\end\s*\{(?:tikzpicture|tikzcd|xy|pspicture|picture|asy)\}",
    re.IGNORECASE,
)
_CITATION_RE = re.compile(
    r"(?:~|\s)*\\(?:cite|citep|citet|citealp|citeauthor|citeyear|nocite)\*?"
    r"(?:\s*\[[^\]]*\]){0,2}\s*\{[^{}]*\}"
)
_REFERENCE_RE = re.compile(
    r"(?:~|\s)*(?:\\S\s*)?\\(?:ref|pageref|eqref|autoref|cref|Cref)\*?\s*\{[^{}]*\}"
)
_INCLUDE_GRAPHICS_RE = re.compile(r"\\includegraphics\b")
_LSTLISTING_BEGIN = re.compile(
    r"\\begin\s*\{lstlisting\}(?:\s*\[[^\]]*\])?",
    re.IGNORECASE,
)
_LSTLISTING_END = re.compile(r"\\end\s*\{lstlisting\}", re.IGNORECASE)
_MINTED_BEGIN = re.compile(
    r"\\begin\s*\{minted\}(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}",
    re.IGNORECASE,
)
_MINTED_END = re.compile(r"\\end\s*\{minted\}", re.IGNORECASE)
_MISSING_STYLE_RE = re.compile(r"File [`'](?P<package>[^`']+)\.sty['`] not found", re.I)
_DETERMINISTIC_CONTEXT_FAILURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"Environment \S+ undefined", re.I),
    re.compile(r"Undefined control sequence", re.I),
    re.compile(r"Command \\?\S+ undefined", re.I),
    re.compile(r"Package xcolor Error: Undefined color", re.I),
    re.compile(r"Package pgfkeys Error", re.I),
)
_UNICODE_ENGINE_RE = re.compile(
    r"\\usepackage(?:\s*\[[^\]]*\])?\s*\{[^{}]*\bfontspec\b[^{}]*\}"
    r"|\\(?:setmainfont|setsansfont|setmonofont|newfontfamily)\b",
    re.IGNORECASE,
)
_PREVIEW_PACKAGE_RE = re.compile(
    r"\\(?:usepackage|RequirePackage)(?:\s*\[[^\]]*\])?\s*\{[^{}]*\bpreview\b[^{}]*\}",
    re.IGNORECASE,
)

_FALLBACK_PREAMBLE = r"""
\documentclass{article}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb}
\usepackage{array,booktabs,multirow,tabularx,longtable}
\usepackage{graphicx,xcolor,colortbl,makecell,dcolumn,rotating,pifont}
\usepackage{listings}
\lstset{breaklines=true,breakatwhitespace=false,columns=fullflexible,keepspaces=true,
  showstringspaces=false,basicstyle=\ttfamily\footnotesize,
  literate=
    {‘}{{'}}1 {’}{{'}}1
    {“}{{"}}1 {”}{{"}}1
    {–}{{--}}1 {—}{{--}}1
    {…}{{...}}3 { }{{ }}1}
""".strip()


class FigureRenderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        repair_eligible: bool = True,
        repair_ineligible_reason: str | None = None,
        error_code: str = "LATEX_FIGURE_RENDER_FAILED",
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
class FigureRenderArtifact:
    content: bytes
    mime_type: str
    extension: str
    compiler: str
    render_context: dict[str, Any] = field(default_factory=dict)


def figure_requires_latex_render(
    source_latex: str,
    *,
    resolved_assets: object = None,
) -> bool:
    """Return true for LaTeX-native or legacy PostScript-backed figures.

    Browser-compatible manifest assets keep their existing direct-preview path. EPS/PS assets
    require the bounded local LaTeX renderer because browsers cannot display PostScript reliably.
    A missing ``includegraphics`` target is never reclassified as a drawn figure.
    """
    assets = resolved_assets if isinstance(resolved_assets, list) else []
    if not source_latex.strip():
        return False
    if re.search(
        r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{[^{}]*#[1-9][^{}]*\}",
        source_latex,
    ):
        return True
    if _INCLUDE_GRAPHICS_RE.search(source_latex) is not None:
        return any(
            str(asset.get("path", "")).lower().endswith((".eps", ".ps"))
            or str(asset.get("mime_type", "")).lower() == "application/postscript"
            for asset in assets
            if isinstance(asset, dict)
        )
    return not assets


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


def _outer_figure_content_start(body: str, begin: re.Match[str]) -> int:
    cursor = begin.end()
    if begin.group("environment") != "wrapfigure":
        return cursor
    for _ in range(2):
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor >= len(body) or body[cursor] != "{":
            return begin.end()
        end = _matching_brace(body, cursor)
        if end is None:
            return begin.end()
        cursor = end
    return cursor


def _hoist_matrix_declarations(body: str) -> tuple[str, str]:
    r"""Move injected declarations out of a matrix-like figure environment.

    A direct tikzcd is a matrix: tokens after its begin command are cells, not a normal
    environment prologue. Body-scoped state is injected there so the parser can keep it attached
    to the independently rendered block, then this renderer moves that exact declaration prefix
    before the matrix. This mirrors the table renderer's tabular hoist and avoids interpreting or
    rewriting any declaration.
    """

    match = _DIRECT_MATRIX_FIGURE_BEGIN.match(body)
    if match is None:
        return "", body
    declarations, remainder = split_leading_render_declarations(body[match.end() :])
    if not declarations:
        return "", body
    return declarations, f"{body[: match.end()]}\n{remainder}"


def _balanced_visual_boundary(source: str, minimum_end: int) -> int | None:
    r"""Find the first complete TeX group/environment boundary after a visual ends.

    Native drawings are frequently wrapped by ``\resizebox``, ``center`` or ``minipage``.
    Cropping exactly at ``\end{tikzpicture}`` corrupts those wrappers, while retaining the whole
    float can execute unrelated publisher hooks.  Scan the original token stream and stop as soon
    as every wrapper opened before the native visual has closed.
    """

    environment_stack: list[str] = []
    brace_depth = 0
    cursor = 0
    environment_token = re.compile(
        r"\\(?P<kind>begin|end)\s*\{(?P<name>[^{}]+)\}"
    )
    control_word = re.compile(r"\\[A-Za-z@]+\*?")
    while cursor < len(source):
        environment = environment_token.match(source, cursor)
        if environment is not None:
            name = environment.group("name")
            if environment.group("kind") == "begin":
                environment_stack.append(name)
            elif environment_stack and environment_stack[-1] == name:
                environment_stack.pop()
            else:
                return None
            cursor = environment.end()
        elif source[cursor] == "\\":
            command = control_word.match(source, cursor)
            # A control symbol consumes the following character; in particular, escaped braces
            # are data and must not change grouping depth.
            cursor = command.end() if command is not None else min(cursor + 2, len(source))
        else:
            character = source[cursor]
            if character == "{":
                brace_depth += 1
            elif character == "}":
                brace_depth -= 1
                if brace_depth < 0:
                    return None
            cursor += 1
        if cursor >= minimum_end and brace_depth == 0 and not environment_stack:
            return cursor
    return None


def normalize_figure_latex_for_render(source_latex: str) -> str:
    """Return the idempotent, document-safe figure body used by every render attempt."""
    body = source_latex.strip()
    begin = _OUTER_FIGURE_BEGIN.search(body)
    end_matches = list(_OUTER_FIGURE_END.finditer(body))
    has_outer_figure = (
        begin is not None and end_matches and end_matches[-1].start() > begin.end()
    )
    if has_outer_figure and _SUBFIGURE_BEGIN.search(body) is None:
        body = body[_outer_figure_content_start(body, begin) : end_matches[-1].start()]
    declarations, visual_body = split_leading_render_declarations(body)
    matrix_declarations, visual_body = _hoist_matrix_declarations(visual_body)
    declarations = "\n".join(
        part for part in (declarations, matrix_declarations) if part
    )

    # A fully closed drawn program plus its structural wrappers is the visual boundary.
    # Publisher-specific source-credit hooks after it do not affect the pixels.
    visual_ends = list(_NATIVE_VISUAL_END.finditer(visual_body))
    if has_outer_figure and _SUBFIGURE_BEGIN.search(body) is None and visual_ends:
        boundary = _balanced_visual_boundary(visual_body, visual_ends[-1].end())
        if boundary is not None:
            visual_body = visual_body[:boundary]
    visual_body = _remove_braced_command(visual_body, "caption", optional=True)
    visual_body = _remove_braced_command(visual_body, "label")
    visual_body = _CITATION_RE.sub("", visual_body)
    visual_body = _REFERENCE_RE.sub("", visual_body)
    return "\n".join(part for part in (declarations, visual_body.strip()) if part)


def normalize_figure_latex_for_fallback(source_latex: str) -> str:
    """Sanitize package-dependent code listings for the bounded fallback preamble.

    The canonical source and project preamble always get the first render attempt. This
    transformation is used only after that attempt fails, so custom syntax highlighting remains
    intact whenever the paper's own toolchain is available. The fallback preserves every code
    character and enables line wrapping while removing package/language/style options that cannot
    be reconstructed safely.
    """

    body = normalize_figure_latex_for_render(source_latex)
    body = _LSTLISTING_BEGIN.sub(r"\\begin{lstlisting}", body)
    body = _MINTED_BEGIN.sub(r"\\begin{lstlisting}", body)
    body = _MINTED_END.sub(r"\\end{lstlisting}", body)
    return body.strip()


def _compiler_error(log: str) -> FigureRenderError:
    concise = " ".join(log.split())[-1800:]
    missing_style = _MISSING_STYLE_RE.search(concise)
    if missing_style is not None:
        package = missing_style.group("package")
        return FigureRenderError(
            f"LaTeX figure compilation failed: {concise}",
            repair_eligible=False,
            repair_ineligible_reason=(
                f"The deterministic LaTeX runtime is missing {package}.sty; AI repair must not "
                "remove or replace package-dependent figure content."
            ),
            error_code="MISSING_TEX_PACKAGE",
            missing_package=package,
        )
    if any(pattern.search(concise) is not None for pattern in _DETERMINISTIC_CONTEXT_FAILURES):
        return FigureRenderError(
            f"LaTeX figure compilation failed: {concise}",
            repair_eligible=False,
            repair_ineligible_reason=(
                "The figure depends on deterministic paper-local LaTeX context. The parser and "
                "renderer must resolve that context before AI repair can be considered."
            ),
            error_code="LATEX_CONTEXT_UNRESOLVED",
        )
    return FigureRenderError(f"LaTeX figure compilation failed: {concise}")


def _attempt_record(
    strategy: str,
    error: FigureRenderError,
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


class LatexFigureRenderer:
    def __init__(
        self,
        *,
        latex_command: str = "latex",
        pdf_latex_command: str = "pdflatex",
        unicode_latex_command: str = "xelatex",
        dvisvgm_command: str = "dvisvgm",
        pdf_to_svg_command: str = "pdftocairo",
        kpsewhich_command: str = "kpsewhich",
        timeout_seconds: int = 20,
        max_source_chars: int = 250_000,
        max_output_bytes: int = 10_000_000,
        max_asset_count: int = FIGURE_RENDER_MAX_ASSET_COUNT,
        max_asset_bytes: int = FIGURE_RENDER_MAX_ASSET_BYTES,
    ) -> None:
        self.latex_command = latex_command
        self.pdf_latex_command = pdf_latex_command
        self.unicode_latex_command = unicode_latex_command
        self.dvisvgm_command = dvisvgm_command
        self.pdf_to_svg_command = pdf_to_svg_command
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
        )

    def _document(
        self,
        source_latex: str,
        preamble: str,
        *,
        deterministic_fallback: bool = False,
        tight_pdf_page: bool = False,
    ) -> str:
        if len(source_latex) > self.max_source_chars or len(preamble) > self.max_source_chars:
            raise FigureRenderError(
                "The LaTeX figure or preamble exceeds the render limit.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "The deterministic renderer safety limit was exceeded; AI repair must not "
                    "truncate canonical figure content."
                ),
                error_code="LATEX_RENDER_LIMIT_EXCEEDED",
            )
        if contains_unescaped_tex_command(source_latex, _FORBIDDEN_SOURCE):
            raise FigureRenderError(
                "The LaTeX figure contains a command forbidden by the renderer.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "Unsafe LaTeX source commands require deterministic isolation and must not "
                    "be rewritten by AI repair."
                ),
                error_code="UNSAFE_LATEX_SOURCE",
            )
        if contains_unescaped_tex_command(preamble, _FORBIDDEN_PREAMBLE):
            raise FigureRenderError(
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
        if tight_pdf_page:
            # XeTeX's PGF/TikZ driver emits xdvipdfmx PDF specials. dvisvgm can read the
            # surrounding XDV text, but silently drops those drawing specials. Compile a real
            # PDF instead, and make its media box content-sized before converting that PDF back
            # to SVG. Pass options before the paper preamble so an existing preview import is
            # upgraded without a package-option clash.
            clean_preamble = (
                "\\PassOptionsToPackage{active,tightpage}{preview}\n"
                f"{clean_preamble}\n"
                + (
                    ""
                    if _PREVIEW_PACKAGE_RE.search(clean_preamble) is not None
                    else "\\usepackage{preview}"
                )
            )
        figure_body = (
            normalize_figure_latex_for_fallback(source_latex)
            if deterministic_fallback
            else normalize_figure_latex_for_render(source_latex)
        )
        if tight_pdf_page:
            figure_body = f"\\begin{{preview}}\n{figure_body}\n\\end{{preview}}"
        return (
            f"{clean_preamble}\n"
            "\\begin{document}\n"
            "\\pagestyle{empty}\\thispagestyle{empty}\n"
            "\\noindent\n"
            f"{figure_body}\n"
            # Ship pending floats, then terminate below LaTeX's document-hook layer.  A focused
            # block must not execute publisher title/affiliation hooks unrelated to the visual.
            "\\clearpage\n"
            "\\makeatletter\\@@end\n"
        )

    def _stage_assets(self, workdir: Path, assets: Mapping[str, bytes]) -> None:
        if len(assets) > self.max_asset_count:
            raise FigureRenderError("The figure references too many source assets.")
        if sum(len(content) for content in assets.values()) > self.max_asset_bytes:
            raise FigureRenderError("The figure source assets exceed the render size limit.")
        for relative_path, content in assets.items():
            candidate = PurePosixPath(relative_path)
            if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
                raise FigureRenderError("The figure references an unsafe source asset path.")
            destination = (workdir / Path(*candidate.parts)).resolve()
            if not destination.is_relative_to(workdir.resolve()):
                raise FigureRenderError("The figure source asset escapes the render directory.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

    def _compile_document(
        self,
        document: str,
        assets: Mapping[str, bytes],
    ) -> tuple[bytes, str]:
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
            with tempfile.TemporaryDirectory(prefix="latex-figure-") as temporary:
                workdir = Path(temporary)
                self._stage_assets(workdir, assets)
                tex_path = workdir / "figure.tex"
                tex_path.write_text(document, encoding="utf-8")
                unicode_engine = _UNICODE_ENGINE_RE.search(document) is not None
                pdf_engine = (
                    not unicode_engine
                    and (
                        any(
                            PurePosixPath(path).suffix.lower() in _PDF_GRAPHICS_SUFFIXES
                            for path in assets
                        )
                        or re.search(r"\\pdfoutput\s*=\s*1\b", document) is not None
                    )
                )
                compiler = (
                    self.unicode_latex_command
                    if unicode_engine
                    else self.pdf_latex_command
                    if pdf_engine
                    else self.latex_command
                )
                if unicode_engine and not shutil.which(compiler):
                    raise FigureRenderError(
                        "The paper requires a Unicode TeX engine, but XeLaTeX is unavailable.",
                        repair_eligible=False,
                        repair_ineligible_reason=(
                            "A missing TeX engine is an environment dependency failure and "
                            "must not be rewritten by AI repair."
                        ),
                        error_code="LATEX_RENDERER_UNAVAILABLE",
                    )
                output_path = workdir / (
                    "figure.pdf"
                    if unicode_engine or pdf_engine
                    else "figure.dvi"
                )
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
                converted_path = workdir / ("figure.png" if pdf_engine else "figure.svg")
                conversion_command = (
                    [
                        self.pdf_to_svg_command,
                        "-png",
                        "-singlefile",
                        "-r",
                        str(FIGURE_RENDER_RASTER_DPI),
                        output_path.name,
                        converted_path.stem,
                    ]
                    if pdf_engine
                    else [
                        self.dvisvgm_command,
                        *(["--pdf", "--embed-bitmaps"] if unicode_engine else []),
                        "--bbox=min",
                        "--no-fonts",
                        "--page=1",
                        f"--output={converted_path.name}",
                        output_path.name,
                    ]
                )
                conversion_result = subprocess.run(
                    conversion_command,
                    cwd=workdir,
                    env=environment,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
                if conversion_result.returncode != 0 or not converted_path.is_file():
                    log = (conversion_result.stdout + conversion_result.stderr).decode(
                        "utf-8", errors="replace"
                    )
                    raise FigureRenderError(
                        f"LaTeX figure conversion failed: {' '.join(log.split())[-1800:]}"
                    )
                content = converted_path.read_bytes()
        except subprocess.TimeoutExpired as error:
            raise FigureRenderError(
                f"LaTeX figure rendering exceeded {self.timeout_seconds} seconds."
            ) from error
        return content, compiler

    def render(
        self,
        *,
        source_latex: str,
        preamble: str = "",
        assets: Mapping[str, bytes] | None = None,
    ) -> FigureRenderArtifact:
        if not self.available:
            raise FigureRenderError(
                "The local LaTeX figure renderer is not installed.",
                repair_eligible=False,
                repair_ineligible_reason=(
                    "A missing renderer dependency is an environment failure, not a LaTeX "
                    "source defect."
                ),
                error_code="LATEX_RENDERER_UNAVAILABLE",
            )
        attempt_records: list[dict[str, Any]] = []
        failures: list[FigureRenderError] = []
        content = b""
        selected_strategy = "fallback-preamble"
        selected_context: RenderContext | None = None
        selected_compiler = self.latex_command
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
        ) -> FigureRenderError | None:
            nonlocal content, selected_strategy, selected_context, selected_compiler
            active_assets = exact_assets if candidate_assets is None else candidate_assets
            try:
                content, selected_compiler = self._compile_document(
                    self._document(
                        source_latex,
                        candidate,
                        deterministic_fallback=strategy == "fallback-preamble",
                        tight_pdf_page=_UNICODE_ENGINE_RE.search(candidate) is not None,
                    ),
                    active_assets,
                )
                selected_strategy = strategy
                selected_context = context
                return None
            except FigureRenderError as error:
                failures.append(error)
                attempt_records.append(_attempt_record(strategy, error, context=context))
                return error

        def compile_context(
            candidate: str,
            *,
            strategy: str,
            context: RenderContext | None = None,
        ) -> FigureRenderError | None:
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

        # The complete paper preamble may contain front/back-matter declarations whose
        # document-class hooks emit author addresses or other unrelated content at
        # \end{document}.  Use it only when the dependency-focused declaration closure cannot
        # compile the target; successful isolated contexts are both more precise and safer.
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
                detail = f"All deterministic figure render contexts failed ({strategies}). {detail}"
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
            raise FigureRenderError(
                detail,
                repair_eligible=repair_eligible,
                repair_ineligible_reason=repair_ineligible_reason,
                error_code=(
                    failures[-1].error_code
                    if failures
                    else "LATEX_FIGURE_RENDER_FAILED"
                ),
                missing_package=(failures[-1].missing_package if failures else None),
                attempts=tuple(attempt_records),
            )
        raster_output = selected_compiler == self.pdf_latex_command
        if len(content) > self.max_output_bytes:
            raise FigureRenderError("The rendered figure image exceeds the output size limit.")
        if raster_output:
            if not content.startswith(_PNG_SIGNATURE):
                raise FigureRenderError("The rendered figure did not produce a valid PNG image.")
        else:
            lowered = content[:4096].lower()
            if b"<svg" not in lowered or b"<script" in lowered or b"<foreignobject" in lowered:
                raise FigureRenderError("The rendered figure did not produce a safe SVG document.")
            if re.search(rb"(?:href|xlink:href)\s*=\s*['\"](?!#|data:)", content):
                raise FigureRenderError("The rendered figure SVG references an external asset.")
        selected_converter = self.pdf_to_svg_command if raster_output else self.dvisvgm_command
        compiler_label = Path(selected_compiler).name
        converter_label = Path(selected_converter).name
        return FigureRenderArtifact(
            content=content,
            mime_type="image/png" if raster_output else "image/svg+xml",
            extension="png" if raster_output else "svg",
            compiler=(
                f"{compiler_label}+{converter_label}-png@{FIGURE_RENDER_RASTER_DPI}dpi"
                if raster_output
                else f"{compiler_label}+{converter_label}-pdf"
                if selected_compiler == self.unicode_latex_command
                else f"{compiler_label}+{converter_label}"
            ),
            render_context={
                "strategy": selected_strategy,
                **(
                    selected_context.metadata()
                    if selected_context is not None
                    else {}
                ),
                "attempts": attempt_records,
            },
        )
