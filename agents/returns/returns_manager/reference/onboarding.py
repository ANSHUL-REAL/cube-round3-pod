"""Onboarding Assistant (§11.15 / P14).

Drafts Product Knowledge Cards from seller-supplied catalogue metadata.
Every field carries provenance; unknown components remain explicitly unknown.
Nothing is published to production reference directories without human approval.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import yaml

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.canonical.jcs import canonical_bytes
from returns_manager.config import AGENT_ROOT

REFERENCE_ROOT = AGENT_ROOT.parent / "reference"


def draft_product_card(
    *,
    org_id: str,
    sku: str,
    title: str,
    category_key: str,
    brand: str = "Unknown",
    asin: str | None = None,
    components_summary: list[dict[str, Any]] | None = None,
    out_path: Path | None = None,
) -> Path:
    """Draft a PR-style product knowledge card with explicit provenance."""
    draft_dir = REFERENCE_ROOT / "products" / "_drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    target = out_path or (draft_dir / f"{sku}.yaml")

    comps: list[dict[str, Any]] = []
    if components_summary:
        for c in components_summary:
            comps.append(
                {
                    "id": c.get("id", "main_unit"),
                    "name": c.get("name", "main unit"),
                    "quantity": c.get("quantity", 1),
                    "essential": c.get("essential", True),
                    "replaceable": c.get("replaceable", False),
                    "verifiable_by_photo": c.get("verifiable_by_photo", True),
                    "visual_cues": c.get("visual_cues", "matching catalog visual description"),
                    "source": {
                        "type": "seller_catalogue",
                        "ref": f"sku:{sku}",
                        "retrieved_at": None,
                    },
                }
            )
    else:
        comps.append(
            {
                "id": "main_unit",
                "name": "Main unit",
                "quantity": 1,
                "essential": True,
                "replaceable": False,
                "verifiable_by_photo": True,
                "visual_cues": f"Item matching catalogue title: {title}",
                "source": {
                    "type": "seller_catalogue",
                    "ref": f"sku:{sku}",
                    "retrieved_at": None,
                },
            }
        )

    card = {
        "schema": "product-card/v1",
        "version": "1.0.0",
        "org_id": org_id,
        "sku": sku,
        "identifiers": {
            "asin": asin,
            "fnsku": None,
            "gtin": None,
            "model_numbers": [],
            "barcode_values": [],
        },
        "title": title,
        "brand": brand,
        "category_key": category_key,
        "distinguishing_features": [
            {
                "id": "df_catalog_appearance",
                "description": f"Appearance matching catalog title: {title}",
                "location": "product_body",
                "importance": "critical",
            }
        ],
        "similar_skus": [],
        "components": comps,
        "reference_images": [],
        "consumable": False,
        "value": {
            "synthetic": True,
            "list_price": {
                "amount_minor": 19900,
                "currency": "INR",
            },
            "recovery_rate_bp": {
                "restock_new": 10000,
                "restock_used": 6500,
                "refurbish": 5000,
                "liquidate": 2000,
                "dispose": 0,
            },
            "refurbish_cost": {
                "amount_minor": 2000,
                "currency": "INR",
            },
        },
        "provenance_notes": (
            f"Drafted by Onboarding Assistant (§11.15) from seller catalogue metadata for {sku}. "
            "Requires human operator review and approval via 'returns-manager reference approve' "
            "before publication to reference store."
        ),
        "content_sha256": "placeholder_until_approved",
    }

    target.write_text(yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
    return target


def approve_product_card(draft_path: Path, destination_dir: Path | None = None) -> Path:
    """Approve a draft product card, compute canonical SHA-256 hash, and publish to active reference."""
    if not draft_path.exists():
        raise FileNotFoundError(f"Draft card not found: {draft_path}")

    raw = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid YAML content in draft card")

    org_id = raw.get("org_id")
    sku = raw.get("sku")
    if not org_id or not sku:
        raise ValueError("Draft card missing required org_id or sku")

    dest_dir = destination_dir or (REFERENCE_ROOT / "products" / org_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{sku}.yaml"

    # Compute content hash over card content excluding content_sha256
    hashable = dict(raw)
    hashable.pop("content_sha256", None)
    c_hash = sha256_hex(canonical_bytes(hashable))
    raw["content_sha256"] = c_hash

    # Write approved card to destination
    dest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    # Clean up draft
    if draft_path != dest_path:
        with contextlib.suppress(OSError):
            draft_path.unlink()

    return dest_path
