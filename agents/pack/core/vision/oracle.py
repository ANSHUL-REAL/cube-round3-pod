"""A perceiver that reports known contents with full confidence.

Used to test the decision engine separately from vision accuracy, for example by
replaying the organisers' sample CSV, where `observed_in_box` is given.
"""

from __future__ import annotations

from pathlib import Path

from ..models import CatalogueItem, DetectedObject, Perception, Scene, SkuCount
from ..quality import PreparedImage

ORACLE_VERSION = "oracle/1"


class OraclePerceiver:
    def __init__(self, observed: dict[str, int], unknown_products: list[str] | None = None):
        self.observed = observed
        self.unknown_products = unknown_products or []

    def perceive(
        self,
        photos: list[PreparedImage],
        candidates: list[CatalogueItem],
        allowed_inserts: list[str],
        catalogue_root: Path | None,
    ) -> Perception:
        objects = []
        for sku, qty in self.observed.items():
            for _ in range(qty):
                objects.append(
                    DetectedObject(
                        object_id=f"o{len(objects) + 1}",
                        classification="CANDIDATE",
                        sku=sku,
                        confidence=1.0,
                        description=sku,
                    )
                )
        for desc in self.unknown_products:
            objects.append(
                DetectedObject(
                    object_id=f"o{len(objects) + 1}",
                    classification="UNKNOWN_PRODUCT",
                    confidence=1.0,
                    description=desc,
                )
            )
        return Perception(
            objects=objects,
            counts=[SkuCount(sku=s, count=q, count_certain=True) for s, q in self.observed.items()],
            scene=Scene(box_interior_fully_visible=True, items_may_be_hidden=False, visibility_confidence=1.0),
            candidates=[c.sku for c in candidates],
            model_version=ORACLE_VERSION,
            prompt_version="none",
            latency_ms=0,
        )
