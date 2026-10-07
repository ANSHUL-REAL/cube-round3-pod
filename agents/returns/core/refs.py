"""Load the reference documents one return needs: product card, condition rubric, category policy, parameters.

Written for Round 3, replacing the Round 2 database loaders (`llm/context.py: load_references`,
`reference/loader.py`) with files under agents/returns/reference. What it keeps from Round 2 (section 11.2a): if
anything the judgment needs is missing, the model is not called and the reason is reported.

Every document is hash-checked against its own `content_sha256` before use. A document that fails is treated as
absent, never half-trusted.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from .canonical import sha256_jcs
from .errors import MissingReference
from .models import (
    CategoryPolicyV1,
    ConditionRubricV1,
    ProductCardV1,
    SkuCategoryMapV1,
    SourcesV1,
)
from .paths import REFERENCE_DIR
from .refhash import verify_reference_content_sha256
from .types import EffectivePolicy


def _load_yaml(path: Path) -> dict | None:
    """The parsed document, or None if it does not exist, is not a mapping, or fails its content hash."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict) or not verify_reference_content_sha256(doc):
        return None
    return doc


def _safe_name(value: str) -> bool:
    return bool(value) and not any(c in value for c in "/\\:") and ".." not in value


def load_card(org_id: str, sku: str) -> ProductCardV1 | None:
    """The seller's catalogue card for this SKU under this organisation, or None. Never another org's card."""
    if not (_safe_name(org_id) and _safe_name(sku)):
        return None
    doc = _load_yaml(REFERENCE_DIR / "products" / org_id / f"{sku}.yaml")
    if doc is None:
        return None
    card = ProductCardV1.model_validate(doc)
    return card if (card.org_id, card.sku) == (org_id, sku) else None


def load_other_cards(org_id: str, sku: str) -> dict[str, ProductCardV1]:
    """The same organisation's other cards, so look-alike SKUs and barcodes can be told apart."""
    out: dict[str, ProductCardV1] = {}
    folder = REFERENCE_DIR / "products" / org_id
    for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        if path.stem != sku and (other := load_card(org_id, path.stem)) is not None:
            out[other.sku] = other
    return out


def reference_image_bytes(card: ProductCardV1) -> list[tuple[str, str, str, bytes]]:
    """(ref_image_id, view, sha256, bytes) in card priority order. A file that fails its sha256 is missing."""
    out: list[tuple[str, str, str, bytes]] = []
    base = (REFERENCE_DIR / "products" / card.org_id).resolve()
    for img in card.reference_images:
        path = (base / Path(img.path)).resolve()
        if base not in path.parents:
            raise MissingReference(["no_product_reference"], [f"reference image {img.path} is outside the reference folder"])
        try:
            data = path.read_bytes()
        except OSError:
            raise MissingReference(["no_product_reference"], [f"reference image {img.path} cannot be read"]) from None
        if hashlib.sha256(data).hexdigest() != img.sha256:
            raise MissingReference(["no_product_reference"], [f"reference image {img.path} fails its sha256"])
        out.append((img.id, img.view, img.sha256, data))
    return out


def _active_rubric(category: str) -> ConditionRubricV1 | None:
    active = _load_yaml(REFERENCE_DIR / "rubrics" / "active.yaml")
    snapshot_id = (active or {}).get("active_snapshots", {}).get(category)
    if not snapshot_id:
        return None
    for path in sorted((REFERENCE_DIR / "rubrics").rglob("*.yaml")):
        if path.name == "active.yaml":
            continue
        doc = _load_yaml(path)
        if doc and doc.get("snapshot_id") == snapshot_id:
            return ConditionRubricV1.model_validate(doc)
    return None


def _policy(category: str) -> CategoryPolicyV1 | None:
    for path in sorted((REFERENCE_DIR / "policies").rglob("*.yaml")):
        doc = _load_yaml(path)
        if doc and doc.get("category_key") == category:
            return CategoryPolicyV1.model_validate(doc)
    return None


def load_sources() -> SourcesV1 | None:
    doc = _load_yaml(REFERENCE_DIR / "sources.yaml")
    return SourcesV1.model_validate(doc) if doc else None


def load_references(org_id: str, sku: str):
    """(card, other_cards, rubric, effective_policy, rule_source), or MissingReference naming everything absent.

    The Round 2 gate (section 11.2a) is kept: no card, no parts list or no reference image means no model call.
    `rule_source` says where the condition scale came from (EVIDENCE-CONTRACT section 8: look the rules up and
    record the source), including that the Round 2 author could not verify it for the target marketplace.
    """
    card = load_card(org_id, sku)
    if card is None:
        raise MissingReference(["no_product_reference"],
                               [f"active product card for {sku or 'unknown SKU'} under {org_id}"])
    reasons: list[str] = []
    missing: list[str] = []
    if not card.components:
        reasons.append("no_product_reference")
        missing.append(f"components (parts list) on card {card.sku} v{card.version}")
    if not card.reference_images:
        reasons.append("no_product_reference")
        missing.append(f"reference images on card {card.sku} v{card.version}")
    cat_map_doc = _load_yaml(REFERENCE_DIR / "categories" / "sku-category-map.yaml")
    cat_map = SkuCategoryMapV1.model_validate(cat_map_doc) if cat_map_doc else None
    if cat_map is None or card.sku not in cat_map.mapping:
        reasons.append("no_category_mapping")
        missing.append(f"sku-category-map entry for {card.sku}")
    rubric = _active_rubric(card.category_key)
    if rubric is None:
        reasons.append("no_rubric_for_category")
        missing.append(f"active rubric snapshot for category {card.category_key}")
    policy_doc = _policy(card.category_key)
    if policy_doc is None:
        reasons.append("no_policy_for_category")
        missing.append(f"category policy for {card.category_key}")
    if reasons:
        raise MissingReference(list(dict.fromkeys(reasons)), missing)
    assert rubric is not None and policy_doc is not None
    policy = EffectivePolicy.from_policy(policy_doc, None, None)
    sources = load_sources()
    source = next((s for s in (sources.sources if sources else []) if s.id == rubric.source_id), None)
    rule_source = {
        "snapshot_id": rubric.snapshot_id, "snapshot_sha256": rubric.content_sha256,
        "source_id": rubric.source_id, "title": source.title if source else None,
        "url": source.url if source else None, "retrieved_at": source.retrieved_at if source else None,
        "document_sha256": source.sha256_of_downloaded_bytes if source else None,
        "source_marketplace": rubric.source_marketplace, "applies_to_marketplace": rubric.applies_to_marketplace,
        "verification_status": rubric.verification_status,
        "policy_id": policy.policy_id, "policy_sha256": policy.content_sha256,
    }
    return card, load_other_cards(org_id, card.sku), rubric, policy, rule_source


def card_fingerprint(card: ProductCardV1) -> str:
    """The card's own content hash (checked on load), or a hash of the parsed card."""
    return card.content_sha256 or sha256_jcs(card.model_dump(mode="json", by_alias=True))
