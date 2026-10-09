"""The one model call per return: Gemini looks at the photos and fills the `judgment/v1` schema.

The model only OBSERVES (Round 2 design): the schema has no disposition field, and everything after this call is
deterministic code (`core/`). One call per unit, no tools, no repair turn; each attempt is bounded by
`returns_model_timeout_s`, and a busy, timed-out or unreachable call is retried once on the fallback model, so the
stage still ends inside the orchestrator's stage timeout (2 x 28 s + 2 s inside 75 s, D-O07). Anything that goes wrong
becomes a `JudgeError`, which `app.handle` turns into a pending record that keeps the captures.

Tests replace `app.get_judge` with a scripted judge; nothing here is exercised without a key.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .core.context import Bundle
from .core.images import sniff_mime
from .core.schemas import JudgmentV1, gemini_response_schema


class JudgeError(Exception):
    """The model did not give a usable answer. `code` becomes the record's error code."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.calls = 1  # model calls actually made before giving up


@dataclass(frozen=True)
class JudgeResult:
    judgment: JudgmentV1
    model: str  # the model version the provider reports (falls back to the requested id)
    usage: dict[str, int]
    latency_ms: int
    cost_usd: float | None = None
    calls: int = 1


def extract_json(text: str) -> dict[str, Any]:
    """The JSON object in the model's text (Round 2 `llm/loop.py: _extract_json`)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise JudgeError("model_output_invalid", "no JSON object in the model output")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise JudgeError("model_output_invalid", f"model output is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise JudgeError("model_output_invalid", "model output is not a JSON object")
    return data


def parse_judgment(text: str) -> JudgmentV1:
    raw = extract_json(text)
    try:
        return JudgmentV1.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise JudgeError("model_output_invalid",
                         f"model output failed judgment/v1 validation at {where or '<root>'}: {first.get('msg', '')}") from exc


class GeminiJudge:
    """Gemini through google-genai's `generateContent`. Not exercised in the tests (no key, no network)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise JudgeError("model_not_configured", "GEMINI_API_KEY is not set.")
        from google import genai
        from google.genai import types

        self.settings, self._types = settings, types
        self.client = genai.Client(api_key=settings.gemini_api_key,
                                   http_options=types.HttpOptions(timeout=int(settings.returns_model_timeout_s * 1000)))

    def _config(self, bundle: Bundle):
        t, s = self._types, self.settings
        config = t.GenerateContentConfig(
            system_instruction=bundle.system, response_mime_type="application/json",
            max_output_tokens=s.returns_max_output_tokens,
            thinking_config=t.ThinkingConfig(thinking_level=s.returns_thinking.upper()))
        if s.returns_output_mode == "json_schema":
            config.response_json_schema = gemini_response_schema()
        return config

    def judge(self, bundle: Bundle) -> JudgeResult:
        from google.genai import errors

        t = self._types
        contents = [t.Part.from_text(text=x) if kind == "text" else t.Part.from_bytes(data=x, mime_type=sniff_mime(x))
                    for kind, x in bundle.parts]
        started = time.perf_counter()
        s = self.settings
        # Attempt 1 on the configured model. A busy (429/5xx) or timed-out call is tried once more, on the fallback
        # model when one is set (a live demo should not die on one "model overloaded" reply). The record names the
        # model that actually answered (resp.model_version) and counts every call made.
        models = [s.returns_model] + ([s.returns_fallback_model] if s.returns_fallback_model else [s.returns_model])
        calls, resp, last = 0, None, None
        for attempt, model in enumerate(models[: 1 + max(0, s.returns_model_retries)]):
            if attempt:
                time.sleep(s.returns_retry_backoff_s)
            calls += 1
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=self._config(bundle))
                break
            except errors.APIError as exc:
                auth = exc.code in (401, 403)
                last = JudgeError("model_auth" if auth else "model_unavailable",
                                  f"model call failed (HTTP {exc.code}) on {model}", retryable=not auth)
                last.calls = calls
                if auth or exc.code not in (408, 429, 500, 502, 503, 504):
                    raise last from exc
            except Exception as exc:  # timeouts and connection failures surface as httpx errors
                last = JudgeError("model_unavailable", f"model call failed on {model}: {type(exc).__name__}")
                last.calls = calls
        if resp is None:
            raise last
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not resp.text:
            raise JudgeError("model_unavailable", "the model returned no text (empty or blocked response)")
        um = resp.usage_metadata
        usage = {"input_tokens": (um.prompt_token_count or 0) if um else 0,
                 "output_tokens": (um.candidates_token_count or 0) if um else 0,
                 "thinking_tokens": (um.thoughts_token_count or 0) if um else 0}
        cost = None
        if s.cost_per_1m_input_usd is not None and s.cost_per_1m_output_usd is not None:
            cost = round((usage["input_tokens"] * s.cost_per_1m_input_usd
                          + (usage["output_tokens"] + usage["thinking_tokens"]) * s.cost_per_1m_output_usd) / 1e6, 6)
        return JudgeResult(parse_judgment(resp.text), resp.model_version or model, usage, latency_ms, cost, calls=calls)
