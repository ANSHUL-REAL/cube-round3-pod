"""Context assembly for the one model call per return (Round 2 `llm/context.py`, sections 11.2 and 11.2a).

What is kept from Round 2: the SKU block (product card subset, rubric text, reference images) first, the unit block
(order, barcodes decoded by code, task, return photos) last; the model sees aliases only (P1..P3 for return photos,
R1.. for reference images); the operator's own observation is never sent (blinding); no customer data is sent, only
the order id and SKU. `card_subset` and `rubric_payload` are the Round 2 functions.

Round 3 changes: no database and no photo store (everything comes from files and the request); no tools are offered
(one call, no round trips, so the stage timeout can be kept), which the unit block states in one line; the output
schema is always placed in the task text when the model is not asked for constrained output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_bytes, sha256_jcs
from .models import ConditionRubricV1, ProductCardV1
from .params import load_params
from .prompts import get_prompt
from .schemas import SCHEMA_VERSION, gemini_response_schema
from .types import BarcodeDecode, EffectivePolicy, JudgmentContext

MAX_INITIAL_REFERENCES = 2
MAX_INLINE_BYTES = 20 * 1024 * 1024  # Gemini inline request limit
NO_TOOLS_NOTE = ("NOTE: no tools are available in this session. Do not request any; answer from the images "
                 "and text provided, and mark anything you cannot settle as uncertain.")


@dataclass(frozen=True)
class ReturnPhoto:
    alias: str
    ref: str  # the capture's ref (relative to the capture root), as in the request's inputs[]
    sha256_original: str
    sha256_analysis: str
    analysis_bytes: bytes = field(repr=False)
    quality_status: str = "pass"
    barcodes: tuple[BarcodeDecode, ...] = ()
    gate_issues: tuple[str, ...] = ()
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class ReferenceView:
    alias: str
    ref_image_id: str
    view: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Bundle:
    ctx: JudgmentContext
    order_id: str
    system: str
    parts: tuple[tuple[str, Any], ...]  # ("text", str) or ("image", bytes), in the order sent
    photos: tuple[ReturnPhoto, ...]
    references: tuple[ReferenceView, ...]  # the reference images actually sent
    manifest: dict[str, Any]


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


def assemble(*, order_id: str, card: ProductCardV1, other_cards: dict[str, ProductCardV1], rubric: ConditionRubricV1,
             policy: EffectivePolicy, photos: list[ReturnPhoto], refs: list[tuple[str, str, str, bytes]],
             output_mode: str = "json_prompted") -> Bundle:
    from .errors import MissingReference

    system = get_prompt("judgment")
    task = get_prompt("judgment_task")
    references = tuple(ReferenceView(f"R{i}", rid, view, sha, data) for i, (rid, view, sha, data) in enumerate(refs, start=1))
    initial = references[:MAX_INITIAL_REFERENCES]
    rubric_json = _jcs_text(rubric_payload(rubric))

    sku_texts = [
        "PRODUCT CARD\n" + _jcs_text(card_subset(card)),
        f"CONDITION RUBRIC {rubric.snapshot_id} ({rubric.verification_status})\n" + rubric_json,
        "REFERENCE IMAGES\n" + "\n".join(f"{r.alias} {r.view}" for r in initial),
    ]
    barcode_lines = []
    ordered_codes = {v.upper() for v in card.identifiers.barcode_values}
    for p in photos:
        for bc in p.barcodes:
            mapped = (card.sku if bc.value.upper() in ordered_codes else next(
                (s for s, c in other_cards.items() if bc.value.upper() in {v.upper() for v in c.identifiers.barcode_values}),
                "unknown"))
            barcode_lines.append({"photo": p.alias, "value": bc.value, "format": bc.format, "mapped_sku": mapped})
    extractions = {"barcodes": barcode_lines,
                   "quality_gate": [{"photo": p.alias, "status": p.quality_status, "issues": list(p.gate_issues)} for p in photos]}
    task_text = task.body
    if output_mode == "json_prompted":
        task_text += "\n\nOUTPUT SCHEMA (return only JSON that validates against it):\n" + json.dumps(
            gemini_response_schema(), separators=(",", ":"), sort_keys=True)

    parts: list[tuple[str, Any]] = [("text", t) for t in sku_texts]
    parts += [("image", r.data) for r in initial]
    parts += [
        ("text", "ORDER\n" + _jcs_text({"order_id": order_id, "ordered_sku": card.sku})),
        ("text", "DETERMINISTIC EXTRACTIONS\n" + _jcs_text(extractions)),
        ("text", task_text),
        ("text", NO_TOOLS_NOTE),
        ("text", "RETURN PHOTOS\n" + "\n".join(p.alias for p in photos)),
    ]
    parts += [("image", p.analysis_bytes) for p in photos]
    size = sum(len(x) if isinstance(x, bytes) else len(x.encode("utf-8")) for _, x in parts)
    if size > MAX_INLINE_BYTES:
        raise MissingReference(["request_too_large"], [f"inline payload {size} bytes > 20 MB"])

    ctx = JudgmentContext(
        org_id=card.org_id, ordered_sku=card.sku, card=card, other_cards=other_cards, rubric=rubric, policy=policy,
        params=load_params(), photo_aliases=tuple(p.alias for p in photos),
        reference_aliases=tuple(r.alias for r in initial),
        barcodes=tuple(bc for p in photos for bc in p.barcodes),
        # Quotes are checked against the plain rubric sentences the model read (not their JSON escaping).
        rubric_text_sent="\n".join([g.text for g in rubric.grades] + [u.text for u in rubric.unacceptable_conditions]),
    )
    manifest = {
        "prompt": {"id": system.prompt_id, "version": system.version, "sha256": system.sha256},
        "task_prompt": {"id": task.prompt_id, "version": task.version, "sha256": task.sha256},
        "judgment_schema_version": SCHEMA_VERSION,
        "response_schema_sha256": sha256_jcs(gemini_response_schema()),
        "product_card": {"sku": card.sku, "version": card.version, "sha256": card.content_sha256},
        "reference_images_sent": [{"alias": r.alias, "id": r.ref_image_id, "sha256": r.sha256} for r in initial],
        "output_mode": output_mode, "tools_offered": False,
    }
    return Bundle(ctx=ctx, order_id=order_id, system=system.body, parts=tuple(parts), photos=tuple(photos),
                  references=initial, manifest=manifest)
