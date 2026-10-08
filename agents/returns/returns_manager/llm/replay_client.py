"""Replay cassettes (§8.12): deterministic tests and development without spending free-tier quota.

A cassette is JSONL, one line per model request, in call order:
`{request_fingerprint, model, request_summary, response, recorded_at, sdk_version}`.

`request_fingerprint = SHA-256(JCS(request body with every inline image replaced by the SHA-256 of its data
and the server-issued previous_interaction_id removed))`. A replayed request whose fingerprint differs fails
loudly: the prompt, schema, context or tools changed, so the recording no longer answers this request.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from returns_manager.canonical.hashing import sha256_jcs
from returns_manager.errors import VerificationFailed
from returns_manager.llm.client import ModelClient, ModelRequest, ModelResponse, response_from_raw


class CassetteMismatch(VerificationFailed):
    pass


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _scrub(v) for k, v in node.items()}
        if out.get("type") == "image" and isinstance(node.get("data"), str):
            out["data"] = "sha256:" + hashlib.sha256(node["data"].encode("ascii")).hexdigest()
        return out
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    return node


def request_fingerprint(request: ModelRequest) -> str:
    body = copy.deepcopy(request.body())
    body.pop("previous_interaction_id", None)
    return sha256_jcs(_scrub(body))


def request_summary(request: ModelRequest) -> dict[str, Any]:
    kinds = [item.get("type") for item in request.input]
    images = sum(1 for k in kinds if k == "image")
    return {
        "model": request.model,
        "input_items": len(kinds),
        "images": images,
        "tools": sorted(t.get("name", "") for t in request.tools),
        "continuation": request.previous_interaction_id is not None,
    }


def _sdk_version() -> str:
    try:
        from google.genai import version

        return str(version.__version__)
    except Exception:
        return "unknown"


class ReplayModelClient:
    """Serves a cassette in order. Consumes no quota and makes no network call."""

    def __init__(self, cassette: Path) -> None:
        self.path = cassette
        self._lines = [json.loads(line) for line in cassette.read_text(encoding="utf-8").splitlines() if line]
        self._next = 0
        self.requests_sent = 0

    async def create(self, request: ModelRequest) -> ModelResponse:
        if self._next >= len(self._lines):
            raise CassetteMismatch(
                f"{self.path.name}: request #{self._next + 1} is not in the cassette; re-record it"
            )
        entry = self._lines[self._next]
        fp = request_fingerprint(request)
        if entry["request_fingerprint"] != fp:
            raise CassetteMismatch(
                f"{self.path.name} request #{self._next + 1}: request changed: re-record the cassette and "
                "bump the prompt/schema version"
            )
        self._next += 1
        self.requests_sent += 1
        return response_from_raw(entry["response"], latency_ms=int(entry.get("latency_ms", 0)))


class RecordingModelClient:
    """Wraps a live client and appends every request/response to a cassette."""

    def __init__(self, inner: ModelClient, cassette: Path) -> None:
        self.inner = inner
        self.path = cassette
        cassette.parent.mkdir(parents=True, exist_ok=True)

    async def create(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.create(request)
        line = {
            "request_fingerprint": request_fingerprint(request),
            "model": request.model,
            "request_summary": request_summary(request),
            "response": response.raw,
            "latency_ms": response.latency_ms,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sdk_version": _sdk_version(),
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        return response
