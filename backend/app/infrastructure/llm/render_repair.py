from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.infrastructure.llm.provider_http import (
    provider_http_error_message,
    read_provider_http_error,
)


class RenderRepairError(RuntimeError):
    pass


ReasoningEffort = Literal["off", "low", "medium", "high"]


def _openai_reasoning_parameters(effort: ReasoningEffort) -> dict[str, Any]:
    # `minimal` is supported by more GPT-5 variants than `none`, so it is the
    # safest approximation of the provider-neutral `off` tier.
    native_effort = {
        "off": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
    }[effort]
    return {"reasoning": {"effort": native_effort}}


def _chat_reasoning_parameters(provider: str, effort: ReasoningEffort) -> dict[str, Any]:
    if provider in {"glm", "deepseek"}:
        if effort == "off":
            return {"thinking": {"type": "disabled"}}
        # These providers currently expose only high/max as distinct enabled
        # levels; low and medium are documented compatibility aliases for high.
        native_effort = "max" if effort == "high" else "high"
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": native_effort,
        }
    if provider == "kimi":
        if effort == "off":
            return {"thinking": {"type": "disabled"}}
        native_effort = "max" if effort == "high" else effort
        return {
            "thinking": {
                "type": "enabled",
                "effort": native_effort,
            }
        }
    # An arbitrary OpenAI-compatible endpoint may reject vendor-specific
    # fields. Leave its native/default behavior untouched.
    return {}


class RenderRepairClient(Protocol):
    def repair(
        self,
        *,
        block_type: str,
        source_latex: str,
        renderer: str,
        error_message: str,
        available_assets: list[str],
    ) -> dict[str, Any]: ...


def _http_error_message(error: urllib.error.HTTPError, client_request_id: str) -> str:
    diagnostic = read_provider_http_error(error, client_request_id)
    return provider_http_error_message("LLM render repair", diagnostic)


_FORBIDDEN_LATEX = re.compile(
    r"\\(?:write18|write|openout|read|input|include|usepackage|documentclass|"
    r"begin\s*\{document\}|end\s*\{document\})\b",
    re.IGNORECASE,
)

RENDER_REPAIR_MAX_LATEX_CHARS = 16_000


def _repair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "corrected_latex": {"type": "string"},
            "plain_text": {"type": "string"},
            "table_rows": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "asset_paths": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "corrected_latex",
            "plain_text",
            "table_rows",
            "asset_paths",
            "explanation",
            "confidence",
        ],
        "additionalProperties": False,
    }


def _repair_prompt(
    *,
    block_type: str,
    source_latex: str,
    renderer: str,
    error_message: str,
    available_assets: list[str],
) -> str:
    return json.dumps(
        {
            "block_type": block_type,
            "renderer": renderer,
            "renderer_error": error_message[:2000],
            "source_latex": source_latex[:RENDER_REPAIR_MAX_LATEX_CHARS],
            "available_asset_paths": available_assets[:500],
        },
        ensure_ascii=False,
    )


def _validate_repaired_payload(repaired: object, available_assets: list[str]) -> dict[str, Any]:
    if not isinstance(repaired, dict):
        raise RenderRepairError("LLM render repair returned an invalid payload.")
    corrected = repaired.get("corrected_latex")
    plain_text = repaired.get("plain_text")
    explanation = repaired.get("explanation")
    confidence = repaired.get("confidence")
    table_rows = repaired.get("table_rows")
    asset_paths = repaired.get("asset_paths")
    if not all(isinstance(value, str) for value in (corrected, plain_text, explanation)):
        raise RenderRepairError("LLM render repair returned invalid text fields.")
    if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        raise RenderRepairError("LLM render repair returned an invalid confidence.")
    if (
        len(corrected) > RENDER_REPAIR_MAX_LATEX_CHARS
        or len(plain_text) > RENDER_REPAIR_MAX_LATEX_CHARS
        or len(explanation) > 2000
    ):
        raise RenderRepairError("LLM render repair exceeded output limits.")
    if _FORBIDDEN_LATEX.search(corrected):
        raise RenderRepairError("LLM render repair proposed forbidden LaTeX commands.")
    if not isinstance(table_rows, list) or not all(
        isinstance(row, list)
        and len(row) <= 50
        and all(isinstance(cell, str) and len(cell) <= 2000 for cell in row)
        for row in table_rows[:200]
    ):
        raise RenderRepairError("LLM render repair returned invalid table rows.")
    allowed_assets = set(available_assets)
    if not isinstance(asset_paths, list) or not all(
        isinstance(path, str) and path in allowed_assets for path in asset_paths
    ):
        raise RenderRepairError("LLM render repair returned an unknown asset path.")
    return {
        "corrected_latex": corrected.strip(),
        "plain_text": plain_text.strip(),
        "table_rows": table_rows[:200],
        "asset_paths": asset_paths,
        "explanation": explanation.strip(),
        "confidence": float(confidence),
    }


RENDER_REPAIR_SYSTEM_PROMPT = (
    "Repair one LaTeX content block for safe browser rendering. Return only one JSON object "
    "matching the supplied JSON schema. Never add document-level commands, file reads, shell "
    "commands, packages, scripts, URLs, or executable code. Preserve the author's meaning. "
    "For tables, corrected_latex must contain one complete, safe tabular, tabularx, or longtable "
    "environment that preserves the table; also provide plain cell strings when possible. "
    "For figures, choose asset paths only from "
    "available_asset_paths. For KaTeX failures, corrected_latex must differ from the input "
    "and must remove or replace the exact construct named by renderer_error. Leave fields "
    "empty when they do not apply."
)


@dataclass(frozen=True, slots=True)
class OpenAIResponsesRenderRepairClient:
    api_key: str = field(repr=False)
    model: str
    api_base: str = ""
    timeout_seconds: int = 60
    reasoning_effort: ReasoningEffort = "off"

    def _endpoint(self) -> str:
        base = self.api_base.strip().rstrip("/") or "https://api.openai.com/v1"
        return base if base.endswith("/responses") else f"{base}/responses"

    @staticmethod
    def _schema() -> dict[str, Any]:
        return _repair_schema()

    def repair(
        self,
        *,
        block_type: str,
        source_latex: str,
        renderer: str,
        error_message: str,
        available_assets: list[str],
    ) -> dict[str, Any]:
        prompt = _repair_prompt(
            block_type=block_type,
            source_latex=source_latex,
            renderer=renderer,
            error_message=error_message,
            available_assets=available_assets,
        )
        payload = {
            "model": self.model,
            "instructions": RENDER_REPAIR_SYSTEM_PROMPT,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "paper_render_repair",
                    "strict": True,
                    "schema": self._schema(),
                }
            },
        }
        payload.update(_openai_reasoning_parameters(self.reasoning_effort))
        client_request_id = str(uuid.uuid4())
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Client-Request-Id": client_request_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RenderRepairError(_http_error_message(error, client_request_id)) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RenderRepairError(f"LLM render repair request failed: {error}") from error

        output_text = next(
            (
                content.get("text")
                for item in result.get("output", [])
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") == "output_text" and isinstance(content.get("text"), str)
            ),
            None,
        )
        if output_text is None:
            raise RenderRepairError("LLM render repair returned no structured text output.")
        try:
            repaired = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise RenderRepairError("LLM render repair returned invalid JSON.") from error
        return _validate_repaired_payload(repaired, available_assets)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleChatRenderRepairClient:
    api_key: str = field(repr=False)
    model: str
    api_base: str
    timeout_seconds: int = 60
    provider: str = "openai_compatible"
    reasoning_effort: ReasoningEffort = "off"

    def _endpoint(self) -> str:
        base = self.api_base.strip().rstrip("/")
        if not base:
            raise RenderRepairError("The configured LLM API base URL is empty.")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def repair(
        self,
        *,
        block_type: str,
        source_latex: str,
        renderer: str,
        error_message: str,
        available_assets: list[str],
    ) -> dict[str, Any]:
        prompt = _repair_prompt(
            block_type=block_type,
            source_latex=source_latex,
            renderer=renderer,
            error_message=error_message,
            available_assets=available_assets,
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{RENDER_REPAIR_SYSTEM_PROMPT}\nJSON schema:\n"
                        f"{json.dumps(_repair_schema(), ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        payload.update(_chat_reasoning_parameters(self.provider, self.reasoning_effort))
        client_request_id = str(uuid.uuid4())
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Client-Request-Id": client_request_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RenderRepairError(_http_error_message(error, client_request_id)) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RenderRepairError(f"LLM render repair request failed: {error}") from error

        try:
            output_text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RenderRepairError(
                "LLM render repair returned no structured text output."
            ) from error
        if not isinstance(output_text, str) or not output_text.strip():
            raise RenderRepairError("LLM render repair returned no structured text output.")
        try:
            repaired = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise RenderRepairError("LLM render repair returned invalid JSON.") from error
        return _validate_repaired_payload(repaired, available_assets)


SUPPORTED_LLM_PROVIDERS = frozenset({"openai", "deepseek", "kimi", "glm", "openai_compatible"})


def build_render_repair_client(
    *,
    api_key: str,
    model: str,
    api_base: str,
    provider: str,
    reasoning_effort: ReasoningEffort = "off",
) -> RenderRepairClient:
    normalized_provider = provider.strip().lower()
    if normalized_provider == "openai":
        return OpenAIResponsesRenderRepairClient(
            api_key=api_key,
            model=model,
            api_base=api_base,
            reasoning_effort=reasoning_effort,
        )
    if normalized_provider in SUPPORTED_LLM_PROVIDERS:
        return OpenAICompatibleChatRenderRepairClient(
            api_key=api_key,
            model=model,
            api_base=api_base,
            provider=normalized_provider,
            reasoning_effort=reasoning_effort,
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")
