from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import CatalogueItem, Perception
from ..quality import PreparedImage


class PerceptionError(RuntimeError):
    """The vision model could not produce a usable answer (timeout, quota, bad output)."""


class Perceiver(Protocol):
    def perceive(
        self,
        photos: list[PreparedImage],
        candidates: list[CatalogueItem],
        allowed_inserts: list[str],
        catalogue_root: Path | None,
    ) -> Perception: ...
