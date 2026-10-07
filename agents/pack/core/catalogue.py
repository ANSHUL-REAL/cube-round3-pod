"""Catalogue loading and choosing which candidate products the model sees for a box."""

from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .models import Catalogue, CatalogueItem, Order
from .quality import ImageDecodeError, prepare_reference

log = logging.getLogger(__name__)


def load_catalogue(folder: str | Path) -> Catalogue:
    folder = Path(folder)
    data = json.loads((folder / "catalogue.json").read_text(encoding="utf-8"))
    return Catalogue.model_validate(data)


def load_org_catalogue(catalogue_dir: str | Path, org_id: str) -> tuple[Catalogue, Path]:
    """The organisers' sample SKUs plus the organisation's own catalogue, if it has one.

    Returns the catalogue and the folder its reference-image paths are relative to.
    """
    root = Path(catalogue_dir)
    sample = load_catalogue(root / "sample")
    folder = root / org_id
    if not (folder / "catalogue.json").exists():
        return sample.model_copy(update={"organization_id": org_id}), root / "sample"
    own = load_catalogue(folder)
    items = {i.sku: i for i in sample.items} | {i.sku: i for i in own.items}
    return Catalogue(organization_id=org_id, items=list(items.values()), allowed_inserts=own.allowed_inserts), folder


def select_candidates(order: Order, catalogue: Catalogue, settings: Settings) -> list[CatalogueItem]:
    """Expected SKUs, their look-alikes, and a few decoys, in a shuffled order.

    The model is never told which candidates were ordered or how many. Look-alikes and
    decoys force it to compare against references instead of confirming what it expects.
    """
    expected = list(order.expected())
    chosen: list[str] = [s for s in expected if catalogue.get(s)]

    related: list[str] = []
    for sku in expected:
        item = catalogue.get(sku)
        if item:
            related += item.confusable_with + item.component_lookalikes
    for sku in related:
        if sku not in chosen and catalogue.get(sku) and len(chosen) < settings.max_candidates:
            chosen.append(sku)

    rng = random.Random(order.order_id)
    rest = sorted(s for s in catalogue.skus if s not in chosen)
    # Prefer decoys that have reference photos (real products) over text-only sample SKUs.
    with_photos = [s for s in rest if catalogue.get(s).reference_images]
    rest = with_photos if len(with_photos) >= settings.decoys_per_box else rest
    room = max(0, settings.max_candidates - len(chosen))
    chosen += rng.sample(rest, min(settings.decoys_per_box, len(rest), room))

    rng.shuffle(chosen)
    return [catalogue.get(s) for s in chosen]


@lru_cache(maxsize=512)
def _reference_bytes(path: str, max_side: int) -> bytes:
    return prepare_reference(Path(path).read_bytes(), max_side)


def reference_images(item: CatalogueItem, root: str | Path, settings: Settings) -> list[bytes]:
    out = []
    for rel in item.reference_images[: settings.ref_images_per_sku]:
        path = Path(root) / rel
        if path.exists():
            try:
                out.append(_reference_bytes(str(path), settings.ref_max_side_px))
            except (ImageDecodeError, OSError):
                # One unreadable reference photo mustn't make every box that shows this sku
                # (even as a decoy) fail; the model still gets the text description.
                log.warning("Skipping unreadable reference photo %s", path)
    return out
