from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.os_adapters import ResolvedToolchain, ToolchainCapability, ToolchainCommands
from app.infrastructure.latex import (
    FigureRenderError,
    LatexFigureRenderer,
    LatexTableRenderer,
    TableRenderError,
)

WORKER_SYSTEM_COMMANDS = tuple(ToolchainCommands.__dataclass_fields__)
_REQUIRED_TEX_FILES = (
    "IEEEtran.bst",
    "dsfont.sty",
    "extarrows.sty",
    "fontspec.sty",
    "mathtools.sty",
    "preview.sty",
    "standalone.cls",
    "stmaryrd.sty",
    "tikz-cd.sty",
)
_BIBLIOGRAPHY_SMOKE_BIB = r"""
@article{runtime-smoke,
  author = {Ada Lovelace and Alan M. Turing},
  title = {A Bibliography Runtime Smoke Test},
  journal = {AcadematePaper Systems},
  year = {1950}
}
""".strip()
_PDFLATEX_COMPATIBILITY_SMOKE_SOURCE = r"""
\documentclass{article}
\usepackage{amssymb,dsfont,extarrows,tikz-cd}
\pagestyle{empty}
\begin{document}
$\mathds{Q}\xlongrightarrow{\sim}\mathds{R}$
\[
\begin{tikzcd}
A \arrow[r, hook] & B
\end{tikzcd}
\]
\end{document}
""".strip()
_XELATEX_COMPATIBILITY_SMOKE_SOURCE = r"""
\documentclass{article}
\usepackage{amssymb,fontspec}
\pagestyle{empty}
\begin{document}
$A\lhook\joinrel\longrightarrow B$
\end{document}
""".strip()
_TABLE_RENDER_SMOKE_SOURCE = r"""
\begin{tabular}{lSS}
\toprule
\multirow{2}{*}{\makecell{Model\\Name}} & \multicolumn{2}{c}{Score} \\
\cmidrule{2-3}
& A & B \\
\midrule
Baseline & \num{1.25e3} & 2.5 \\
\multicolumn{3}{c}{\includegraphics[width=1pt]{assets/probe.png}} \\
\bottomrule
\end{tabular}
""".strip()
_TABLE_RENDER_SMOKE_ASSETS = {
    "assets/probe.png": base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
}
_FIGURE_RENDER_SMOKE_LONG_VALUE = (
    "A deliberately long metadata value verifies that the safe listing fallback is available "
    "and wraps code without requiring a paper-specific style or syntax language definition."
)
_FIGURE_RENDER_SMOKE_SOURCE = "\n".join(
    [
        r"\begin{figure}",
        r"\includegraphics[width=1pt]{assets/probe.eps}",
        r"\tiny",
        r"\begin{lstlisting}[language=json,firstnumber=1]",
        '{"title":"dependency smoke—UTF-8","author":"O’Keeffe","abstract":"'
        f"{_FIGURE_RENDER_SMOKE_LONG_VALUE}"
        '"}',
        r"\end{lstlisting}",
        r"\caption{Dependency smoke}",
        r"\end{figure}",
    ]
)
_FIGURE_RENDER_SMOKE_ASSETS = {
    "assets/probe.eps": (
        b"%!PS-Adobe-3.0 EPSF-3.0\n"
        b"%%BoundingBox: 0 0 10 10\n"
        b"newpath 0 0 moveto 10 10 lineto stroke\nshowpage\n"
    )
}
_UNICODE_FIGURE_RENDER_SMOKE_SOURCE = (
    r'\begin{tikzcd}F \arrow[r, "i"] \arrow[d] & E \\'
    r'\mathbf{1} \arrow[r] & B\end{tikzcd}'
)
_UNICODE_FIGURE_RENDER_SMOKE_PREAMBLE = (
    r"\documentclass{article}\usepackage{fontspec}\usepackage{tikz-cd}"
)
_PDF_FIGURE_RENDER_SMOKE_SOURCE = (
    r"\begin{figure}\includegraphics[width=1pt]{assets/probe.png}\end{figure}"
)
_PDF_FIGURE_RENDER_SMOKE_PREAMBLE = (
    r"\documentclass{article}\usepackage{graphicx}"
)


class RuntimeDependencyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerRuntimeReport:
    ok: bool
    commands: dict[str, str | None]
    table_render_smoke: str
    figure_render_smoke: str
    texlive_profile: str | None = None
    texlive_release: str | None = None
    texlive_package_smoke: str = "not-run"
    texlive_compile_smoke: str = "not-run"
    resolver: str | None = None
    managed_root: str | None = None
    manifest_path: str | None = None
    toolchain_fingerprint: str | None = None
    capabilities: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _TexLiveProbe:
    ok: bool
    profile: str | None
    release: str | None
    package_smoke: str
    compile_smoke: str
    error: str | None = None


def _run_tex_compile(command: str, source: str, *, workdir: Path) -> str | None:
    source_path = workdir / "runtime-smoke.tex"
    source_path.write_text(source, encoding="utf-8")
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
        result = subprocess.run(
            [
                command,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "-no-shell-escape",
                source_path.name,
            ],
            cwd=workdir,
            env=environment,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    pdf_path = workdir / "runtime-smoke.pdf"
    if result.returncode == 0 and pdf_path.is_file() and pdf_path.read_bytes().startswith(b"%PDF-"):
        return None
    log = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    return " ".join(log.split())[-1800:] or f"{command} did not produce a PDF"


def _run_bibliography_smoke(
    *,
    biber_command: str,
    bibtex_command: str,
    workdir: Path,
) -> str | None:
    (workdir / "bibliography-smoke.bib").write_text(
        _BIBLIOGRAPHY_SMOKE_BIB,
        encoding="utf-8",
    )
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
        biber_result = subprocess.run(
            [
                biber_command,
                "--tool",
                f"--output-directory={workdir}",
                "--output-format=biblatexml",
                "--output-file=bibliography-smoke.xml",
                "bibliography-smoke.bib",
            ],
            cwd=workdir,
            env=environment,
            capture_output=True,
            check=False,
            timeout=60,
        )
        (workdir / "bibliography-smoke.aux").write_text(
            "\\relax\n\\citation{runtime-smoke}\n"
            "\\bibstyle{IEEEtran}\n\\bibdata{bibliography-smoke}\n",
            encoding="utf-8",
        )
        bibtex_result = subprocess.run(
            [bibtex_command, "bibliography-smoke"],
            cwd=workdir,
            env=environment,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    xml_path = workdir / "bibliography-smoke.xml"
    bbl_path = workdir / "bibliography-smoke.bbl"
    if (
        biber_result.returncode == 0
        and bibtex_result.returncode == 0
        and xml_path.is_file()
        and "runtime-smoke" in xml_path.read_text(encoding="utf-8", errors="replace")
        and bbl_path.is_file()
        and r"\bibitem{runtime-smoke}" in bbl_path.read_text(
            encoding="utf-8", errors="replace"
        )
    ):
        return None
    output = biber_result.stdout + biber_result.stderr + bibtex_result.stdout + bibtex_result.stderr
    return " ".join(output.decode("utf-8", errors="replace").split())[-1800:]


def _run_pdf_tool_smoke(
    *,
    pdfinfo_command: str,
    pdftotext_command: str,
    workdir: Path,
) -> str | None:
    pdf_path = workdir / "runtime-smoke.pdf"
    try:
        info_result = subprocess.run(
            [pdfinfo_command, str(pdf_path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        text_result = subprocess.run(
            [pdftotext_command, str(pdf_path), "-"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    if (
        info_result.returncode == 0
        and "Pages:" in info_result.stdout
        and text_result.returncode == 0
        and text_result.stdout.strip()
    ):
        return None
    output = " ".join(
        (
            info_result.stdout
            + info_result.stderr
            + text_result.stdout
            + text_result.stderr
        ).split()
    )
    return output[-1800:] or "PDF info/text tools did not return usable output"


def _probe_texlive_runtime(
    toolchain: ResolvedToolchain,
    *,
    compile_smoke: bool,
) -> _TexLiveProbe:
    commands = toolchain.commands.as_dict()
    expected_profile = os.environ.get("ACADEMATE_TEXLIVE_PROFILE", toolchain.profile)
    expected_release = os.environ.get("ACADEMATE_TEXLIVE_RELEASE", toolchain.version)
    profile = toolchain.profile
    manifest_release = toolchain.version
    if profile != expected_profile or manifest_release != expected_release:
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=manifest_release,
            package_smoke="not-run",
            compile_smoke="not-run",
            error=(
                "The installed TeX Live runtime profile does not match the image contract: "
                f"expected {expected_profile}/{expected_release}, found "
                f"{profile or 'unknown'}/{manifest_release or 'unknown'}."
            ),
        )

    tex_command = commands.get("tex")
    kpsewhich_command = commands.get("kpsewhich")
    if tex_command is None or kpsewhich_command is None:
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=manifest_release,
            package_smoke="not-run",
            compile_smoke="not-run",
            error="TeX Live release probing requires tex and kpsewhich.",
        )
    try:
        version_result = subprocess.run(
            [tex_command, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=None,
            package_smoke="not-run",
            compile_smoke="not-run",
            error=f"Cannot execute the pinned TeX Live engine: {error}",
        )
    release_match = re.search(r"TeX Live\s+(\d{4})", version_result.stdout)
    actual_release = release_match.group(1) if release_match is not None else None
    if version_result.returncode != 0 or actual_release != expected_release:
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=actual_release,
            package_smoke="not-run",
            compile_smoke="not-run",
            error=(
                f"Expected TeX Live {expected_release}, but tex reported "
                f"{actual_release or 'an unknown release'}."
            ),
        )

    missing_packages: list[str] = []
    for tex_file in _REQUIRED_TEX_FILES:
        try:
            result = subprocess.run(
                [kpsewhich_command, tex_file],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            missing_packages.append(tex_file)
            continue
        if result.returncode != 0 or not result.stdout.strip():
            missing_packages.append(tex_file)
    if missing_packages:
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=actual_release,
            package_smoke="failed",
            compile_smoke="not-run",
            error=(
                "The pinned TeX Live profile is incomplete; kpsewhich could not resolve: "
                + ", ".join(missing_packages)
            ),
        )
    if not compile_smoke:
        return _TexLiveProbe(
            ok=True,
            profile=profile,
            release=actual_release,
            package_smoke="passed",
            compile_smoke="skipped",
        )

    pdflatex_command = commands.get("pdflatex")
    xelatex_command = commands.get("xelatex")
    biber_command = commands.get("biber")
    bibtex_command = commands.get("bibtex")
    pdfinfo_command = commands.get("pdfinfo")
    pdftotext_command = commands.get("pdftotext")
    if (
        pdflatex_command is None
        or xelatex_command is None
        or biber_command is None
        or bibtex_command is None
        or pdfinfo_command is None
        or pdftotext_command is None
    ):
        return _TexLiveProbe(
            ok=False,
            profile=profile,
            release=actual_release,
            package_smoke="passed",
            compile_smoke="failed",
            error="TeX Live compatibility smoke requires pdflatex and xelatex.",
        )
    with tempfile.TemporaryDirectory(prefix="texlive-runtime-smoke-") as temporary:
        workdir = Path(temporary)
        pdflatex_error = _run_tex_compile(
            pdflatex_command,
            _PDFLATEX_COMPATIBILITY_SMOKE_SOURCE,
            workdir=workdir,
        )
        if pdflatex_error is not None:
            return _TexLiveProbe(
                ok=False,
                profile=profile,
                release=actual_release,
                package_smoke="passed",
                compile_smoke="failed",
                error=f"TeX Live pdflatex compatibility smoke failed: {pdflatex_error}",
            )
        pdf_tool_error = _run_pdf_tool_smoke(
            pdfinfo_command=pdfinfo_command,
            pdftotext_command=pdftotext_command,
            workdir=workdir,
        )
        if pdf_tool_error is not None:
            return _TexLiveProbe(
                ok=False,
                profile=profile,
                release=actual_release,
                package_smoke="passed",
                compile_smoke="failed",
                error=f"PDF info/text compatibility smoke failed: {pdf_tool_error}",
            )
        for generated_path in workdir.glob("runtime-smoke.*"):
            generated_path.unlink()
        xelatex_error = _run_tex_compile(
            xelatex_command,
            _XELATEX_COMPATIBILITY_SMOKE_SOURCE,
            workdir=workdir,
        )
        if xelatex_error is not None:
            return _TexLiveProbe(
                ok=False,
                profile=profile,
                release=actual_release,
                package_smoke="passed",
                compile_smoke="failed",
                error=f"TeX Live xelatex compatibility smoke failed: {xelatex_error}",
            )
        bibliography_error = _run_bibliography_smoke(
            biber_command=biber_command,
            bibtex_command=bibtex_command,
            workdir=workdir,
        )
        if bibliography_error is not None:
            return _TexLiveProbe(
                ok=False,
                profile=profile,
                release=actual_release,
                package_smoke="passed",
                compile_smoke="failed",
                error=(
                    "TeX Live bibliography compatibility smoke failed: "
                    f"{bibliography_error}"
                ),
            )
    return _TexLiveProbe(
        ok=True,
        profile=profile,
        release=actual_release,
        package_smoke="passed",
        compile_smoke="passed",
    )


def _runtime_report(
    toolchain: ResolvedToolchain,
    *,
    ok: bool,
    table_render_smoke: str,
    figure_render_smoke: str,
    capabilities: dict[str, str],
    texlive_probe: _TexLiveProbe | None = None,
    error: str | None = None,
) -> WorkerRuntimeReport:
    return WorkerRuntimeReport(
        ok=ok,
        commands=toolchain.commands.as_dict(),
        table_render_smoke=table_render_smoke,
        figure_render_smoke=figure_render_smoke,
        texlive_profile=(texlive_probe.profile if texlive_probe else toolchain.profile),
        texlive_release=(texlive_probe.release if texlive_probe else toolchain.version),
        texlive_package_smoke=(texlive_probe.package_smoke if texlive_probe else "not-run"),
        texlive_compile_smoke=(texlive_probe.compile_smoke if texlive_probe else "not-run"),
        resolver=toolchain.resolver,
        managed_root=toolchain.managed_root,
        manifest_path=toolchain.manifest_path,
        toolchain_fingerprint=toolchain.fingerprint,
        capabilities=dict(capabilities),
        error=error,
    )


def probe_worker_runtime(
    renderer: LatexTableRenderer | None = None,
    figure_renderer: LatexFigureRenderer | None = None,
    *,
    toolchain: ResolvedToolchain,
    render_smoke: bool = True,
) -> WorkerRuntimeReport:
    commands = toolchain.commands
    candidate = renderer or LatexTableRenderer(
        latex_command=commands.latex,
        pdf_latex_command=commands.pdflatex,
        dvisvgm_command=commands.dvisvgm,
        pdf_to_svg_command=commands.pdftocairo,
        pdf_info_command=commands.pdfinfo,
        kpsewhich_command=commands.kpsewhich,
    )
    figure_candidate = figure_renderer or LatexFigureRenderer(
        latex_command=commands.latex,
        pdf_latex_command=commands.pdflatex,
        unicode_latex_command=commands.xelatex,
        dvisvgm_command=commands.dvisvgm,
        pdf_to_svg_command=commands.pdftocairo,
        kpsewhich_command=commands.kpsewhich,
    )
    capability_states = {
        capability.value: "not-run" for capability in toolchain.capabilities
    }
    texlive_probe = _probe_texlive_runtime(toolchain, compile_smoke=render_smoke)
    if not texlive_probe.ok:
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="not-run",
            figure_render_smoke="not-run",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=texlive_probe.error,
        )
    if not render_smoke:
        capability_states = dict.fromkeys(capability_states, "skipped")
        return _runtime_report(
            toolchain,
            ok=True,
            table_render_smoke="skipped",
            figure_render_smoke="skipped",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
        )
    for capability in (
        ToolchainCapability.PDFLATEX,
        ToolchainCapability.XELATEX,
        ToolchainCapability.BIBER_BIBLATEXML,
        ToolchainCapability.BIBTEX_IEEE,
        ToolchainCapability.PDF_TEXT,
        ToolchainCapability.PDF_INFO,
    ):
        capability_states[capability.value] = "passed"
    try:
        artifact = candidate.render(
            source_latex=_TABLE_RENDER_SMOKE_SOURCE,
            assets=_TABLE_RENDER_SMOKE_ASSETS,
        )
    except TableRenderError as error:
        capability_states[ToolchainCapability.TABLE_PNG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="failed",
            figure_render_smoke="not-run",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=f"Worker LaTeX dependency smoke test failed: {error}",
        )
    if artifact.mime_type != "image/png" or not artifact.content.startswith(b"\x89PNG"):
        capability_states[ToolchainCapability.TABLE_PNG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="failed",
            figure_render_smoke="not-run",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error="Worker LaTeX dependency smoke test did not produce a valid raster table.",
        )
    capability_states[ToolchainCapability.TABLE_PNG.value] = "passed"
    try:
        figure_artifact = figure_candidate.render(
            source_latex=_FIGURE_RENDER_SMOKE_SOURCE,
            preamble="",
            assets=_FIGURE_RENDER_SMOKE_ASSETS,
        )
    except FigureRenderError as error:
        capability_states[ToolchainCapability.FIGURE_SVG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=f"Worker LaTeX listing-Figure dependency smoke test failed: {error}",
        )
    if figure_artifact.mime_type != "image/svg+xml" or b"<svg" not in figure_artifact.content:
        capability_states[ToolchainCapability.FIGURE_SVG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error="Worker LaTeX listing-Figure smoke test did not produce a valid SVG.",
        )
    try:
        unicode_figure_artifact = figure_candidate.render(
            source_latex=_UNICODE_FIGURE_RENDER_SMOKE_SOURCE,
            preamble=_UNICODE_FIGURE_RENDER_SMOKE_PREAMBLE,
            assets={},
        )
    except FigureRenderError as error:
        capability_states[ToolchainCapability.FIGURE_SVG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=f"Worker XeLaTeX-to-SVG figure smoke test failed: {error}",
        )
    unicode_svg_width = re.search(
        rb"<svg[^>]*\bwidth=['\"]([0-9.]+)pt['\"]",
        unicode_figure_artifact.content[:2000],
    )
    if (
        unicode_figure_artifact.mime_type != "image/svg+xml"
        or b"<svg" not in unicode_figure_artifact.content
        or b"stroke" not in unicode_figure_artifact.content
        or unicode_svg_width is None
        or float(unicode_svg_width.group(1)) > 200
        or not unicode_figure_artifact.compiler.startswith("xelatex+")
    ):
        capability_states[ToolchainCapability.FIGURE_SVG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=(
                "Worker XeLaTeX-to-SVG smoke test did not preserve tightly cropped "
                "TikZ vector geometry."
            ),
        )
    capability_states[ToolchainCapability.FIGURE_SVG.value] = "passed"
    try:
        pdf_figure_artifact = figure_candidate.render(
            source_latex=_PDF_FIGURE_RENDER_SMOKE_SOURCE,
            preamble=_PDF_FIGURE_RENDER_SMOKE_PREAMBLE,
            assets=_TABLE_RENDER_SMOKE_ASSETS,
        )
    except FigureRenderError as error:
        capability_states[ToolchainCapability.FIGURE_PNG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error=f"Worker PDF-to-PNG figure smoke test failed: {error}",
        )
    if (
        pdf_figure_artifact.mime_type != "image/png"
        or not pdf_figure_artifact.content.startswith(b"\x89PNG\r\n\x1a\n")
        or not pdf_figure_artifact.compiler.startswith("pdflatex+pdftocairo")
    ):
        capability_states[ToolchainCapability.FIGURE_PNG.value] = "failed"
        return _runtime_report(
            toolchain,
            ok=False,
            table_render_smoke="passed",
            figure_render_smoke="failed",
            capabilities=capability_states,
            texlive_probe=texlive_probe,
            error="Worker PDF-to-PNG figure smoke test did not produce a valid PNG.",
        )
    capability_states[ToolchainCapability.FIGURE_PNG.value] = "passed"
    return _runtime_report(
        toolchain,
        ok=True,
        table_render_smoke="passed",
        figure_render_smoke="passed",
        capabilities=capability_states,
        texlive_probe=texlive_probe,
    )


def require_worker_runtime(
    renderer: LatexTableRenderer | None = None,
    figure_renderer: LatexFigureRenderer | None = None,
    *,
    toolchain: ResolvedToolchain,
) -> WorkerRuntimeReport:
    report = probe_worker_runtime(renderer, figure_renderer, toolchain=toolchain)
    if not report.ok:
        raise RuntimeDependencyError(report.error or "Worker runtime dependencies are unavailable.")
    return report
