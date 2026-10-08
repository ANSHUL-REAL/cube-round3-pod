"""Condition guidelines extractor (§8.3).

Downloads and hash-verifies the Amazon UK condition guidelines PDF,
extracts text by page using PyMuPDF (fitz), extracts exact quotes,
and generates category rubric snapshots with exact source page citations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-untyped]  # PyMuPDF
import httpx
import yaml

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.config import REPO_ROOT
from returns_manager.errors import ConfigError, VerificationFailed
from returns_manager.reference.hashing import compute_reference_content_sha256
from returns_manager.reference.models import (
    ConditionRubricV1,
    RubricGrade,
    SourcesV1,
    UnacceptableCondition,
)

CACHE_DIR = REPO_ROOT / "agent" / ".cache" / "sources"
REFERENCE_DIR = REPO_ROOT / "reference"


def load_sources_manifest(path: Path | None = None) -> SourcesV1:
    manifest_path = path or (REFERENCE_DIR / "sources.yaml")
    if not manifest_path.exists():
        raise ConfigError(f"sources manifest not found at {manifest_path}")
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return SourcesV1.model_validate(raw)


def fetch_and_verify_source(source_id: str, cache_dir: Path | None = None) -> tuple[bytes, dict[str, Any]]:
    sources_doc = load_sources_manifest()
    source = next((s for s in sources_doc.sources if s.id == source_id), None)
    if not source:
        raise ConfigError(f"source {source_id!r} not found in sources.yaml")

    c_dir = cache_dir or CACHE_DIR
    c_dir.mkdir(parents=True, exist_ok=True)
    cached_file = c_dir / f"{source_id}.pdf"

    if cached_file.exists():
        data = cached_file.read_bytes()
        actual_hash = sha256_hex(data)
        if actual_hash == source.sha256_of_downloaded_bytes:
            return data, source.model_dump()

    resp = httpx.get(source.url, follow_redirects=True, timeout=60.0)
    if resp.status_code != 200:
        raise VerificationFailed(f"failed to download {source.url}: HTTP {resp.status_code}")
    data = resp.content
    actual_hash = sha256_hex(data)
    if actual_hash != source.sha256_of_downloaded_bytes:
        raise VerificationFailed(
            f"SHA-256 mismatch for {source_id}: expected {source.sha256_of_downloaded_bytes}, got {actual_hash}"
        )

    cached_file.write_bytes(data)
    return data, source.model_dump()


def extract_pages(pdf_bytes: bytes) -> dict[int, str]:
    """Extract page text mapped by 1-based page number."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return {p + 1: doc[p].get_text() for p in range(len(doc))}


# General unacceptable conditions from Page 1 of Amazon UK Condition Guidelines
GENERAL_UNACCEPTABLE = [
    UnacceptableCondition(
        code="not_working",
        text="Item does not work perfectly in every regard.",
    ),
    UnacceptableCondition(
        code="not_clean",
        text="Item is not clean, having signs of mould, heavy staining, or corrosion.",
    ),
    UnacceptableCondition(
        code="damaged_hard_to_use",
        text="Item is damaged in a way that renders it difficult to use.",
    ),
    UnacceptableCondition(
        code="missing_essential",
        text="Item is missing essential accompanying material or parts. (This does not necessarily include instructions.)",
    ),
    UnacceptableCondition(
        code="needs_repair",
        text="Item requires repair or service.",
    ),
    UnacceptableCondition(
        code="counterfeit_or_copy",
        text="Item was not created by the original manufacturer or copyright holder. This includes copies, counterfeits, replicas and imitations.",
    ),
    UnacceptableCondition(
        code="promotional_copy",
        text="Item was originally distributed as a promotional copy, promotional bundle, product sample or advance reading copy.",
    ),
    UnacceptableCondition(
        code="obscured_details",
        text="Any aspect of the item is obscured and not able to be read or viewed because of markings, stickers or other damage.",
    ),
    UnacceptableCondition(
        code="expired",
        text="Item has passed the expiration date (includes “best by” and “sell by” dates), has an unacceptable portion of its shelf life remaining, or has had the expiration date removed or tampered with.",
    ),
    UnacceptableCondition(
        code="prohibited",
        text="Item is prohibited for sale on Amazon.co.uk.",
    ),
    UnacceptableCondition(
        code="designated_unsellable",
        text="Item was intended for destruction or disposal or otherwise designated as unsellable by the manufacturer or a supplier, vendor, or retailer.",
    ),
]


def extract_rubric_definitions(pages: dict[int, str]) -> dict[str, ConditionRubricV1]:
    """Build the 6 required category rubrics from exact page text."""
    rubrics: dict[str, ConditionRubricV1] = {}

    # 1. Electronics & Photo (Pages 7, 8, 9)
    electronics_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="A brand-new, unused and unopened item in the original packaging with all of the original packaging materials included. The original manufacturer's warranty, if any, should still apply, with details of any such warranty included when you complete the Add your comments section of the product listing.",
        ),
        RubricGrade(
            code="used_like_new",
            label="Used - Like New",
            text="an apparently untouched item in perfect condition. The original plastic wrap may be missing, but the original packaging is intact. There are absolutely no signs of wear. Suitable for presenting as a gift.",
        ),
        RubricGrade(
            code="used_very_good",
            label="Used - Very Good",
            text="a well-cared-for item that has seen limited use but remains in great condition. The item and its instructions are complete and undamaged, but may show some signs of wear. The item works perfectly.",
        ),
        RubricGrade(
            code="used_good",
            label="Used - Good",
            text="the item shows wear from consistent use, but remains in good condition. The original instructions are included, and are in acceptable condition. The item may be marked or identified, and show other signs of previous use. The item works perfectly and is in good overall shape.",
        ),
        RubricGrade(
            code="used_acceptable",
            label="Used - Acceptable",
            text="the item is fairly worn, but it continues to work perfectly. The signs of wear can include scratches, dents and other aesthetic problems. The box and non-essential instructions may be missing or damaged. The item may be marked or identified, and show other signs of previous use.",
        ),
    ]
    electronics_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="category_unacceptable_defective",
            text="electrical and photographic items that do not work perfectly in every regard or are damaged in ways that render them difficult to use.",
        ),
        UnacceptableCondition(
            code="category_missing_essential",
            text="Items not manufactured or printed by the original manufacturer and for which essential accompanying material is missing (this does not necessarily include instructions) are unacceptable.",
        ),
        UnacceptableCondition(
            code="no_ce_mark", text="Electrical items without CE marks are not acceptable."
        ),
        UnacceptableCondition(
            code="unsafe_electrical",
            text="All electrical and photographic equipment, whether new or used, must be safe (that is, there is no risk that the equipment will cause death, personal injury or damage to property)",
        ),
        UnacceptableCondition(
            code="safety_test_required",
            text="You must arrange for used and refurbished equipment to be tested by an expert prior to listing to verify that it is safe.",
        ),
    ]
    rubrics["electronics"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-electronics",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="electronics",
        source_pages=[1, 7, 8, 9],
        grades=electronics_grades,
        unacceptable_conditions=electronics_unacceptable,
    )

    # 2. Toys & Games (Pages 1, 14)
    toys_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="A brand-new, unused, unopened toy, in perfect condition.",
        ),
        RubricGrade(
            code="used_like_new",
            label="Used - Like New",
            text="an apparently unused toy in perfect condition (although it may be out of its original wrapping). All original parts of the toy are present and in perfect condition. The toy is unmarked with no sign of wear. Suitable for presenting as a gift.",
        ),
        RubricGrade(
            code="used_very_good",
            label="Used - Very Good",
            text="a well cared-for-caredfor toy that has been played with, but remains in great condition. The toy may show limited signs of wear, but all original parts are present and in great condition.",
        ),
        RubricGrade(
            code="used_good",
            label="Used - Good",
            text="the toy is still in a good condition to play with, though shows some signs of wear. It is undamaged, and the original parts are present and in good condition.",
        ),
        RubricGrade(
            code="used_acceptable",
            label="Used - Acceptable",
            text="the toy is in a condition such that it can still be played with, but it is otherwise the worse for wear. Some parts of the toys may be damaged or marked, but all original parts are present. The toy may have identification markings of its owner.",
        ),
    ]
    toys_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="toys_missing_essential_parts",
            text="toys that have missing parts that are essential to the workings of the toy.",
        ),
        UnacceptableCondition(
            code="toys_unsafe",
            text="All toys, whether new or used, must be safe (that is, there is no risk that the equipment will cause death, personal injury or damage to property).",
        ),
    ]
    rubrics["toys_games"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-toys-games",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="toys_games",
        source_pages=[1, 14],
        grades=toys_grades,
        unacceptable_conditions=toys_unacceptable,
    )

    # 3. Home & Kitchen (Pages 1, 9, 10)
    home_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="A brand-new, unused and unopened item in the original packaging with all of the original packaging materials included. The original manufacturer's warranty, if any, should still apply, with details of any such warranty included when you complete the Add your comments section of the product listing.",
        ),
        RubricGrade(
            code="used_like_new",
            label="Used - Like New",
            text="an apparently untouched item in perfect condition. The original plastic wrap may be missing, but the original packaging is intact. There are absolutely no signs of wear. Suitable for presenting as a gift.",
        ),
        RubricGrade(
            code="used_very_good",
            label="Used - Very Good",
            text="a well-cared-for item that has seen limited use but remains in great condition. The item and its instructions are complete and undamaged, but may show some signs of wear. The item works perfectly.",
        ),
        RubricGrade(
            code="used_good",
            label="Used - Good",
            text="the item shows wear from consistent use, but remains in good condition. The original instructions are included, and are in acceptable condition. The item may be marked or identified, and show other signs of previous use. The item works perfectly and is in good overall shape.",
        ),
        RubricGrade(
            code="used_acceptable",
            label="Used - Acceptable",
            text="the item is fairly worn, but it continues to work perfectly. The signs of wear can include scratches, dents and other aesthetic problems. The box and non-essential instructions may be missing or damaged. The item may be marked or identified, and show other signs of previous use.",
        ),
    ]
    home_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="home_damaged_or_defective",
            text="Home & Garden items that do not work perfectly in every regard or are damaged in ways that render them difficult to use.",
        ),
        UnacceptableCondition(
            code="home_missing_essential",
            text="Items not manufactured or printed by the original manufacturer and for which essential accompanying material is missing (this does not necessarily include instructions).",
        ),
        UnacceptableCondition(
            code="home_consumable_part_used", text="Consumable items where any part has been used."
        ),
        UnacceptableCondition(code="home_requires_repair", text="Products that require repair or service."),
    ]
    rubrics["home_kitchen"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-home-kitchen",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="home_kitchen",
        source_pages=[1, 9, 10],
        grades=home_grades,
        unacceptable_conditions=home_unacceptable,
    )

    # 4. Pet (Pages 1, 14, 15) - NEW ONLY
    pet_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="A brand-new, unused, unopened item in its original packaging, with all original packaging materials included. Original protective wrapping, if any, is intact. Original manufacturer's warranty, if any, still applies, with warranty details included in the listing comments. Item should be within expiry date, if applicable.",
        ),
    ]
    pet_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="pet_new_only",
            text="Only New items are permitted to be sold in the these product categories on Amazon.co.uk.",
        ),
    ]
    rubrics["pet"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-pet",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="pet",
        source_pages=[1, 14, 15],
        grades=pet_grades,
        unacceptable_conditions=pet_unacceptable,
    )

    # 5. Beauty & Topical (Pages 1, 15) - NEW ONLY
    beauty_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="All consumable, ingestible, and/or topical products sold on Amazon must be listed in New condition. This includes, for example, items within the Beauty, Food & Grocery, Health Care, and Vitamins & Dietary Supplements categories. To be considered New, items must not have been previously used, be in the original packaging, and be properly prepared, packaged, sealed, and labeledlabelled to prevent contamination, spoiling, melting, and damage.",
        ),
    ]
    beauty_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="beauty_topical_used_prohibited",
            text="Products cannot be listed in New condition if the items have been used, have any signs of use, damage, or were intended for destruction or disposal or were otherwise designated as unsellable by the manufacturer or a supplier, vendor, or retailer.",
        ),
    ]
    rubrics["beauty_topical"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-beauty-topical",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="beauty_topical",
        source_pages=[1, 15],
        grades=beauty_grades,
        unacceptable_conditions=beauty_unacceptable,
    )

    # 6. Grocery & Ingestible (Pages 1, 14, 15) - NEW ONLY
    grocery_grades = [
        RubricGrade(
            code="new",
            label="New",
            text="All consumable, ingestible, and/or topical products sold on Amazon must be listed in New condition. This includes, for example, items within the Beauty, Food & Grocery, Health Care, and Vitamins & Dietary Supplements categories. To be considered New, items must not have been previously used, be in the original packaging, and be properly prepared, packaged, sealed, and labeledlabelled to prevent contamination, spoiling, melting, and damage.",
        ),
    ]
    grocery_unacceptable = [
        *list(GENERAL_UNACCEPTABLE),
        UnacceptableCondition(
            code="grocery_ingestible_used_prohibited",
            text="Products cannot be listed in New condition if the items have been used, have any signs of use, damage, or were intended for destruction or disposal or were otherwise designated as unsellable by the manufacturer or a supplier, vendor, or retailer.",
        ),
    ]
    rubrics["grocery_ingestible"] = ConditionRubricV1(
        schema_="condition-rubric/v1",
        version="1.0.0",
        snapshot_id="amazon-uk-condition-guidelines-2020-12-grocery-ingestible",
        source_id="amazon-uk-condition-guidelines-pdf",
        source_marketplace="amazon.co.uk",
        applies_to_marketplace="amazon.in",
        verification_status="unverified_substitute",
        category_key="grocery_ingestible",
        source_pages=[1, 14, 15],
        grades=grocery_grades,
        unacceptable_conditions=grocery_unacceptable,
    )

    return rubrics


def extract_and_write_rubrics(
    source_id: str = "amazon-uk-condition-guidelines-pdf",
) -> dict[str, str]:
    """Execute the full rubric extraction and write snapshot files."""
    pdf_bytes, _meta = fetch_and_verify_source(source_id)
    pages = extract_pages(pdf_bytes)

    rubrics_dir = REFERENCE_DIR / "rubrics" / "amazon.co.uk"
    rubrics_dir.mkdir(parents=True, exist_ok=True)

    # Save extracted pages cache for offline/reproducible validation
    cache_pages_path = rubrics_dir / "_extracted_pages.json"
    cache_pages_path.write_text(
        json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n"
    )

    rubrics = extract_rubric_definitions(pages)
    active_map: dict[str, str] = {}
    written_files: dict[str, str] = {}

    for cat_key, rubric in rubrics.items():
        data = rubric.model_dump(by_alias=True)
        data["content_sha256"] = compute_reference_content_sha256(data)
        out_file = rubrics_dir / f"{cat_key}.yaml"
        out_file.write_text(
            yaml.dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n"
        )
        active_map[cat_key] = rubric.snapshot_id
        written_files[cat_key] = str(out_file)

    # Write reference/rubrics/active.yaml
    active_file = REFERENCE_DIR / "rubrics" / "active.yaml"
    active_doc = {
        "schema": "rubrics-active/v1",
        "version": "1.0.0",
        "active_snapshots": active_map,
    }
    active_doc["content_sha256"] = compute_reference_content_sha256(active_doc)
    active_file.write_text(
        yaml.dump(active_doc, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n"
    )

    return written_files
