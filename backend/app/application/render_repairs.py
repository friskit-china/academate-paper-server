from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from typing import Any

from app.infrastructure.latex.katex import normalize_katex_latex
from app.infrastructure.latex.parser import normalize_formula_with_preamble


class FormulaSelectorError(ValueError):
    pass


class FormulaRepairValidationError(ValueError):
    pass


_KATEX_UNDEFINED_COMMAND = re.compile(
    r"Undefined control sequence:\s*(\\[A-Za-z@]+)",
    re.IGNORECASE,
)
_INLINE_LATEX_DELIMITER = re.compile(
    r"\$\$([\s\S]+?)\$\$|\$([^$]+?)\$|\\\[([\s\S]+?)\\\]|\\\((.+?)\\\)"
)
_LATEX_SEMANTIC_TOKEN = re.compile(r"\\#|#|\\[A-Za-z@]+|[A-Za-zΑ-Ωα-ω]+|\d+")
_LATEX_PRESENTATION_COMMANDS = {
    "aligned",
    "alignedat",
    "array",
    "begin",
    "bf",
    "big",
    "bigg",
    "biggl",
    "biggr",
    "bigl",
    "bigr",
    "boldmath",
    "boldsymbol",
    "cases",
    "displaystyle",
    "end",
    "frac",
    "gathered",
    "hbox",
    "left",
    "limits",
    "mathbb",
    "mathbf",
    "mathcal",
    "mathit",
    "mathrm",
    "mathsf",
    "mathtt",
    "matrix",
    "mbox",
    "operatorname",
    "over",
    "overline",
    "phantom",
    "pmatrix",
    "raisebox",
    "right",
    "rm",
    "scriptscriptstyle",
    "scriptstyle",
    "sqrt",
    "split",
    "substack",
    "text",
    "textbf",
    "textit",
    "textrm",
    "textstyle",
    "underline",
    "vphantom",
    "vmatrix",
    "widehat",
    "widetilde",
}
_LATEX_SEMANTIC_ALIASES = {
    "ge": "geq",
    "goto": "to",
    "le": "leq",
    "ne": "neq",
}


_ALLOWED_PATH_KEYS = {
    "caption_segments",
    "item_segments",
    "parts",
    "row_segments",
    "segments",
    "title_segments",
    "units",
}


def formula_text_hash(latex: str) -> str:
    return hashlib.sha256(latex.encode("utf-8")).hexdigest()


def _latex_semantic_tokens(latex: str) -> Counter[str]:
    """Extract symbols/identifiers while ignoring presentation-only wrappers."""

    tokens: Counter[str] = Counter()
    for match in _LATEX_SEMANTIC_TOKEN.finditer(latex):
        raw = match.group(0)
        token = raw[1:] if raw.startswith("\\") else raw
        lowered = token.casefold()
        if lowered in _LATEX_PRESENTATION_COMMANDS:
            continue
        tokens[_LATEX_SEMANTIC_ALIASES.get(lowered, lowered)] += 1
    return tokens


def inline_latex_render_payload(
    value: str,
    *,
    renderer_preamble: str | None = None,
) -> dict[str, Any]:
    """Build stable, selector-addressable segments for metadata rich text.

    Abstract metadata does not pass through the full LaTeX document parser, but its inline
    formula repairs still need the same server-verified ``["segments", index]`` contract as
    body blocks. Keep the delimiters out of the canonical math value and preserve every plain
    text span so browser and server indexes remain identical.
    """
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in _INLINE_LATEX_DELIMITER.finditer(value):
        if match.start() > cursor:
            segments.append({"type": "text", "text": value[cursor : match.start()]})
        source_latex = next(group for group in match.groups() if group is not None).strip()
        latex = (
            normalize_formula_with_preamble(source_latex, renderer_preamble)
            if renderer_preamble is not None
            else normalize_katex_latex(source_latex)
        )
        if latex:
            segments.append(
                {
                    "type": "math",
                    "latex": latex,
                    "display": match.group(1) is not None or match.group(3) is not None,
                }
            )
        cursor = match.end()
    if cursor < len(value):
        segments.append({"type": "text", "text": value[cursor:]})
    payload: dict[str, Any] = {"text": value, "segments": segments}
    if renderer_preamble is not None:
        payload["macro_context_hash"] = hashlib.sha256(
            renderer_preamble.encode("utf-8")
        ).hexdigest()
    return payload


def hydrate_inline_latex_render_payload(payload: object, value: str) -> dict[str, Any]:
    """Add addressable metadata segments without overwriting existing repair audit data."""
    current = dict(payload) if isinstance(payload, dict) else {}
    if isinstance(current.get("segments"), list):
        return current
    return {**current, **inline_latex_render_payload(value)}


def validate_katex_repair(
    *,
    source_latex: str,
    corrected_latex: str,
    renderer_error: str,
) -> None:
    """Reject model output that demonstrably preserves the reported KaTeX failure."""
    source = source_latex.strip()
    corrected = corrected_latex.strip()
    if not corrected:
        raise FormulaRepairValidationError(
            "The KaTeX repair returned no corrected LaTeX."
        )
    if corrected == source:
        raise FormulaRepairValidationError(
            "The KaTeX repair returned the failing LaTeX unchanged."
        )
    match = _KATEX_UNDEFINED_COMMAND.search(renderer_error)
    unsupported_command: str | None = None
    if match is not None:
        command = match.group(1)
        unsupported_command = command[1:].casefold()
        if re.search(rf"{re.escape(command)}(?![A-Za-z@])", corrected):
            raise FormulaRepairValidationError(
                f"The KaTeX repair still contains the unsupported command {command}."
            )
    source_tokens = _latex_semantic_tokens(source)
    if unsupported_command is not None:
        source_tokens.pop(unsupported_command, None)
    corrected_tokens = _latex_semantic_tokens(corrected)
    if corrected_tokens != source_tokens:
        introduced = corrected_tokens - source_tokens
        removed = source_tokens - corrected_tokens
        details: list[str] = []
        if introduced:
            details.append(f"introduced: {', '.join(sorted(introduced)[:5])}")
        if removed:
            details.append(f"removed: {', '.join(sorted(removed)[:5])}")
        raise FormulaRepairValidationError(
            "The KaTeX repair changed symbols, identifiers, or numbers from the canonical "
            f"source ({'; '.join(details)}). Format repair must preserve canonical content."
        )


def normalize_formula_selector(selector: object) -> dict[str, Any]:
    if not isinstance(selector, dict):
        raise FormulaSelectorError("An inline formula selector is required.")
    path = selector.get("path")
    exact_latex = selector.get("exact_latex")
    text_hash = selector.get("text_hash")
    if not isinstance(path, list) or not 2 <= len(path) <= 8:
        raise FormulaSelectorError("The inline formula selector path is invalid.")
    normalized_path: list[str | int] = []
    for part in path:
        if isinstance(part, bool):
            raise FormulaSelectorError("The inline formula selector path is invalid.")
        if isinstance(part, int):
            if not 0 <= part <= 100_000:
                raise FormulaSelectorError("The inline formula selector index is invalid.")
            normalized_path.append(part)
            continue
        if isinstance(part, str) and part in _ALLOWED_PATH_KEYS:
            normalized_path.append(part)
            continue
        raise FormulaSelectorError("The inline formula selector path is invalid.")
    if not isinstance(exact_latex, str) or not 1 <= len(exact_latex) <= 16_000:
        raise FormulaSelectorError("The inline formula exact text is invalid.")
    expected_hash = formula_text_hash(exact_latex)
    if text_hash is not None and text_hash != expected_hash:
        raise FormulaSelectorError("The inline formula text hash is invalid.")
    return {
        "path": normalized_path,
        "exact_latex": exact_latex,
        "text_hash": expected_hash,
    }


def formula_selector_key(selector: object) -> str:
    normalized = normalize_formula_selector(selector)
    canonical = json.dumps(
        {"path": normalized["path"], "text_hash": normalized["text_hash"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def selected_formula(payload: object, selector: object) -> str:
    normalized = normalize_formula_selector(selector)
    current: object = payload
    for part in normalized["path"]:
        if isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
        else:
            if not isinstance(current, list) or part >= len(current):
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
    if (
        not isinstance(current, dict)
        or current.get("type") not in {"math", "equation"}
        or not isinstance(current.get("latex"), str)
    ):
        raise FormulaSelectorError("The inline formula selector does not target math.")
    latex = current["latex"]
    if latex != normalized["exact_latex"] or formula_text_hash(latex) != normalized["text_hash"]:
        raise FormulaSelectorError("The inline formula changed before repair.")
    return latex


def selected_formula_node(payload: object, selector: object) -> dict[str, Any]:
    """Return a defensive copy of the exact selector-addressed math node."""

    normalized = normalize_formula_selector(selector)
    current: object = payload
    for part in normalized["path"]:
        if isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
        else:
            if not isinstance(current, list) or part >= len(current):
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
    if (
        not isinstance(current, dict)
        or current.get("type") not in {"math", "equation"}
        or current.get("latex") != normalized["exact_latex"]
    ):
        raise FormulaSelectorError("The inline formula changed before rendering.")
    return deepcopy(current)


def apply_formula_latex_render(
    payload: object,
    selector: object,
    render_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Attach a deterministic TeX render without changing the formula text."""

    if not isinstance(payload, dict):
        raise FormulaSelectorError("The inline formula payload is invalid.")
    normalized = normalize_formula_selector(selector)
    updated = deepcopy(payload)
    current: object = updated
    for part in normalized["path"]:
        if isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
        else:
            if not isinstance(current, list) or part >= len(current):
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
    if (
        not isinstance(current, dict)
        or current.get("type") not in {"math", "equation"}
        or current.get("latex") != normalized["exact_latex"]
    ):
        raise FormulaSelectorError("The inline formula changed before rendering.")
    current["latex_render"] = deepcopy(render_metadata)
    return updated


def propagate_formula_latex_render(
    payload: object,
    *,
    math_node_id: str | None,
    latex: str,
    render_metadata: dict[str, Any],
) -> object:
    """Reuse one immutable TeX asset in translated payloads with matching math."""

    updated = deepcopy(payload)

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") in {"math", "equation"}:
            same_id = bool(math_node_id) and value.get("math_node_id") == math_node_id
            same_latex = value.get("latex") == latex
            if same_id or same_latex:
                value["latex_render"] = deepcopy(render_metadata)
        for item in value.values():
            visit(item)

    visit(updated)
    return updated


def formula_matches_deterministic_normalization(payload: object, selector: object) -> bool:
    """Return true only when the selector path contains its exact local normalization.

    This supports durable-task reconciliation after a parser upgrade or an equivalent sibling
    repair has already fixed the derived payload.  The path and original hash are still fully
    validated, and an unchanged or unknown formula never matches.
    """

    normalized = normalize_formula_selector(selector)
    current: object = payload
    for part in normalized["path"]:
        if isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        else:
            if not isinstance(current, list) or part >= len(current):
                return False
            current = current[part]
    if (
        not isinstance(current, dict)
        or current.get("type") not in {"math", "equation"}
        or not isinstance(current.get("latex"), str)
    ):
        return False
    expected = normalize_katex_latex(normalized["exact_latex"])
    return expected != normalized["exact_latex"] and current["latex"] == expected


def equation_matches_deterministic_normalization(
    source_payload: object,
    render_payload: object,
    renderer_error: str,
) -> bool:
    """Verify that a parser upgrade removed the exact command reported by KaTeX.

    Whole-equation repair tasks predate selector-addressed inline repair.  Reconciliation is safe
    only when the current render payload exactly equals the normalization of canonical source and
    the undefined command named by the original renderer is no longer present.  Unknown or merely
    changed formulas never match.
    """

    if not isinstance(source_payload, dict) or not isinstance(render_payload, dict):
        return False
    source_latex = source_payload.get("latex")
    rendered_latex = render_payload.get("latex")
    if not isinstance(source_latex, str) or not isinstance(rendered_latex, str):
        return False
    command_match = _KATEX_UNDEFINED_COMMAND.search(renderer_error)
    if command_match is None:
        return False
    unsupported_command = command_match.group(1)
    expected = normalize_katex_latex(source_latex)
    if expected == source_latex.strip() or rendered_latex != expected:
        return False
    return re.search(rf"{re.escape(unsupported_command)}(?![A-Za-z@])", expected) is None


def apply_formula_repair(
    payload: object,
    selector: object,
    corrected_latex: str,
    *,
    repair_metadata: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FormulaSelectorError("The inline formula payload is invalid.")
    normalized = normalize_formula_selector(selector)
    updated = deepcopy(payload)
    current: object = updated
    for part in normalized["path"]:
        if isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
        else:
            if not isinstance(current, list) or part >= len(current):
                raise FormulaSelectorError("The inline formula selector no longer resolves.")
            current = current[part]
    if (
        not isinstance(current, dict)
        or current.get("type") not in {"math", "equation"}
        or current.get("latex") != normalized["exact_latex"]
    ):
        raise FormulaSelectorError("The inline formula changed before repair.")
    current["latex"] = corrected_latex
    repairs = updated.get("inline_formula_repairs")
    if not isinstance(repairs, dict):
        repairs = {}
    repairs[formula_selector_key(normalized)] = {
        **repair_metadata,
        "path": normalized["path"],
        "original_latex": normalized["exact_latex"],
        "corrected_latex": corrected_latex,
        "text_hash": normalized["text_hash"],
    }
    updated["inline_formula_repairs"] = repairs
    return updated


def formula_repair_metadata(payload: object, selector: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    repairs = payload.get("inline_formula_repairs")
    if not isinstance(repairs, dict):
        return None
    metadata = repairs.get(formula_selector_key(selector))
    return metadata if isinstance(metadata, dict) else None
