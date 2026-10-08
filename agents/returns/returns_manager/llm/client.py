"""The model client seam (§11.1): one narrow protocol, a Gemini implementation and a replay implementation.

`ModelRequest` is exactly the body of one `interactions.create` call (verified against google-genai 2.25.0,
build log P5 spike). `ModelResponse` is the provider-neutral view the session loop needs; `raw` keeps the full
JSON as returned (for cassettes and audit) — thinking content is never requested (`thinking_summaries` stays
off) and never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from returns_manager.errors import ReturnsManagerError


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system_instruction: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    generation_config: dict[str, Any]
    response_format: dict[str, Any] | None
    store: bool = True
    previous_interaction_id: str | None = None

    def body(self) -> dict[str, Any]:
        """Keyword arguments for `client.interactions.create` (no sampling parameters, ever)."""
        out: dict[str, Any] = {
            "model": self.model,
            "system_instruction": self.system_instruction,
            "input": self.input,
            "generation_config": self.generation_config,
            "store": self.store,
        }
        if self.tools:
            out["tools"] = self.tools
        if self.response_format is not None:
            out["response_format"] = self.response_format
        if self.previous_interaction_id:
            out["previous_interaction_id"] = self.previous_interaction_id
        return out


@dataclass(frozen=True)
class FunctionCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    interaction_id: str
    status: str
    output_text: str
    function_calls: list[FunctionCall]
    usage: dict[str, int]
    errors: list[dict[str, Any]]
    latency_ms: int
    raw: dict[str, Any] = field(repr=False)


class ModelClient(Protocol):
    async def create(self, request: ModelRequest) -> ModelResponse: ...


# ── provider errors (classified by jobs.retry.classify_error) ─────────────────


class ProviderError(ReturnsManagerError):
    """A classified failure of a model request. `error_class` is one of the §10.4 classes."""

    def __init__(
        self,
        error_class: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.status_code = status_code
        self.retry_after_s = retry_after_s


class SafetyBlockedError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__("safety_blocked", message)


class TruncatedError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__("truncated", message)


class SchemaError(ProviderError):
    """The model's final output failed JSON parsing or `judgment/v1` validation."""

    def __init__(self, message: str) -> None:
        super().__init__("schema_error", message)


def normalize_usage(raw_usage: dict[str, Any] | None) -> dict[str, int]:
    """Keep the provider's integer token totals under their own names (§7.4 `usage`)."""
    keys = (
        "total_input_tokens",
        "total_output_tokens",
        "total_thought_tokens",
        "total_cached_tokens",
        "total_tool_use_tokens",
        "total_tokens",
    )
    raw_usage = raw_usage or {}
    return {k: int(raw_usage.get(k) or 0) for k in keys}


def response_from_raw(raw: dict[str, Any], latency_ms: int) -> ModelResponse:
    """Build the neutral response from the interaction JSON (same code path for live and replay)."""
    steps = raw.get("steps") or []
    calls = [
        FunctionCall(id=str(s["id"]), name=str(s["name"]), arguments=dict(s.get("arguments") or {}))
        for s in steps
        if s.get("type") == "function_call"
    ]
    # output_text: text of the final model_output step(s) after the last user input (mirrors the SDK helper).
    texts: list[str] = []
    for step in reversed(steps):
        kind = step.get("type")
        if kind == "user_input" or (kind != "model_output" and texts):
            break
        if kind == "model_output":
            for item in reversed(step.get("content") or []):
                if item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
    errors = [dict(e) for e in raw.get("errors") or []]
    for step in steps:
        if step.get("type") == "model_output" and step.get("error"):
            errors.append({"step_error": step["error"]})
    return ModelResponse(
        interaction_id=str(raw.get("id") or ""),
        status=str(raw.get("status") or ""),
        output_text="".join(reversed(texts)),
        function_calls=calls,
        usage=normalize_usage(raw.get("usage")),
        errors=errors,
        latency_ms=latency_ms,
        raw=raw,
    )
