"""Structured JSON logging with secret redaction (§18.1, T-SEC-09).

`configure_logging()` wires BOTH `structlog` and the stdlib `logging` module through the
same processor chain, so a call site written as `structlog.get_logger(__name__).info(...)`
and one written as `logging.getLogger(__name__).info(...)` (several already exist in
`jobs/`, `cli/`, `mcp_server.py`) are rendered identically: one JSON object per line, with
the redaction processor applied to both.

Never logged, in any field or inside free text (redacted if present): API keys, JWTs,
signed URLs, Supabase keys, raw image bytes, prompts, thinking content. §18.1 also lists
the event fields a log line may carry: `ts, level, event, request_id, org_id, unit_id,
return_id, job_id, inspection_id, model, latency_ms, tokens_in, tokens_out, cached_tokens,
quota_used_today, error_class`. A caller binds whichever of these it has via
`logger.bind(...)` or keyword arguments to the log call; absent fields are omitted, never
padded with null.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_REDACTED = "***REDACTED***"

# Fields whose value is redacted outright, regardless of type or content (§18.1: "raw image
# bytes, prompts, thinking content"). Compared case-insensitively against the event-dict key.
_DENY_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "system_prompt",
        "thinking",
        "thought",
        "thoughts",
        "reasoning",
        "image_bytes",
        "image_data",
        "raw_image",
        "photo_bytes",
        "signed_url",
    }
)

# Patterns scrubbed out of any string value (including inside a free-text `event` message),
# because a secret can leak through string interpolation, not just through a named field.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Our own API keys: rmk_<env>_<random>.
    (re.compile(r"\brmk_(?:local|test|demo)_[A-Za-z0-9]{8,}\b"), _REDACTED),
    # JSON Web Tokens (three base64url segments separated by dots).
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), _REDACTED),
    # Supabase v2 API keys (sb_secret_..., sb_publishable_...) and legacy service_role JWT label.
    (re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]{8,}\b"), _REDACTED),
    # A signed URL: any http(s) URL carrying a token/signature query parameter. Keep the path,
    # drop the query string, so the log still says *what* was signed without leaking *how*.
    (
        re.compile(
            r"(https?://[^\s\"'?]+)\?[^\s\"']*\b(?:token|signature|sig|X-Amz-Signature)=[^\s\"']*",
            re.IGNORECASE,
        ),
        r"\1?" + _REDACTED,
    ),
    # A raw Postgres/Supabase connection string with an embedded password.
    (re.compile(r"(postgres(?:ql)?://[^:\s]+):[^@\s]+@"), r"\1:" + _REDACTED + "@"),
)


def _redact_string(value: str) -> str:
    for pattern, repl in _PATTERNS:
        value = pattern.sub(repl, value)
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and k.lower() in _DENY_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact_processor(logger: object, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """A structlog processor: redacts denylisted keys and secret-shaped strings everywhere."""
    for key, value in list(event_dict.items()):
        if isinstance(key, str) and key.lower() in _DENY_KEYS:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_configured = False


def configure_logging(level: str = "info", *, stream: Any = None) -> None:
    """Configure structlog + stdlib logging to emit redacted JSON lines.

    Idempotent: safe to call from every entry point (API app startup, worker, CLI, MCP
    server) without double-installing handlers. `stream` defaults to stdout; tests pass an
    in-memory buffer to capture and assert on the emitted lines.
    """
    global _configured
    target = stream if stream is not None else sys.stdout

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(target)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_LEVEL_MAP.get(level.lower(), logging.INFO))
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """A bound structlog logger. Callers pass known §18.1 fields as keyword arguments."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
