"""One rule for where a capture may live, used by every agent that reads photos.

A capture ref is relative to the capture root and must be `<subject_id>/<stage>/<file>`. The check is made on the
RESOLVED path, so `UNIT-1/pack/../../UNIT-2/pack/x.jpg` (which starts with the right words but ends up in another unit's
folder) is refused, as are absolute paths, drive letters and links that point elsewhere.
"""
from __future__ import annotations

from pathlib import Path


class CapturePathError(ValueError):
    """The ref does not point inside <root>/<subject_id>/<stage>/."""


def resolve_capture(root: Path, ref: str, subject_id: str, stage: str) -> Path:
    if not isinstance(ref, str) or not ref or not isinstance(subject_id, str) or not subject_id:
        raise CapturePathError(f"capture {ref!r} is not a usable reference")
    if "\\" in ref or ref.startswith("/") or ":" in ref or any(p in ("", ".", "..") for p in ref.split("/")):
        raise CapturePathError(f"capture {ref!r} must be a plain relative path (no '..', no absolute path, no drive)")
    allowed = (Path(root).resolve() / subject_id / stage).resolve()
    path = (Path(root).resolve() / ref).resolve()
    if allowed not in path.parents:
        raise CapturePathError(f"capture {ref!r} is not under {subject_id}/{stage}/")
    return path
