"""Parsing and sampling latency profiles for load testing (§19, §20)."""

from __future__ import annotations

from returns_manager.errors import BadRequest
from returns_manager.load.models import LatencyProfile


def _parse_time_ms(val_str: str) -> float:
    """Parse a time string into milliseconds (e.g. '50ms', '0.05s', '100')."""
    s = val_str.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2])
    if s.endswith("s"):
        return float(s[:-1]) * 1000.0
    return float(s)


def parse_latency_profile(profile_str: str | None) -> LatencyProfile:
    """Parse a CLI latency profile string into a `LatencyProfile`.

    Supported formats:
    - 'instant' or None: 0ms delay
    - 'fixed:<duration>' (e.g. 'fixed:50ms', 'fixed:0.05s')
    - 'uniform:<min>:<max>' (e.g. 'uniform:10ms:50ms', 'uniform:0.01s:0.05s')
    - '<duration>' (e.g. '50ms', '100') -> treated as fixed
    """
    if not profile_str or profile_str.strip().lower() in ("instant", "zero", "0", "0ms", "0s"):
        return LatencyProfile(kind="instant", raw=profile_str or "instant")

    raw = profile_str.strip()
    s = raw.lower()

    if s.startswith("fixed:"):
        part = s[len("fixed:") :]
        try:
            ms = _parse_time_ms(part)
            if ms < 0:
                raise ValueError("duration cannot be negative")
            return LatencyProfile(kind="fixed", fixed_ms=ms, raw=raw)
        except Exception as exc:
            raise BadRequest(f"invalid fixed latency profile '{profile_str}': {exc}") from exc

    if s.startswith("uniform:"):
        parts = s[len("uniform:") :].split(":")
        if len(parts) != 2:
            raise BadRequest(
                f"invalid uniform latency profile '{profile_str}'; expected 'uniform:<min>:<max>'"
            )
        try:
            min_ms = _parse_time_ms(parts[0])
            max_ms = _parse_time_ms(parts[1])
            if min_ms < 0 or max_ms < min_ms:
                raise ValueError("min must be >= 0 and <= max")
            return LatencyProfile(kind="uniform", min_ms=min_ms, max_ms=max_ms, raw=raw)
        except Exception as exc:
            raise BadRequest(f"invalid uniform latency profile '{profile_str}': {exc}") from exc

    # Direct duration string, e.g. "50ms"
    try:
        ms = _parse_time_ms(s)
        if ms < 0:
            raise ValueError("duration cannot be negative")
        return LatencyProfile(kind="fixed", fixed_ms=ms, raw=raw)
    except Exception as exc:
        raise BadRequest(
            f"unknown latency profile format '{profile_str}'. "
            "Use 'instant', 'fixed:<time>', or 'uniform:<min>:<max>'"
        ) from exc
