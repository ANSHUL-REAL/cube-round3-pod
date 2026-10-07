"""Gemini implementation of the perceiver: one call per box, with a response cache.

The cache is keyed by everything the model is sent: model, thinking budget, system
instruction and every prompt part (candidate texts and reference photos, allowed inserts,
box photos, task). Re-running the eval or reloading a record never spends quota twice on
the same input, and any change to the input makes a fresh call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from ..catalogue import reference_images
from ..config import Settings
from ..models import CatalogueItem, Perception
from ..quality import PreparedImage
from .base import PerceptionError
from .prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, VisionResponse, build_parts, normalise

RETRYABLE = {429, 500, 502, 503, 504}
log = logging.getLogger(__name__)


class GeminiPerceiver:
    def __init__(self, settings: Settings, use_cache: bool = True):
        if not settings.gemini_api_key:
            raise PerceptionError("GEMINI_API_KEY is not set.")
        self.settings = settings
        self.model = settings.gemini_model
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=int(settings.gemini_timeout_s * 1000)),
        )
        self.cache_dir = Path(settings.cache_dir) if use_cache else None
        # Eval ablation only: when set, the prompt also states the order.
        self.order_hint: dict[str, int] | None = None

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION + ("+order" if self.order_hint else "")

    def _config(self) -> types.GenerateContentConfig:
        kwargs = dict(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=VisionResponse,
            temperature=0.0,
        )
        if self.settings.gemini_thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.settings.gemini_thinking_budget
            )
        return types.GenerateContentConfig(**kwargs)

    def _cache_key(self, parts: list[str | bytes], candidates: list[CatalogueItem]) -> str:
        """Hash of everything the model is sent and the settings that change its answer."""
        h = hashlib.sha256()
        h.update(f"{self.model}|{self.prompt_version}|{self.settings.gemini_thinking_budget}".encode())
        h.update(hashlib.sha256(SYSTEM_INSTRUCTION.encode()).digest())
        h.update(hashlib.sha256(json.dumps(VisionResponse.model_json_schema(), sort_keys=True).encode()).digest())
        for item in candidates:
            h.update(hashlib.sha256(item.model_dump_json().encode()).digest())
        # Every prompt part in order: candidate texts and reference photos, allowed inserts,
        # the order (eval ablation only), box photos and the task text. One digest per part.
        for part in parts:
            h.update(hashlib.sha256(part if isinstance(part, bytes) else part.encode()).digest())
        return h.hexdigest()

    @staticmethod
    def _read_cache(path: Path) -> tuple[dict, VisionResponse] | None:
        """The saved answer, or None. A file that can't be read or doesn't validate (e.g. cut
        short by a crash) is deleted, so the box gets a fresh call instead of failing forever."""
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if not (isinstance(entry.get("usage"), dict) and isinstance(entry.get("model_version"), str)
                    and isinstance(entry.get("latency_ms"), int)):
                raise ValueError("missing or wrong fields")
            return entry, VisionResponse.model_validate(json.loads(entry["text"]))
        except Exception as exc:  # any unreadable file means: ask the model again
            log.warning("Discarding unreadable cache file %s: %s", path.name, exc)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    @staticmethod
    def _write_cache(path: Path, entry: dict) -> None:
        """Written to a temporary file and renamed, so a crash never leaves half a file.
        Best effort: if the cache can't be written, the answer is still used."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem[:16]}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(entry, f)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("Could not save the model answer to the cache: %s", exc)

    def _call(self, contents: list) -> tuple[str, dict, str, int]:
        attempts = self.settings.gemini_max_retries + 1
        last: Exception | None = None
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=contents, config=self._config()
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                if not resp.text:
                    raise PerceptionError("Model returned an empty response.")
                um = resp.usage_metadata
                usage = {
                    "input_tokens": (um.prompt_token_count or 0) if um else 0,
                    "output_tokens": (um.candidates_token_count or 0) if um else 0,
                    "thinking_tokens": (um.thoughts_token_count or 0) if um else 0,
                    "total_tokens": (um.total_token_count or 0) if um else 0,
                }
                return resp.text, usage, resp.model_version or self.model, latency_ms
            except errors.APIError as exc:
                last = exc
                if exc.code not in RETRYABLE or attempt == attempts - 1:
                    break
            except httpx.HTTPError as exc:  # timeouts and connection failures
                last = exc
                break
            time.sleep(2 * (attempt + 1))
        raise PerceptionError(f"Vision model call failed: {last}") from last

    def perceive(
        self,
        photos: list[PreparedImage],
        candidates: list[CatalogueItem],
        allowed_inserts: list[str],
        catalogue_root: Path | None,
    ) -> Perception:
        refs = [
            (item, reference_images(item, catalogue_root, self.settings) if catalogue_root else [])
            for item in candidates
        ]
        parts = build_parts([p.jpeg for p in photos], refs, allowed_inserts, self.order_hint)
        cache_file = self.cache_dir / f"{self._cache_key(parts, candidates)}.json" if self.cache_dir else None

        hit = self._read_cache(cache_file) if cache_file else None
        cached = hit is not None
        if hit:
            entry, resp = hit
        else:
            contents = [
                types.Part.from_bytes(data=p, mime_type="image/jpeg") if isinstance(p, bytes) else p
                for p in parts
            ]
            text, usage, model_version, latency_ms = self._call(contents)
            entry = {"text": text, "usage": usage, "model_version": model_version, "latency_ms": latency_ms}
            try:
                resp = VisionResponse.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise PerceptionError(f"Model output did not match the schema: {exc}") from exc
            if cache_file:
                self._write_cache(cache_file, entry)

        skus = [c.sku for c in candidates]
        objects, counts, scene, issues = normalise(resp, skus, len(photos))
        return Perception(
            objects=objects,
            counts=counts,
            scene=scene,
            image_issues=issues,
            candidates=skus,
            model_version=entry["model_version"],
            prompt_version=self.prompt_version,
            latency_ms=entry["latency_ms"],
            usage=entry["usage"],
            cached=cached,
        )


def list_models(settings: Settings) -> list[str]:
    client = genai.Client(api_key=settings.gemini_api_key)
    names = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" in actions:
            names.append(m.name.removeprefix("models/"))
    return sorted(names)
