"""Barcode detection using zxing-cpp (§9.1, §0.7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import zxingcpp
from PIL import Image


@dataclass(frozen=True)
class DetectedBarcode:
    text: str
    format: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "format": self.format,
        }


def read_barcodes_from_image(image: Image.Image) -> list[DetectedBarcode]:
    """Read barcodes from a PIL Image. Returns detected barcodes with text and format."""
    try:
        results = zxingcpp.read_barcodes(image)
        return [DetectedBarcode(text=str(b.text), format=str(b.format)) for b in results if b.text]
    except Exception:
        return []
