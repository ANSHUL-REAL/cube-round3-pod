"""The Judgment session loop (§11.6): manual, stateful, budgeted.

- One session = all checks in one structured output (Rule 2). Normally one request; at most
  `RM_MAX_ROUND_TRIPS` requests including tool round trips, reserved from the daily quota before the first
  request (a session never starts that cannot finish) and released if unused.
- Conversation state lives on Google's side (`store=True`); every continuation is chained with
  `previous_interaction_id`. History is never rebuilt, edited or reordered; system instruction, tools,
  generation config and response format are re-sent identically on every turn; the model never changes
  mid-session.
- No sampling parameters. Tool choice `auto`. Thinking content is never requested (`thinking_summaries`
  is left off) and never stored.
- `json_prompted` mode: the schema is in the task text and one repair turn is allowed (counted as a request).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from returns_manager.config import Settings
from returns_manager.llm.client import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    ProviderError,
    SafetyBlockedError,
    SchemaError,
    TruncatedError,
)
from returns_manager.llm.context import ContextBundle
from returns_manager.llm.pricing import cost_usd_micros
from returns_manager.llm.prompts import get_prompt
from returns_manager.llm.quota import QuotaGuard, Role
from returns_manager.llm.schemas import JudgmentV1, gemini_response_schema
from returns_manager.llm.tools import ToolExecutor, ToolRecord, tool_definitions

_SAFETY_WORDS = ("safety", "blocked", "prohibited", "recitation", "blocklist")


@dataclass
class SessionTrace:
    """Everything spent and seen in a session; kept even when the session fails (for inspection_runs)."""

    model: str
    output_mode: str
    # This includes failed provider calls.  `requests` deliberately contains
    # responses only, because it supplies usage, latency and interaction IDs.
    requests_sent: int = 0
    requests: list[ModelResponse] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)

    @property
    def usage(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for r in self.requests:
            for k, v in r.usage.items():
                total[k] = total.get(k, 0) + v
        return total

    @property
    def cost_usd_micros(self) -> int | None:
        costs = [cost_usd_micros(self.model, r.usage) for r in self.requests]
        return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)

    @property
    def latency_ms(self) -> int:
        return sum(r.latency_ms for r in self.requests)

    @property
    def interaction_ids(self) -> list[str]:
        return [r.interaction_id for r in self.requests]

    @property
    def statuses(self) -> list[str]:
        return [r.status for r in self.requests]


@dataclass
class SessionResult:
    judgment: JudgmentV1
    raw_output: dict[str, Any]
    trace: SessionTrace
    crop_aliases: tuple[str, ...]
    reference_aliases: tuple[str, ...]


class SessionFailed(Exception):
    def __init__(self, cause: Exception, trace: SessionTrace) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.trace = trace


def _check_status(resp: ModelResponse) -> None:
    if resp.function_calls:
        return
    blob = json.dumps(resp.errors).lower()
    if resp.status == "failed":
        if any(w in blob for w in _SAFETY_WORDS):
            raise SafetyBlockedError(f"response blocked by safety filters ({resp.status})")
        raise ProviderError("server_error", f"interaction failed: {blob[:300]}")
    if resp.status in ("incomplete", "budget_exceeded"):
        if any(w in blob for w in _SAFETY_WORDS):
            raise SafetyBlockedError(f"response blocked by safety filters ({resp.status})")
        raise TruncatedError(f"interaction ended '{resp.status}' before a complete output")
    if resp.status == "cancelled":
        raise ProviderError("server_error", "interaction was cancelled by the provider")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise SchemaError("no JSON object in the model output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise SchemaError("model output is not a JSON object")
    return data


async def run_session(
    client: ModelClient,
    quota: QuotaGuard,
    bundle: ContextBundle,
    settings: Settings,
    *,
    role: Role = "judgment",
    model: str | None = None,
    thinking: str | None = None,
    max_round_trips: int | None = None,
    crop_budget: int | None = None,
    max_output_tokens: int | None = None,
) -> SessionResult:
    model_id = model or settings.rm_judgment_model
    rounds = max_round_trips or settings.rm_max_round_trips
    mode = settings.rm_output_mode
    trace = SessionTrace(model=model_id, output_mode=mode)
    executor = ToolExecutor(
        bundle=bundle,
        settings=settings,
        crop_budget=settings.rm_max_crops_per_session if crop_budget is None else crop_budget,
        crop_resolution=settings.rm_crop_resolution,
    )
    system = get_prompt("judgment").body
    tools = tool_definitions()
    generation_config: dict[str, Any] = {
        "thinking_level": thinking or settings.rm_judgment_thinking,
        # The bare string "auto", not {"allowed_tools": {"mode": "auto"}}: the installed SDK's
        # local type stubs permit "auto" in both places (ToolChoice = Union[ToolChoiceConfig,
        # ToolChoiceType]), but a live call proved the server itself rejects "auto" inside
        # allowed_tools.mode ("400: allowed_tools mode must be either ANY or VALIDATED") -
        # build-log.md's P5 spike verified only against the local source, not a live call, and
        # that documented shape turned out to be wrong; see finding F-023. This bare-string
        # form is the one actually verified live.
        "tool_choice": "auto",
        "max_output_tokens": max_output_tokens or settings.rm_max_output_tokens,
    }
    response_format = (
        {"type": "text", "mime_type": "application/json", "schema": gemini_response_schema()}
        if mode == "json_schema"
        else None
    )

    def request(input_items: list[dict[str, Any]], previous: str | None) -> ModelRequest:
        return ModelRequest(
            model=model_id,
            system_instruction=system,
            input=input_items,
            tools=tools,
            generation_config=generation_config,
            response_format=response_format,
            store=settings.rm_interaction_store,
            previous_interaction_id=previous,
        )

    try:
        async with quota.reserve(model_id, role, rounds) as reservation:
            await reservation.take()
            resp = await _send(client, request(bundle.input_items(), None), trace)
            trace.requests.append(resp)
            repaired = False
            for round_trip in range(1, rounds + 1):
                _check_status(resp)
                if resp.function_calls:
                    if round_trip == rounds:
                        raise SchemaError("no final JSON within the round-trip budget")
                    steps = executor.execute_all(resp.function_calls, round_trip)
                    trace.tools = executor.records
                    await reservation.take()
                    resp = await _send(client, request(steps, resp.interaction_id), trace)
                    trace.requests.append(resp)
                    continue
                try:
                    raw = (
                        json.loads(resp.output_text)
                        if mode == "json_schema"
                        else _extract_json(resp.output_text)
                    )
                    judgment = JudgmentV1.model_validate(raw)
                except (json.JSONDecodeError, ValidationError, SchemaError) as exc:
                    detail = str(exc)[:1500]
                    if mode == "json_prompted" and not repaired and round_trip < rounds:
                        repaired = True
                        fix = [
                            {
                                "type": "text",
                                "text": "Your JSON did not validate:\n"
                                + detail
                                + "\nReturn only the corrected JSON object.",
                            }
                        ]
                        await reservation.take()
                        resp = await _send(client, request(fix, resp.interaction_id), trace)
                        trace.requests.append(resp)
                        continue
                    raise SchemaError(f"model output failed judgment/v1 validation: {detail[:300]}") from None
                trace.tools = executor.records
                return SessionResult(
                    judgment=judgment,
                    raw_output=raw,
                    trace=trace,
                    crop_aliases=tuple(executor.crop_aliases),
                    reference_aliases=tuple(executor.reference_aliases),
                )
            raise SchemaError("no final JSON within the round-trip budget")
    except ProviderError as exc:
        # Let Reservation.__aexit__ release unused tokens before marking the
        # day exhausted.  Marking first would be undone by that release.
        if exc.error_class == "quota_exhausted":
            await quota.mark_exhausted(model_id, role)
        trace.tools = executor.records
        raise SessionFailed(exc, trace) from exc
    except Exception as exc:
        trace.tools = executor.records
        raise SessionFailed(exc, trace) from exc


async def _send(
    client: ModelClient,
    req: ModelRequest,
    trace: SessionTrace,
) -> ModelResponse:
    # A provider failure can occur before a response object exists.  Count it
    # before invoking the client so inspection_runs records every API attempt.
    trace.requests_sent += 1
    return await client.create(req)
