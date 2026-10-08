"""`inspect --dry-run` (§11.2): what would be sent, what it would cost, and whether today's quota covers it.

No model call is made. Token counts are ESTIMATES: images use the per-resolution token table of §1.8
(low 280 · medium 560 · high 1,120 · ultra_high 2,240); text uses ~4 characters per token. The cost is the
paid-equivalent (free tier used) of the input only, plus a bound for the maximum output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.llm.context import (
    MissingReference,
    PhotoSource,
    assemble,
    load_photos,
    load_references,
    reference_bytes,
)
from returns_manager.llm.pricing import LABEL, cost_usd_micros
from returns_manager.llm.prompts import get_prompt
from returns_manager.llm.quota import QuotaGuard
from returns_manager.llm.schemas import gemini_response_schema
from returns_manager.llm.tools import tool_definitions

IMAGE_TOKENS = {"low": 280, "medium": 560, "high": 1120, "ultra_high": 2240}


@dataclass(frozen=True)
class DryRun:
    would_call_model: bool
    skip_reasons: list[str]
    missing: list[str]
    summary: dict[str, Any]


def estimate_tokens(items: list[dict[str, Any]], system: str, extra_text: str) -> int:
    tokens = (len(system) + len(extra_text)) // 4
    for item in items:
        if item.get("type") == "image":
            tokens += IMAGE_TOKENS.get(str(item.get("resolution", "high")), 1120)
        else:
            tokens += len(str(item.get("text", ""))) // 4
    return tokens


async def dry_run(
    db: Database, settings: Settings, photos: PhotoSource, org_id: str, return_id: str
) -> DryRun:
    quota = QuotaGuard(db, settings)
    status = await quota.status(settings.rm_judgment_model, "judgment")
    budget = {
        "model": settings.rm_judgment_model,
        "used_today": status.requests_used,
        "daily_budget": status.daily_budget,
        "remaining_today": status.requests_remaining,
        "resets_at_utc": status.next_reset_utc.isoformat(),
        "session_reserves": settings.rm_max_round_trips,
    }
    async with db.transaction(org_id) as conn:
        try:
            ret, card, others, rubric, policy = await load_references(conn, org_id, return_id)
            refs = reference_bytes(card, org_id)
            loaded = await load_photos(conn, photos, return_id)
        except MissingReference as m:
            return DryRun(False, m.reasons, m.missing, {"quota": budget})
    bundle = assemble(
        settings=settings,
        return_row=ret,
        card=card,
        other_cards=others,
        rubric=rubric,
        policy=policy,
        photos=loaded,
        refs=refs,
    )
    extra = json.dumps(tool_definitions()) + json.dumps(gemini_response_schema())
    in_tokens = estimate_tokens(bundle.input_items(), get_prompt("judgment").body, extra)
    in_cost = cost_usd_micros(settings.rm_judgment_model, {"total_input_tokens": in_tokens})
    max_cost = cost_usd_micros(
        settings.rm_judgment_model,
        {"total_input_tokens": in_tokens, "total_output_tokens": settings.rm_max_output_tokens},
    )
    summary = {
        "return_id": return_id,
        "sku": card.sku,
        "card_version": card.version,
        "rubric": bundle.manifest["rubric"],
        "policy": bundle.manifest["policy"],
        "photos": bundle.manifest["photos"],
        "reference_images_sent": bundle.manifest["reference_images_sent"],
        "resolutions": bundle.manifest["resolutions"],
        "sku_block_sha256": bundle.manifest["sku_block_sha256"],
        "estimated_input_tokens": in_tokens,
        "estimate_method": "images by §1.8 resolution table; text ≈ 4 chars/token; excludes thinking",
        "paid_equivalent_usd_input_only": None if in_cost is None else in_cost / 1e6,
        "paid_equivalent_usd_upper_bound": None if max_cost is None else max_cost / 1e6,
        "cost_label": LABEL,
        "quota": budget,
        "quota_covers_session": status.requests_remaining >= settings.rm_max_round_trips,
    }
    return DryRun(True, [], [], summary)
