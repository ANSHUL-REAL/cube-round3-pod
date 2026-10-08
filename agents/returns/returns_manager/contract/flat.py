"""Flat view builder (§14.3).

Takes an EvidenceRecord (or the raw DB evidence document) and produces a dict
with exactly the FLAT_COLUMNS keys in the canonical order.  The first 15 columns
match the header of data/returns_sample.csv byte for byte.

`operator_disposition` is the human-confirmed final decision ('pending_review'
until finalized).  `agent_disposition` is the engine recommendation from the
first inspection.  These two are kept distinct as mandated by §14.3.
"""

from __future__ import annotations

from typing import Any

from returns_manager.contract.schema import FLAT_COLUMNS


def _get(doc: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """Traverse nested dict with a dotted path and return the value or default."""
    cur: Any = doc
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur if cur is not None else default


def build_flat_row(document: dict[str, Any]) -> dict[str, str]:
    """Convert an evidence record document to a flat row dict.

    All values are strings (as required by the CSV format).  Empty when absent.
    """
    ext = document.get("extensions", {}).get("returns", {})
    if hasattr(ext, "model_dump"):
        ext = ext.model_dump()
    if not isinstance(ext, dict):
        ext = {}

    disp = ext.get("disposition") or {}
    if hasattr(disp, "model_dump"):
        disp = disp.model_dump()

    claims = ext.get("claim_signals") or {}
    if hasattr(claims, "model_dump"):
        claims = claims.model_dump()

    # photo_refs: ';'-separated rm-photo:<photo_id> references
    photos = ext.get("photos") or []
    photo_refs = ";".join(
        f"rm-photo:{p['photo_id']}" if isinstance(p, dict) and "photo_id" in p else "" for p in photos
    )

    # review_reasons: ';'-separated
    review_reasons_raw = disp.get("review_reasons") or []
    review_reasons = ";".join(review_reasons_raw)

    # returned_damaged.value is tri-state ('yes'/'no'/'uncertain'), not a plain bool;
    # only 'yes' flattens to true - 'no' and 'uncertain' are both truthy strings.
    returned_damaged = claims.get("returned_damaged") or {}
    if isinstance(returned_damaged, dict):
        returned_damaged_str = "true" if returned_damaged.get("value") == "yes" else "false"
    else:
        returned_damaged_str = str(bool(returned_damaged)).lower()

    # observed_state: use model value
    obs_state_block = ext.get("observed_state") or {}
    if hasattr(obs_state_block, "model_dump"):
        obs_state_block = obs_state_block.model_dump()
    observed_state_val = obs_state_block.get("model", "")

    row: dict[str, Any] = {
        # Official columns (must match returns_sample.csv header)
        "record_id": document.get("record_id", ""),
        "unit_id": ext.get("unit_id", ""),
        "org_id": document.get("organization_id", ""),
        "order_id": ext.get("order_id", ""),
        "ordered_sku": ext.get("ordered_sku", ""),
        "ordered_asin": ext.get("ordered_asin", ""),
        "identity_match": "",
        "parts_list": "",
        "parts_missing": "",
        "observed_state": observed_state_val,
        "amazon_condition": "",
        "operator_disposition": disp.get("final_disposition", "pending_review") or "pending_review",
        "photo_refs": photo_refs,
        "operator_id": document.get("operator_label", ""),
        "captured_at": document.get("captured_at", ""),
        # Additions
        "agent_disposition": disp.get("recommended_disposition") or "",
        "no_recommendation_reason": disp.get("no_recommendation_reason") or "",
        "provisional": str(bool(disp.get("provisional", False))).lower(),
        "disposition_rule_id": disp.get("rule_id") or "",
        "requires_review": str(bool(disp.get("requires_review", False))).lower(),
        "review_reasons": review_reasons,
        "relistable_as_is": str(bool(disp.get("relistable_as_is", False))).lower(),
        "parts_uncertain": "",
        "claim_item_not_returned": str(bool(claims.get("item_not_returned", False))).lower(),
        "claim_wrong_item_returned": str(bool(claims.get("wrong_item_returned", False))).lower(),
        "claim_returned_damaged": returned_damaged_str,
        "record_version": str(ext.get("record_version", 1)),
        "record_status": document.get("status", ""),
        "document_sha256": document.get("content_hash", "").removeprefix("sha256:"),
        "contract_version": ext.get("contract_version", "1.0.0"),
    }

    # Populate from identity and completeness blocks
    identity_block = ext.get("identity") or {}
    if hasattr(identity_block, "model_dump"):
        identity_block = identity_block.model_dump()
    row["identity_match"] = identity_block.get("identity_match", "")

    completeness_block = ext.get("completeness") or {}
    if hasattr(completeness_block, "model_dump"):
        completeness_block = completeness_block.model_dump()
    row["parts_list"] = completeness_block.get("parts_list", "")
    row["parts_missing"] = completeness_block.get("parts_missing", "")
    row["parts_uncertain"] = completeness_block.get("parts_uncertain", "")

    condition_block = ext.get("condition") or {}
    if hasattr(condition_block, "model_dump"):
        condition_block = condition_block.model_dump()
    row["amazon_condition"] = condition_block.get("amazon_condition", "")

    # Ensure all columns are strings and in canonical order
    return {col: str(row.get(col, "")) for col in FLAT_COLUMNS}


def flat_rows_to_csv(rows: list[dict[str, str]]) -> str:
    """Render flat rows as CSV (RFC 4180, UTF-8, no BOM)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FLAT_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
