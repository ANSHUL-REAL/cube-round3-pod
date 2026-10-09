"""Onboard a product for Returns: add a real photo of the item as sold to its product card.

    python -m agents.returns.onboard --org org_demo_alpha --sku SKU-TOWEL-BLU towel_new.jpg [--view contents_layout]

Returns judges a returned parcel only against a verified reference ("no reference, no model call", D-RT07): the card must
list at least one reference photo whose SHA-256 matches the file. The organisers' sample SKUs ship with placeholder cards and
no photos, so out of the box Returns answers `no_product_reference` for every one of them. Onboarding is the step a seller
does once per product: photograph it new, with its parts laid out. This tool stores the photo beside the card, records its
hash, bumps the card's version, notes what was done in `provenance_notes`, and re-seals the card's own `content_sha256`.

It never invents a reference: only a readable photo the caller supplies is added. The rest of the card (title, features,
components) stays whatever it was; a placeholder card is still marked as one.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PIL import Image, ImageOps, UnidentifiedImageError

from shared.utils.console import utf8_console

from .core.models import ProductCardV1
from .core.paths import REFERENCE_DIR
from .core.refhash import compute_reference_content_sha256, verify_reference_content_sha256
from .core.refs import _safe_name, load_card, reference_image_bytes

VIEWS = ("front", "back", "left", "right", "top", "bottom", "label", "packaging", "accessories", "contents_layout")
EXT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
MAX_BYTES = 12 * 1024 * 1024


class OnboardError(ValueError):
    pass


def card_path(org_id: str, sku: str) -> Path:
    if not (_safe_name(org_id) and _safe_name(sku)):
        raise OnboardError("org and sku must be plain names")
    return REFERENCE_DIR / "products" / org_id / f"{sku}.yaml"


REF_MAX_SIDE = 1600


def _normalise(data: bytes) -> bytes:
    """Upright, at most REF_MAX_SIDE px, JPEG. A phone photo is 4-8 MB and every Returns check sends the reference
    photos to the model with the return photos, so storing it as taken made each call slower and could approach the
    request limit (found in a live rehearsal, 2026-10-09). The card records the SHA-256 of these stored bytes."""
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((REF_MAX_SIDE, REF_MAX_SIDE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=90)
    return out.getvalue()


def onboard(org_id: str, sku: str, data: bytes, view: str = "contents_layout", actor: str = "operator") -> dict:
    """Add `data` (a photo) as a reference image of `sku`. Returns {"card": path, "image": id, "added": bool}."""
    if view not in VIEWS:
        raise OnboardError(f"view must be one of {', '.join(VIEWS)}")
    if not data or len(data) > MAX_BYTES:
        raise OnboardError("the photo is empty or larger than 12 MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").lower()
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise OnboardError("not a readable image") from None
    if fmt not in EXT:
        raise OnboardError("only JPG, PNG or WEBP photos")
    data, fmt = _normalise(data), "jpeg"

    path = card_path(org_id, sku)
    if not path.is_file():
        raise OnboardError(f"no product card for {sku} under {org_id}; write the card first")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not verify_reference_content_sha256(doc):
        raise OnboardError(f"{path.name} fails its own content hash; refusing to build on a card that was edited by hand")

    digest = hashlib.sha256(data).hexdigest()
    images = list(doc.get("reference_images") or [])
    for img in images:
        if img.get("sha256") == digest:
            return {"card": str(path), "image": img["id"], "added": False}
    n = 1 + sum(1 for img in images if str(img.get("id", "")).startswith(f"ref_{view}"))
    ref_id = f"ref_{view}_{n}"
    folder = path.parent / sku
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{ref_id}{EXT[fmt]}"
    (folder / name).write_bytes(data)
    images.append({"id": ref_id, "view": view, "path": f"{sku}/{name}", "sha256": digest})

    major, minor, patch = (str(doc.get("version") or "1.0.0").split(".") + ["0", "0"])[:3]
    doc["version"] = f"{major}.{minor}.{int(patch) + 1}"
    doc["reference_images"] = images
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    note = f"Reference photo {ref_id} ({view}) added {when} by {actor} with agents.returns.onboard: a photo of the physical item."
    doc["provenance_notes"] = f"{(doc.get('provenance_notes') or '').strip()} {note}".strip()
    ProductCardV1.model_validate({k: v for k, v in doc.items() if k != "content_sha256"} | {"content_sha256": "0" * 64})
    doc["content_sha256"] = compute_reference_content_sha256(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")

    card = load_card(org_id, sku)  # the way Returns will read it: content hash and every image hash must check out
    if card is None or not any(i[0] == ref_id for i in reference_image_bytes(card)):
        raise OnboardError("the card did not verify after writing; nothing usable was produced")
    return {"card": str(path), "image": ref_id, "added": True}


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(prog="python -m agents.returns.onboard", description=__doc__.split("\n")[0])
    ap.add_argument("photos", nargs="+", help="photos of the product as sold (new, parts laid out)")
    ap.add_argument("--org", required=True)
    ap.add_argument("--sku", required=True)
    ap.add_argument("--view", default="contents_layout", choices=VIEWS)
    ap.add_argument("--actor", default="operator")
    args = ap.parse_args(argv)
    for p in args.photos:
        try:
            r = onboard(args.org, args.sku, Path(p).read_bytes(), view=args.view, actor=args.actor)
        except (OnboardError, OSError) as exc:
            print(f"{p}: refused: {exc}", file=sys.stderr)
            return 2
        print(f"{p}: {'added as' if r['added'] else 'already on the card as'} {r['image']}  ({r['card']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
