"""Context assembly for the Judgment Agent (§11.2) and the missing-reference gate (§11.2a).

- The SKU block (product card subset, rubric text, reference images) comes first and is byte-identical for
  every return of the same SKU + card version (canonical JSON, fixed order), so Gemini's implicit caching can
  reuse it.
- The unit block (order, deterministic barcode extractions, task instruction, return photos) comes last.
- The model sees aliases only (P1..P3, R1..Rn); the harness maps them back to ids.
- The operator's observation is never sent (blinding, §9.4). No customer data is sent: order id and SKU only.
- If anything the judgment needs is missing, no context is built and the model is not called (§11.2a).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from returns_manager.canonical.hashing import sha256_hex, sha256_jcs
from returns_manager.canonical.jcs import canonical_bytes
from returns_manager.config import REPO_ROOT, Settings
from returns_manager.disposition.params import load_params
from returns_manager.judgment.types import BarcodeDecode, EffectivePolicy, JudgmentContext
from returns_manager.llm.prompts import get_prompt
from returns_manager.llm.schemas import SCHEMA_VERSION, gemini_response_schema
from returns_manager.reference.models import (
    CategoryPolicyV1,
    ConditionRubricV1,
    ProductCardV1,
    SkuCategoryMapV1,
)

REF_DIR = REPO_ROOT / "reference"
PRODUCTS_DIR = REF_DIR / "products"  # committed downscaled reference images live under <org>/images/<SKU>/
MAX_INITIAL_REFERENCES = 2
MAX_INLINE_BYTES = 20 * 1024 * 1024  # Gemini request limit (§1.8)


class PhotoSource(Protocol):
    photos_bucket: str

    async def get(self, bucket: str, key: str) -> bytes: ...


class MissingReference(Exception):
    """§11.2a: the judgment cannot run. `reasons` are the §11.2a codes; `missing` names what is absent."""

    def __init__(self, reasons: list[str], missing: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons
        self.missing = missing


@dataclass(frozen=True)
class ReturnPhoto:
    alias: str
    photo_id: str
    sha256_analysis: str
    analysis_bytes: bytes = field(repr=False)
    original_bytes: bytes = field(repr=False)
    quality_status: str
    barcodes: tuple[BarcodeDecode, ...]
    gate_issues: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceView:
    alias: str
    ref_image_id: str
    view: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ContextBundle:
    ctx: JudgmentContext
    return_id: str
    order_id: str
    photos: tuple[ReturnPhoto, ...]
    references: tuple[ReferenceView, ...]  # every card image, card priority order
    initial_reference_aliases: tuple[str, ...]
    sku_block: list[dict[str, Any]]
    unit_block: list[dict[str, Any]]
    manifest: dict[str, Any]

    def input_items(self) -> list[dict[str, Any]]:
        return [*self.sku_block, *self.unit_block]

    def sku_block_sha256(self) -> str:
        return sha256_jcs(self.sku_block)


def _image_item(data: bytes, resolution: str, mime: str = "image/jpeg") -> dict[str, Any]:
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mime_type": mime,
        "resolution": resolution,
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _jcs_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def card_subset(card: ProductCardV1) -> dict[str, Any]:
    """What the model needs from the card: identifiers, features, siblings, parts list. No value data."""
    return {
        "sku": card.sku,
        "title": card.title,
        "brand": card.brand,
        "category_key": card.category_key,
        "identifiers": {
            "model_numbers": card.identifiers.model_numbers,
            "barcode_values": card.identifiers.barcode_values,
        },
        "distinguishing_features": [f.model_dump() for f in card.distinguishing_features],
        "similar_skus": [{"sku": s.sku, "differs_by": s.differs_by} for s in card.similar_skus],
        "components": [
            {
                "id": c.id,
                "name": c.name,
                "quantity": c.quantity,
                "essential": c.essential,
                "verifiable_by_photo": c.verifiable_by_photo,
                "visual_cues": c.visual_cues,
            }
            for c in card.components
        ],
    }


def rubric_payload(rubric: ConditionRubricV1) -> dict[str, Any]:
    return {
        "snapshot_id": rubric.snapshot_id,
        "verification_status": rubric.verification_status,
        "grades": [{"code": g.code, "label": g.label, "text": g.text} for g in rubric.grades],
        "unacceptable_conditions": [{"code": u.code, "text": u.text} for u in rubric.unacceptable_conditions],
    }


def load_reference_files(category: str) -> tuple[ConditionRubricV1 | None, SkuCategoryMapV1]:
    active = yaml.safe_load((REF_DIR / "rubrics" / "active.yaml").read_text(encoding="utf-8"))
    snapshot_id = active.get("active_snapshots", {}).get(category)
    rubric: ConditionRubricV1 | None = None
    if snapshot_id:
        for path in sorted((REF_DIR / "rubrics").rglob("*.yaml")):
            if path.name == "active.yaml":
                continue
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if doc.get("snapshot_id") == snapshot_id:
                rubric = ConditionRubricV1.model_validate(doc)
                break
    cat_map = SkuCategoryMapV1.model_validate(
        yaml.safe_load((REF_DIR / "categories" / "sku-category-map.yaml").read_text(encoding="utf-8"))
    )
    return rubric, cat_map


async def load_references(
    conn: Any, org_id: str, return_id: str
) -> tuple[dict[str, Any], ProductCardV1, dict[str, ProductCardV1], ConditionRubricV1, EffectivePolicy]:
    """Load everything §11.2a requires, inside the caller's tenant transaction, or raise MissingReference."""
    cur = await conn.execute(
        "SELECT return_id, order_id, unit_id, ordered_sku FROM rm.returns WHERE return_id = %s", (return_id,)
    )
    ret = await cur.fetchone()
    if ret is None:
        raise MissingReference(["order_not_found"], ["return row"])
    reasons: list[str] = []
    missing: list[str] = []
    cur = await conn.execute(
        "SELECT order_id, unit_id, ordered_sku FROM rm.orders WHERE order_id = %s AND unit_id = %s",
        (ret["order_id"], ret["unit_id"]),
    )
    order = await cur.fetchone()
    if order is None:
        reasons.append("order_not_found")
        missing.append(f"order {ret['order_id']} / {ret['unit_id']}")
    sku = ret["ordered_sku"] or (order["ordered_sku"] if order else None)
    card: ProductCardV1 | None = None
    others: dict[str, ProductCardV1] = {}
    if sku:
        cur = await conn.execute("SELECT sku, card FROM rm.products WHERE active ORDER BY sku")
        for row in await cur.fetchall():
            parsed = ProductCardV1.model_validate(row["card"])
            if row["sku"] == sku:
                card = parsed
            else:
                others[row["sku"]] = parsed
    if card is None or not card.components or not card.reference_images:
        reasons.append("no_product_reference")
        if card is None:
            missing.append(f"active product card for {sku or 'unknown SKU'}")
        else:
            if not card.components:
                missing.append(f"components (parts list) on card {card.sku} v{card.version}")
            if not card.reference_images:
                missing.append(f"reference images on card {card.sku} v{card.version}")
    rubric: ConditionRubricV1 | None = None
    policy: EffectivePolicy | None = None
    if card is not None:
        rubric, cat_map = load_reference_files(card.category_key)
        if card.sku not in cat_map.mapping:
            reasons.append("no_category_mapping")
            missing.append(f"sku-category-map entry for {card.sku}")
        if rubric is None:
            reasons.append("no_rubric_for_category")
            missing.append(f"active rubric snapshot for category {card.category_key}")
        cur = await conn.execute(
            "SELECT content FROM rm.category_policies WHERE category_key = %s ORDER BY version DESC LIMIT 1",
            (card.category_key,),
        )
        prow = await cur.fetchone()
        if prow is None:
            reasons.append("no_policy_for_category")
            missing.append(f"category policy for {card.category_key}")
        else:
            cur = await conn.execute("SELECT key, value FROM rm.org_policy_overrides ORDER BY key")
            overrides = {r["key"]: r["value"] for r in await cur.fetchall()}
            policy = EffectivePolicy.from_policy(
                CategoryPolicyV1.model_validate(prow["content"]),
                overrides,
                sha256_jcs(overrides) if overrides else None,
            )
    if reasons:
        raise MissingReference(list(dict.fromkeys(reasons)), missing)
    assert card is not None
    assert rubric is not None
    assert policy is not None
    assert order is not None
    return dict(ret), card, others, rubric, policy


def reference_bytes(card: ProductCardV1, org_id: str) -> list[tuple[str, str, str, bytes]]:
    """(ref_image_id, view, sha256, bytes) in card priority order, from the committed downscaled copies."""
    out: list[tuple[str, str, str, bytes]] = []
    for img in card.reference_images:
        path = PRODUCTS_DIR / org_id / Path(img.path)
        data = path.read_bytes()
        if sha256_hex(data) != img.sha256:
            raise MissingReference(["no_product_reference"], [f"reference image {img.path} fails its sha256"])
        out.append((img.id, img.view, img.sha256, data))
    return out


async def load_photos(conn: Any, source: PhotoSource, return_id: str) -> list[ReturnPhoto]:
    cur = await conn.execute(
        """SELECT photo_id, slot, alias, sha256_analysis, storage_key_original, storage_key_analysis,
                  quality, quality_status
           FROM rm.return_photos WHERE return_id = %s AND superseded = false ORDER BY slot""",
        (return_id,),
    )
    photos = []
    for row in await cur.fetchall():
        quality = row["quality"] or {}
        alias = row["alias"]
        decodes = tuple(
            BarcodeDecode(alias, str(bc.get("text", "")), str(bc.get("format", "")))
            for bc in (quality.get("metrics", {}).get("barcodes") or [])
            if bc.get("text")
        )
        photos.append(
            ReturnPhoto(
                alias=alias,
                photo_id=row["photo_id"],
                sha256_analysis=row["sha256_analysis"],
                analysis_bytes=await source.get(source.photos_bucket, row["storage_key_analysis"]),
                original_bytes=await source.get(source.photos_bucket, row["storage_key_original"]),
                quality_status=row["quality_status"],
                barcodes=decodes,
                gate_issues=tuple(quality.get("issues") or []),
            )
        )
    return photos


def assemble(
    *,
    settings: Settings,
    return_row: dict[str, Any],
    card: ProductCardV1,
    other_cards: dict[str, ProductCardV1],
    rubric: ConditionRubricV1,
    policy: EffectivePolicy,
    photos: list[ReturnPhoto],
    refs: list[tuple[str, str, str, bytes]],
    escalation_focus: list[str] | tuple[str, ...] | None = None,
) -> ContextBundle:
    system = get_prompt("judgment")
    task = get_prompt("judgment_task")
    references = tuple(
        ReferenceView(f"R{i}", rid, view, sha, data) for i, (rid, view, sha, data) in enumerate(refs, start=1)
    )
    initial = references[:MAX_INITIAL_REFERENCES]
    rubric_json = _jcs_text(rubric_payload(rubric))

    sku_block: list[dict[str, Any]] = [
        _text("PRODUCT CARD\n" + _jcs_text(card_subset(card))),
        _text(f"CONDITION RUBRIC {rubric.snapshot_id} ({rubric.verification_status})\n" + rubric_json),
        _text("REFERENCE IMAGES\n" + "\n".join(f"{r.alias} {r.view}" for r in initial)),
        *[_image_item(r.data, settings.rm_reference_photo_resolution) for r in initial],
    ]
    barcode_lines = []
    ordered_codes = {v.upper() for v in card.identifiers.barcode_values}
    for p in photos:
        for bc in p.barcodes:
            mapped = (
                card.sku
                if bc.value.upper() in ordered_codes
                else next(
                    (
                        s
                        for s, c in other_cards.items()
                        if bc.value.upper() in {v.upper() for v in c.identifiers.barcode_values}
                    ),
                    "unknown",
                )
            )
            barcode_lines.append(
                {"photo": p.alias, "value": bc.value, "format": bc.format, "mapped_sku": mapped}
            )
    extractions = {
        "barcodes": barcode_lines,
        "quality_gate": [
            {"photo": p.alias, "status": p.quality_status, "issues": list(p.gate_issues)} for p in photos
        ],
    }
    task_text = task.body
    if settings.rm_output_mode == "json_prompted":
        task_text += "\n\nOUTPUT SCHEMA (return only JSON that validates against it):\n" + json.dumps(
            gemini_response_schema(), separators=(",", ":"), sort_keys=True
        )
    unit_items: list[dict[str, Any]] = [
        _text("ORDER\n" + _jcs_text({"order_id": return_row["order_id"], "ordered_sku": card.sku})),
        _text("DETERMINISTIC EXTRACTIONS\n" + _jcs_text(extractions)),
    ]
    if escalation_focus:
        focus_lines = "\n".join(f"- {f}" for f in escalation_focus)
        msg = (
            "ESCALATION FOCUS\n"
            "Unresolved areas / reason codes to inspect with high precision:\n"
            f"{focus_lines}"
        )
        unit_items.append(_text(msg))
    unit_items.extend(
        [
            _text(task_text),
            _text("RETURN PHOTOS\n" + "\n".join(f"{p.alias} photo_id {p.photo_id}" for p in photos)),
            *[_image_item(p.analysis_bytes, settings.rm_return_photo_resolution) for p in photos],
        ]
    )
    unit_block: list[dict[str, Any]] = unit_items
    size = len(json.dumps([*sku_block, *unit_block]))
    if size > MAX_INLINE_BYTES:
        raise MissingReference(["request_too_large"], [f"inline payload {size} bytes > 20 MB"])

    ctx = JudgmentContext(
        org_id=card.org_id,
        ordered_sku=card.sku,
        card=card,
        other_cards=other_cards,
        rubric=rubric,
        policy=policy,
        params=load_params(),
        photo_aliases=tuple(p.alias for p in photos),
        reference_aliases=tuple(r.alias for r in initial),
        barcodes=tuple(bc for p in photos for bc in p.barcodes),
        # Quotes are checked against the plain rubric sentences the model read (not their JSON escaping).
        rubric_text_sent="\n".join(
            [g.text for g in rubric.grades] + [u.text for u in rubric.unacceptable_conditions]
        ),
    )
    manifest = {
        "prompt": {"id": system.prompt_id, "version": system.version, "sha256": system.sha256},
        "task_prompt": {"id": task.prompt_id, "version": task.version, "sha256": task.sha256},
        "judgment_schema_version": SCHEMA_VERSION,
        "response_schema_sha256": sha256_jcs(gemini_response_schema()),
        "product_card": {"sku": card.sku, "version": card.version, "sha256": card.content_sha256},
        "rubric": {
            "snapshot_id": rubric.snapshot_id,
            "source_marketplace": rubric.source_marketplace,
            "applies_to_marketplace": rubric.applies_to_marketplace,
            "verification_status": rubric.verification_status,
            "sha256": rubric.content_sha256,
        },
        "policy": {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "sha256": policy.content_sha256,
            "org_overrides_sha256": policy.org_overrides_sha256,
        },
        "reference_images_sent": [
            {"alias": r.alias, "id": r.ref_image_id, "sha256": r.sha256} for r in initial
        ],
        "photos": [
            {"alias": p.alias, "photo_id": p.photo_id, "sha256_analysis": p.sha256_analysis} for p in photos
        ],
        "resolutions": {
            "return": settings.rm_return_photo_resolution,
            "reference": settings.rm_reference_photo_resolution,
            "crop": settings.rm_crop_resolution,
        },
        "output_mode": settings.rm_output_mode,
        "sku_block_sha256": sha256_jcs(sku_block),
        "escalation_focus": list(escalation_focus) if escalation_focus else None,
    }
    return ContextBundle(
        ctx=ctx,
        return_id=return_row["return_id"],
        order_id=return_row["order_id"],
        photos=tuple(photos),
        references=references,
        initial_reference_aliases=tuple(r.alias for r in initial),
        sku_block=sku_block,
        unit_block=unit_block,
        manifest=manifest,
    )
