"""Reference data validation and content hashing (§8.1, §20).

Validates:
1. Pydantic v2 schemas and constraints.
2. content_sha256 matching RFC 8785 JCS without the content_sha256 field.
3. Rubric quotes are exact substrings of extracted source pages.
4. Product card referential integrity (SKU matches filename, org_id matches directory,
   category exists in sku-category-map, referenced images exist and sha256 matches).
5. Active rubric snapshots point to existing rubric files.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.config import REPO_ROOT
from returns_manager.errors import VerificationFailed
from returns_manager.reference.hashing import (
    compute_reference_content_sha256,
    verify_reference_content_sha256,
)
from returns_manager.reference.models import (
    CategoryPolicyV1,
    ConditionRubricV1,
    DispositionParamsV1,
    FxV1,
    ModelPricingV1,
    ProductCardV1,
    QualityGateV1,
    SkuCategoryMapV1,
    SourcesV1,
)

REF_DIR = REPO_ROOT / "reference"


def _clean_text(t: str) -> str:
    # De-hyphenate linebreaks (e.g. non-\nessential -> non-essential)
    t = re.sub(r"-\s*\n\s*", "-", t)
    return " ".join(t.split())


def _is_substring(quote: str, page_text: str) -> bool:
    if quote in page_text:
        return True
    return _clean_text(quote) in _clean_text(page_text)


class ReferenceValidator:
    def __init__(self, ref_dir: Path | None = None) -> None:
        self.ref_dir = ref_dir or REF_DIR
        self.errors: list[str] = []
        self.validated_files: list[Path] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def validate_content_hash(self, path: Path, data: dict[str, Any]) -> None:
        if "content_sha256" not in data or not data["content_sha256"]:
            self.error(f"{path.relative_to(self.ref_dir)}: missing content_sha256")
            return
        if not verify_reference_content_sha256(data):
            expected = compute_reference_content_sha256(data)
            actual = data["content_sha256"]
            self.error(
                f"{path.relative_to(self.ref_dir)}: content_sha256 mismatch "
                f"(actual={actual}, expected={expected})"
            )

    def validate_sources(self) -> SourcesV1 | None:
        p = self.ref_dir / "sources.yaml"
        if not p.exists():
            self.error("sources.yaml missing")
            return None
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            doc = SourcesV1.model_validate(raw)
            self.validate_content_hash(p, raw)
            self.validated_files.append(p)
            return doc
        except Exception as exc:
            self.error(f"sources.yaml validation failed: {exc}")
            return None

    def validate_rubrics(self) -> dict[str, ConditionRubricV1]:
        rubrics: dict[str, ConditionRubricV1] = {}
        rubrics_dir = self.ref_dir / "rubrics" / "amazon.co.uk"
        if not rubrics_dir.exists():
            self.error("rubrics/amazon.co.uk directory missing")
            return rubrics

        pages_cache_file = rubrics_dir / "_extracted_pages.json"
        extracted_pages: dict[int, str] = {}
        if pages_cache_file.exists():
            raw_pages = json.loads(pages_cache_file.read_text(encoding="utf-8"))
            extracted_pages = {int(k): v for k, v in raw_pages.items()}
        else:
            self.error("rubrics/amazon.co.uk/_extracted_pages.json missing; run rubric extract")

        for f in sorted(rubrics_dir.glob("*.yaml")):
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8"))
                doc = ConditionRubricV1.model_validate(raw)
                self.validate_content_hash(f, raw)

                # Verify quotes are exact substrings of source pages
                if extracted_pages:
                    combined_text = " ".join(extracted_pages.get(p, "") for p in doc.source_pages)
                    for g in doc.grades:
                        if not _is_substring(g.text, combined_text):
                            self.error(
                                f"{f.name}: grade {g.code} text is not a "
                                f"substring of pages {doc.source_pages}"
                            )
                    for u in doc.unacceptable_conditions:
                        if not _is_substring(u.text, combined_text):
                            self.error(
                                f"{f.name}: unacceptable {u.code} text is not a "
                                f"substring of pages {doc.source_pages}"
                            )

                rubrics[doc.category_key] = doc
                self.validated_files.append(f)
            except Exception as exc:
                self.error(f"{f.relative_to(self.ref_dir)} validation failed: {exc}")

        # Validate active.yaml
        active_file = self.ref_dir / "rubrics" / "active.yaml"
        if not active_file.exists():
            self.error("rubrics/active.yaml missing")
        else:
            try:
                raw_active = yaml.safe_load(active_file.read_text(encoding="utf-8"))
                self.validate_content_hash(active_file, raw_active)
                active_snaps = raw_active.get("active_snapshots", {})
                for cat, snap_id in active_snaps.items():
                    if cat not in rubrics:
                        self.error(f"active.yaml maps category {cat!r} but no rubric file exists")
                    elif rubrics[cat].snapshot_id != snap_id:
                        self.error(
                            f"active.yaml snapshot {snap_id!r} does not match {rubrics[cat].snapshot_id!r}"
                        )
                self.validated_files.append(active_file)
            except Exception as exc:
                self.error(f"active.yaml validation failed: {exc}")

        return rubrics

    def validate_policies(self) -> dict[str, CategoryPolicyV1]:
        policies: dict[str, CategoryPolicyV1] = {}
        policies_dir = self.ref_dir / "policies" / "amazon.co.uk"
        if not policies_dir.exists():
            self.error("policies/amazon.co.uk directory missing")
            return policies

        for f in sorted(policies_dir.glob("*.yaml")):
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8"))
                doc = CategoryPolicyV1.model_validate(raw)
                self.validate_content_hash(f, raw)
                policies[doc.category_key] = doc
                self.validated_files.append(f)
            except Exception as exc:
                self.error(f"{f.relative_to(self.ref_dir)} validation failed: {exc}")

        return policies

    def validate_sku_category_map(self) -> SkuCategoryMapV1 | None:
        p = self.ref_dir / "categories" / "sku-category-map.yaml"
        if not p.exists():
            self.error("categories/sku-category-map.yaml missing")
            return None
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            doc = SkuCategoryMapV1.model_validate(raw)
            self.validate_content_hash(p, raw)
            self.validated_files.append(p)
            return doc
        except Exception as exc:
            self.error(f"categories/sku-category-map.yaml validation failed: {exc}")
            return None

    def validate_rules_and_quality(self) -> None:
        # disposition-params.yaml
        dp_file = self.ref_dir / "rules" / "disposition-params.yaml"
        if not dp_file.exists():
            self.error("rules/disposition-params.yaml missing")
        else:
            try:
                raw = yaml.safe_load(dp_file.read_text(encoding="utf-8"))
                DispositionParamsV1.model_validate(raw)
                self.validate_content_hash(dp_file, raw)
                self.validated_files.append(dp_file)
            except Exception as exc:
                self.error(f"disposition-params.yaml validation failed: {exc}")

        # quality-gate.yaml
        qg_file = self.ref_dir / "quality" / "quality-gate.yaml"
        if not qg_file.exists():
            self.error("quality/quality-gate.yaml missing")
        else:
            try:
                raw = yaml.safe_load(qg_file.read_text(encoding="utf-8"))
                QualityGateV1.model_validate(raw)
                self.validate_content_hash(qg_file, raw)
                self.validated_files.append(qg_file)
            except Exception as exc:
                self.error(f"quality-gate.yaml validation failed: {exc}")

    def validate_pricing_and_fx(self) -> None:
        # gemini.yaml
        gemini_file = self.ref_dir / "pricing" / "gemini.yaml"
        if not gemini_file.exists():
            self.error("pricing/gemini.yaml missing")
        else:
            try:
                raw = yaml.safe_load(gemini_file.read_text(encoding="utf-8"))
                ModelPricingV1.model_validate(raw)
                self.validate_content_hash(gemini_file, raw)
                self.validated_files.append(gemini_file)
            except Exception as exc:
                self.error(f"gemini.yaml validation failed: {exc}")

        # fx.yaml
        fx_file = self.ref_dir / "pricing" / "fx.yaml"
        if not fx_file.exists():
            self.error("pricing/fx.yaml missing")
        else:
            try:
                raw = yaml.safe_load(fx_file.read_text(encoding="utf-8"))
                FxV1.model_validate(raw)
                self.validate_content_hash(fx_file, raw)
                self.validated_files.append(fx_file)
            except Exception as exc:
                self.error(f"fx.yaml validation failed: {exc}")

    def validate_products(
        self,
        category_map: SkuCategoryMapV1 | None,
        active_rubrics: dict[str, ConditionRubricV1],
    ) -> None:
        products_dir = self.ref_dir / "products"
        if not products_dir.exists():
            self.error("products/ directory missing")
            return

        valid_categories = set(category_map.mapping.keys()) if category_map else set()
        image_owners: dict[str, str] = {}  # sha256 -> "org/sku" that first listed it

        for org_dir in sorted(products_dir.iterdir()):
            if not org_dir.is_dir() or org_dir.name.startswith((".", "_")):
                continue
            org_id = org_dir.name
            for f in sorted(org_dir.glob("*.yaml")):
                sku_from_name = f.stem
                try:
                    raw = yaml.safe_load(f.read_text(encoding="utf-8"))
                    doc = ProductCardV1.model_validate(raw)
                    self.validate_content_hash(f, raw)

                    if doc.org_id != org_id:
                        self.error(f"{f.name}: org_id {doc.org_id!r} does not match directory {org_id!r}")
                    if doc.sku != sku_from_name:
                        self.error(f"{f.name}: sku {doc.sku!r} does not match filename {sku_from_name!r}")
                    if category_map and doc.sku not in valid_categories:
                        self.error(f"{f.name}: SKU {doc.sku!r} is missing from sku-category-map.yaml")
                    if doc.category_key not in active_rubrics:
                        self.error(
                            f"{f.name}: category_key {doc.category_key!r} has no active rubric snapshot"
                        )

                    # Every listed reference image must exist, match its hash, decode completely, and be
                    # this product's own photo (the same bytes under two different SKUs cannot be both).
                    for ref_img in doc.reference_images:
                        img_path = org_dir / ref_img.path
                        if not img_path.is_file():
                            self.error(f"{f.name}: image {ref_img.path} does not exist")
                            continue
                        data = img_path.read_bytes()
                        actual_sha = sha256_hex(data)
                        if actual_sha != ref_img.sha256:
                            self.error(
                                f"{f.name}: image {ref_img.path} sha256 mismatch (actual={actual_sha})"
                            )
                        try:
                            with Image.open(io.BytesIO(data)) as im:
                                im.load()
                        except Exception as exc:
                            self.error(f"{f.name}: image {ref_img.path} does not decode ({exc})")
                        owner = image_owners.setdefault(actual_sha, f"{org_id}/{doc.sku}")
                        if owner.split("/", 1)[1] != doc.sku:
                            self.error(
                                f"{f.name}: image {ref_img.path} is byte-identical to a reference image of "
                                f"{owner}; a reference image must show this product"
                            )

                    self.validated_files.append(f)
                except Exception as exc:
                    self.error(f"{f.relative_to(self.ref_dir)} validation failed: {exc}")

    def run_all(self) -> None:
        self.validate_sources()
        rubrics = self.validate_rubrics()
        self.validate_policies()
        cat_map = self.validate_sku_category_map()
        self.validate_rules_and_quality()
        self.validate_pricing_and_fx()
        self.validate_products(cat_map, rubrics)

        if self.errors:
            msg = f"Reference validation failed ({len(self.errors)} error(s)):\n  " + "\n  ".join(self.errors)
            raise VerificationFailed(msg)


def hash_all_reference_files(ref_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Compute and update content_sha256 in all reference YAML files in-place."""
    r_dir = ref_dir or REF_DIR
    updated: list[tuple[Path, str]] = []

    for path in sorted(r_dir.rglob("*.yaml")):
        if path.name.startswith("."):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "schema" not in raw:
            continue
        new_hash = compute_reference_content_sha256(raw)
        raw["content_sha256"] = new_hash
        path.write_text(yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
        updated.append((path, new_hash))

    return updated
