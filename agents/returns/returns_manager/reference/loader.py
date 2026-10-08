"""Reference data loader (§7.2, §20).

Loads validated reference files from disk into the database schema `rm`:
- rubric_snapshots (global, no RLS)
- category_policies (global, no RLS)
- products, product_components, reference_images (tenant-scoped, under db.tenant.transaction)
- orders (tenant-scoped, under db.tenant.transaction)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

from returns_manager.config import REPO_ROOT
from returns_manager.db.pool import Database
from returns_manager.reference.models import (
    CategoryPolicyV1,
    ConditionRubricV1,
    ProductCardV1,
)

REF_DIR = REPO_ROOT / "reference"


async def load_rubrics(db: Database, ref_dir: Path | None = None) -> int:
    r_dir = (ref_dir or REF_DIR) / "rubrics" / "amazon.co.uk"
    if not r_dir.exists():
        return 0

    count = 0
    async with db.transaction(None) as conn:
        for f in sorted(r_dir.glob("*.yaml")):
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            rubric = ConditionRubricV1.model_validate(raw)
            content_json = json.dumps(rubric.model_dump(by_alias=True))

            query = """
            INSERT INTO rm.rubric_snapshots (
                snapshot_id, source_marketplace, applies_to_marketplace,
                category_key, verification_status, source_id,
                content_sha256, content
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (snapshot_id) DO UPDATE SET
                content_sha256 = EXCLUDED.content_sha256,
                content = EXCLUDED.content,
                loaded_at = now()
            """
            await conn.execute(
                query,
                (
                    rubric.snapshot_id,
                    rubric.source_marketplace,
                    rubric.applies_to_marketplace,
                    rubric.category_key,
                    rubric.verification_status,
                    rubric.source_id,
                    rubric.content_sha256 or "",
                    content_json,
                ),
            )
            count += 1
    return count


async def load_policies(db: Database, ref_dir: Path | None = None) -> int:
    p_dir = (ref_dir or REF_DIR) / "policies" / "amazon.co.uk"
    if not p_dir.exists():
        return 0

    count = 0
    async with db.transaction(None) as conn:
        for f in sorted(p_dir.glob("*.yaml")):
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            policy = CategoryPolicyV1.model_validate(raw)
            content_json = json.dumps(policy.model_dump(by_alias=True))

            query = """
            INSERT INTO rm.category_policies (
                policy_id, version, source_marketplace,
                category_key, content_sha256, content
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (policy_id, version) DO UPDATE SET
                content_sha256 = EXCLUDED.content_sha256,
                content = EXCLUDED.content,
                loaded_at = now()
            """
            await conn.execute(
                query,
                (
                    policy.policy_id,
                    policy.version,
                    policy.source_marketplace,
                    policy.category_key,
                    policy.content_sha256 or "",
                    content_json,
                ),
            )
            count += 1
    return count


async def load_products(db: Database, ref_dir: Path | None = None) -> dict[str, int]:
    prod_dir = (ref_dir or REF_DIR) / "products"
    if not prod_dir.exists():
        return {}

    counts: dict[str, int] = {}
    for org_dir in sorted(prod_dir.iterdir()):
        if not org_dir.is_dir() or org_dir.name.startswith((".", "_")):
            continue
        org_id = org_dir.name
        org_count = 0

        async with db.transaction(org_id) as conn:
            for f in sorted(org_dir.glob("*.yaml")):
                raw = yaml.safe_load(f.read_text(encoding="utf-8"))
                card = ProductCardV1.model_validate(raw)
                card_json = json.dumps(card.model_dump(by_alias=True))

                # Insert product
                prod_query = """
                INSERT INTO rm.products (
                    org_id, sku, card_version, asin, fnsku, gtin,
                    title, brand, category_key, card_sha256, card, active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, true)
                ON CONFLICT (org_id, sku, card_version) DO UPDATE SET
                    title = EXCLUDED.title,
                    brand = EXCLUDED.brand,
                    category_key = EXCLUDED.category_key,
                    card_sha256 = EXCLUDED.card_sha256,
                    card = EXCLUDED.card,
                    active = true
                """
                await conn.execute(
                    prod_query,
                    (
                        org_id,
                        card.sku,
                        card.version,
                        card.identifiers.asin,
                        card.identifiers.fnsku,
                        card.identifiers.gtin,
                        card.title,
                        card.brand,
                        card.category_key,
                        card.content_sha256 or "",
                        card_json,
                    ),
                )

                # Upsert components
                for comp in card.components:
                    comp_source = json.dumps(comp.source.model_dump())
                    comp_query = """
                    INSERT INTO rm.product_components (
                        org_id, sku, card_version, component_id, name,
                        quantity, essential, replaceable, verifiable_by_photo,
                        visual_cues, source
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (org_id, sku, card_version, component_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        quantity = EXCLUDED.quantity,
                        essential = EXCLUDED.essential,
                        replaceable = EXCLUDED.replaceable,
                        verifiable_by_photo = EXCLUDED.verifiable_by_photo,
                        visual_cues = EXCLUDED.visual_cues,
                        source = EXCLUDED.source
                    """
                    await conn.execute(
                        comp_query,
                        (
                            org_id,
                            card.sku,
                            card.version,
                            comp.id,
                            comp.name,
                            comp.quantity,
                            comp.essential,
                            comp.replaceable,
                            comp.verifiable_by_photo,
                            comp.visual_cues,
                            comp_source,
                        ),
                    )

                # Upsert reference images
                for img in card.reference_images:
                    img_query = """
                    INSERT INTO rm.reference_images (
                        org_id, sku, card_version, ref_image_id, view,
                        storage_key, sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, sku, card_version, ref_image_id) DO UPDATE SET
                        view = EXCLUDED.view,
                        storage_key = EXCLUDED.storage_key,
                        sha256 = EXCLUDED.sha256
                    """
                    await conn.execute(
                        img_query,
                        (
                            org_id,
                            card.sku,
                            card.version,
                            img.id,
                            img.view,
                            img.path,
                            img.sha256,
                        ),
                    )

                # Exactly one active card version per SKU: older versions stay for traceability, inactive.
                await conn.execute(
                    """
                    UPDATE rm.products SET active = false
                    WHERE org_id = %s AND sku = %s AND card_version <> %s AND active
                    """,
                    (org_id, card.sku, card.version),
                )

                org_count += 1
        counts[org_id] = org_count

    return counts


async def load_orders(db: Database, ref_dir: Path | None = None) -> int:
    orders_file = (ref_dir or REF_DIR) / "orders" / "orders-seed.csv"
    if not orders_file.exists():
        return 0

    rows_by_org: dict[str, list[dict[str, Any]]] = {}
    with orders_file.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            org = r["org_id"]
            rows_by_org.setdefault(org, []).append(r)

    total_inserted = 0
    for org_id, rows in rows_by_org.items():
        async with db.transaction(org_id) as conn:
            for row in rows:
                query = """
                INSERT INTO rm.orders (
                    org_id, order_id, unit_id, ordered_sku,
                    ordered_asin, quantity, fulfilment_route, ordered_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                ON CONFLICT (org_id, order_id, unit_id) DO NOTHING
                """
                await conn.execute(
                    query,
                    (
                        org_id,
                        row["order_id"],
                        row["unit_id"],
                        row["ordered_sku"],
                        row.get("ordered_asin"),
                        int(row.get("quantity") or 1),
                        row.get("fulfilment_route") or "fba",
                        row["ordered_at"],
                    ),
                )
                total_inserted += 1

    return total_inserted


async def load_all(db: Database, ref_dir: Path | None = None) -> dict[str, Any]:
    rubric_count = await load_rubrics(db, ref_dir)
    policy_count = await load_policies(db, ref_dir)
    product_counts = await load_products(db, ref_dir)
    order_count = await load_orders(db, ref_dir)

    return {
        "rubric_snapshots": rubric_count,
        "category_policies": policy_count,
        "products_by_org": product_counts,
        "orders": order_count,
    }
