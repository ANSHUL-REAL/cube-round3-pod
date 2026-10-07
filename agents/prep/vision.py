"""The vision step: ask a model what is VISIBLE on the prepped unit. It never decides anything.

Ported from the Round 2 reference (vision.ts). Changes: the model is not told the expected FNSKU (a model that
knows the answer tends to confirm it; the rules compare the code it transcribed), the answer is one batched call per
unit, and every failure is a typed PerceptionError so the agent can return a pending record instead of an
UNCERTAIN-looking success.

``Observer`` is the seam the tests use: a scripted observer returns an ``Observed`` with no network call.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from .requirements import CheckDef
from .settings import Settings

PROMPT_VERSION = "prep-vision/1"
Band = Literal["high", "medium", "low"]
Status = Literal["met", "not_met", "cant_tell"]
Limit = Literal["blurry", "glare", "too_dark", "low_resolution", "cropped", "out_of_frame", "occluded",
                "not_shown", "other"]


class PerceptionError(Exception):
    """The model did not give a usable answer. ``attempts`` is how many calls were made (for model.calls)."""

    def __init__(self, message: str, *, code: str = "model_unavailable", attempts: int = 0):
        super().__init__(message)
        self.code, self.attempts = code, attempts


class PhotoQuality(BaseModel):
    photo_index: int
    usable: bool
    issues: list[str]


class LabelRead(BaseModel):
    value: str | None
    photo_index: int | None
    legible: bool
    confidence: Band


class Observation(BaseModel):
    check_id: str
    status: Status
    confidence: Band
    photo_index: int | None
    location: str
    evidence: str
    limit: Limit | None
    marks_visible: list[str]


class VisionResponse(BaseModel):
    photo_quality: list[PhotoQuality]
    label_text_read: LabelRead
    observations: list[Observation]


@dataclass
class Observed:
    """What one model call returned, plus what it cost."""

    response: VisionResponse
    model: str
    model_version: str
    provider: str | None
    prompt_version: str = PROMPT_VERSION
    usage: dict = field(default_factory=dict)  # input_tokens, output_tokens, thinking_tokens
    latency_ms: int | None = None
    attempts: int = 1
    cost_usd: float | None = None


class Observer(Protocol):
    model: str
    provider: str | None

    def observe(self, photos: list[bytes], checks: list[CheckDef]) -> Observed: ...


SYSTEM = "\n".join([
    "You are the OBSERVATION component of an automated warehouse prep-inspection system.",
    "Your ONLY job is to report, strictly and literally, what is visible in the photographs of one prepped unit.",
    "You do NOT decide pass or fail. A separate rules engine does that from your observations.",
    "",
    "Hard rules:",
    "1. Describe only what you can actually see. Never assume, infer, or guess what is likely.",
    '2. For each check choose status: "met" (you can clearly SEE the condition described), "not_met" (you can '
    'clearly SEE it violated or absent), or "cant_tell" (the area is not shown, is blurry, glary, cropped or dark, '
    "or you are unsure). When in any doubt, use cant_tell.",
    '3. Use confidence "high" only when the relevant area is clearly visible and in focus; "low" if anything is '
    "ambiguous.",
    "4. Cite which photo (photo_index, starting at 1) and where in it (location, for example lower-right corner). "
    "With cant_tell and nothing to cite, use photo_index null and say why in limit.",
    "5. The evidence field must describe the specific visible thing you based the status on. If you cannot point "
    "to something visible, use cant_tell and leave evidence empty.",
    "6. For the barcode label, transcribe the code text EXACTLY as printed in label_text_read. If it is unreadable, "
    "set legible=false and value=null. Never invent, complete or auto-correct characters.",
    "7. Rate every photo: usable=false if it is blurry, glary, cropped, too dark, out of frame or too "
    "low-resolution to judge, and list the issues.",
    "8. Output only a single JSON object that matches the response schema. No prose, no markdown.",
])


def build_prompt(checks: list[CheckDef], n_photos: int) -> str:
    """The user text. It carries the check ids and what to look for, never an expected value or a verdict."""
    listing = "\n".join(f'  - check_id "{c.check_id}": {c.question}' for c in checks)
    return "\n".join([
        f"There are {n_photos} photo(s) of one prepped unit, in order. photo_index starts at 1.",
        "",
        "Observe these checks (use these exact check_id values, one observation each):",
        listing,
        "",
        "Also: transcribe the barcode label text (label_text_read) and rate every photo's usability.",
        "For every observation fill all fields. marks_visible is an empty list for every check except "
        "handling_marks_present. limit is null unless status is cant_tell (then use one of: blurry, glare, too_dark, "
        "low_resolution, cropped, out_of_frame, occluded, not_shown, other).",
    ])


def extract_json(text: str) -> dict:
    """The JSON object in a model answer, tolerating a markdown fence or stray text around it."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    body = fenced.group(1) if fenced else text
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the model answer")
    return json.loads(body[start:end + 1])


def parse_response(text: str) -> VisionResponse:
    try:
        return VisionResponse.model_validate(extract_json(text))
    except (ValueError, ValidationError) as exc:
        raise PerceptionError(f"the model's answer did not match the schema: {str(exc)[:300]}",
                              code="model_output_invalid") from exc


def cost_usd(usage: dict, settings: Settings) -> float | None:
    """Tokens x the configured prices. Thinking tokens bill as output. None when prices are not configured."""
    pin, pout = settings.cost_per_1m_input_usd, settings.cost_per_1m_output_usd
    if pin is None or pout is None or not usage:
        return None
    out = usage.get("output_tokens", 0) + usage.get("thinking_tokens", 0)
    return round((usage.get("input_tokens", 0) * pin + out * pout) / 1_000_000, 6)


RETRYABLE = {429, 500, 502, 503, 504}


class GeminiObserver:
    """One batched Gemini call per unit (all photos, all checks). Needs GEMINI_API_KEY."""

    provider = "google"

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise PerceptionError("GEMINI_API_KEY is not set.", code="model_not_configured")
        from google import genai
        from google.genai import types

        self.settings, self.model = settings, settings.prep_gemini_model
        self._types = types
        self.client = genai.Client(api_key=settings.gemini_api_key,
                                   http_options=types.HttpOptions(timeout=int(settings.prep_timeout_s * 1000)))

    def _config(self):
        return self._types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json",
                                                 response_schema=VisionResponse, temperature=0.0)

    def observe(self, photos: list[bytes], checks: list[CheckDef]) -> Observed:
        from google.genai import errors

        import httpx

        t = self._types
        contents = [t.Part.from_bytes(data=p, mime_type="image/jpeg") for p in photos]
        contents.append(build_prompt(checks, len(photos)))
        attempts, last = self.settings.prep_max_retries + 1, None
        for attempt in range(1, attempts + 1):
            t0 = time.perf_counter()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=self._config())
                latency = int((time.perf_counter() - t0) * 1000)
                if not resp.text:
                    raise PerceptionError("the model returned an empty answer", code="model_output_invalid",
                                          attempts=attempt)
                um = resp.usage_metadata
                usage = {"input_tokens": (um.prompt_token_count or 0) if um else 0,
                         "output_tokens": (um.candidates_token_count or 0) if um else 0,
                         "thinking_tokens": (getattr(um, "thoughts_token_count", 0) or 0) if um else 0}
                try:
                    parsed = parse_response(resp.text)
                except PerceptionError as exc:
                    exc.attempts = attempt
                    raise
                return Observed(response=parsed, model=self.model, model_version=resp.model_version or self.model,
                                provider=self.provider, usage=usage, latency_ms=latency, attempts=attempt,
                                cost_usd=cost_usd(usage, self.settings))
            except PerceptionError:
                raise
            except errors.APIError as exc:
                last = exc
                if exc.code not in RETRYABLE or attempt == attempts:
                    break
            except httpx.HTTPError as exc:  # timeouts and connection failures
                last = exc
                break
            time.sleep(2 * attempt)
        raise PerceptionError(f"vision model call failed: {last}", attempts=attempt) from last
