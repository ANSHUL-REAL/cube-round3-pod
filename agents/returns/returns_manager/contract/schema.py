"""JSON Schema generation for the evidence contract (§14.1).

Generates:
  - evidence-record.v1.schema.json  (the fixed contract, JSON Schema draft 2020-12)
  - return-evidence-flat.v1.schema.json (the flat CSV view)
  - flat-columns.v1.csv (column list)

Run via: returns-manager contract build
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from returns_manager.contract.models import EvidenceRecord

# Canonical flat column order (§14.3) — official columns first, then additions.
FLAT_COLUMNS: list[str] = [
    # Official columns (must equal returns_sample.csv header prefix byte-for-byte)
    "record_id",
    "unit_id",
    "org_id",
    "order_id",
    "ordered_sku",
    "ordered_asin",
    "identity_match",
    "parts_list",
    "parts_missing",
    "observed_state",
    "amazon_condition",
    "operator_disposition",
    "photo_refs",
    "operator_id",
    "captured_at",
    # Additions
    "agent_disposition",
    "no_recommendation_reason",
    "provisional",
    "disposition_rule_id",
    "requires_review",
    "review_reasons",
    "relistable_as_is",
    "parts_uncertain",
    "claim_item_not_returned",
    "claim_wrong_item_returned",
    "claim_returned_damaged",
    "record_version",
    "record_status",
    "document_sha256",
    "contract_version",
]

# Fixed check key order (§14.2)
FIXED_CHECK_KEYS: list[str] = [
    "photo_quality",
    "unit_presence",
    "identity",
    "completeness",
    # component:<component_id> entries are inserted here per-SKU
    "condition_grade",
    "relistable_as_is",
    "category_policy",
]

DETERMINISTIC_VERSION = "deterministic"
CONTRACT_VERSION = "1.0.0"


def _patch_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Add JSON Schema draft 2020-12 meta-schema URI."""
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://returns-manager/contract/evidence-record.v1.schema.json"
    schema["title"] = "Evidence Record v1"
    schema["description"] = (
        "The fixed evidence contract from the Official Participant Handbook §9, "
        "as implemented by Returns Manager (track 04). Schema version 1.0.0."
    )
    return schema


def generate_evidence_record_schema() -> dict[str, Any]:
    """Generate JSON Schema draft 2020-12 from the EvidenceRecord Pydantic model."""
    raw = EvidenceRecord.model_json_schema()
    return _patch_schema(raw)


def generate_flat_schema() -> dict[str, Any]:
    """Generate a JSON Schema for the flat view columns."""
    props: dict[str, Any] = {col: {"type": "string"} for col in FLAT_COLUMNS}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://returns-manager/contract/return-evidence-flat.v1.schema.json",
        "title": "Return Evidence Flat View v1",
        "type": "object",
        "required": FLAT_COLUMNS[:15],  # Official columns are required
        "properties": props,
        "additionalProperties": False,
    }


def write_contract_artifacts(output_dir: Path) -> list[Path]:
    """Write all contract artifacts to *output_dir*.

    Returns the list of files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # evidence-record.v1.schema.json
    er_path = output_dir / "evidence-record.v1.schema.json"
    er_path.write_text(json.dumps(generate_evidence_record_schema(), indent=2), encoding="utf-8")
    written.append(er_path)

    # return-evidence-flat.v1.schema.json
    flat_path = output_dir / "return-evidence-flat.v1.schema.json"
    flat_path.write_text(json.dumps(generate_flat_schema(), indent=2), encoding="utf-8")
    written.append(flat_path)

    # flat-columns.v1.csv
    col_path = output_dir / "flat-columns.v1.csv"
    col_path.write_text(",".join(FLAT_COLUMNS) + "\n", encoding="utf-8")
    written.append(col_path)

    return written


def check_no_key_duplication(record: EvidenceRecord) -> list[str]:
    """Assert no key is duplicated between fixed contract and extensions.returns (§14.2).

    Per §14.2 note: record_id and captured_at are allowed to appear in both levels
    so cross-pod consumers can read them from either place without nesting.
    Returns a list of unexpectedly duplicated key names (empty = pass).
    """
    # Allowed overlaps documented in §14.2
    _ALLOWED_OVERLAPS = frozenset({"record_id", "captured_at"})

    top_level_keys = set(EvidenceRecord.model_fields.keys()) - {"extensions"}
    ext_returns = record.extensions.get("returns", {})
    if hasattr(ext_returns, "model_dump"):
        ext_keys = set(ext_returns.model_dump().keys())
    elif isinstance(ext_returns, dict):
        ext_keys = set(ext_returns.keys())
    else:
        ext_keys = set()
    unexpected = (top_level_keys & ext_keys) - _ALLOWED_OVERLAPS
    return sorted(unexpected)
