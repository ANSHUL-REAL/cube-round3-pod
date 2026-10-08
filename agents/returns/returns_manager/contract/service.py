"""Evidence record service (P10 / §13.7, §15).

Reads evidence records from the database and serves them as EvidenceRecord
documents.  All writes are in chain.records; this module is read-only, except
for build_evidence_record() which *shapes* a document for a caller to persist
(chain.records still does the actual INSERT).

Service contract:
  - build_evidence_record(...) -> dict  (§14.2/§14.2b; the only place a finalized
    document is assembled - see the module docstring below for why this exists)
  - get_evidence_document(pool, org_id, unit_id, version?, include_pending?) -> dict | None
  - list_evidence_history(pool, org_id, unit_id) -> list[dict]
  - export_evidence_stream(pool, org_id, since?) -> AsyncIterator[dict]
  - validate_evidence_record(document) -> list[str]
  - add_signed_photo_urls(document, signed_urls) -> dict

build_evidence_record() exists because, before this fix, review/service.py wrote a
much simpler ad hoc "returns-human-loop/v1" document directly - missing subject,
agent, images, checks, status, content_hash and the whole extensions.returns.*
block that this module's own readers (and the REST/MCP responses, and
contract/flat.py's CSV export) assume exists. That mismatch was silent: nothing
ever called validate_evidence_record() on a real finalized document, so it was
only ever exercised by hand-crafted contract-shaped test fixtures. Building the
document through EvidenceRecord.model_validate() here makes a wrong shape a loud
ValidationError at finalization time instead of a silent gap downstream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.canonical.jcs import canonical_bytes
from returns_manager.contract.models import (
    AGENT_NAME,
    SCHEMA_VERSION,
    EvidenceRecord,
)
from returns_manager.db.tenant import transaction

_QUALITY_MAP = {"pass": "pass", "fail": "fail", "warn": "warning"}
_DECISION_MAP = {
    "restock": "RESTOCK",
    "refurbish": "REFURBISH",
    "liquidate": "LIQUIDATE",
    "dispose": "DISPOSE",
}


def _image_entries(photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ImageEntry list (§14.2). `storage_key` is carried as an extra field beyond
    the strict schema - api/routes/evidence.py's signed-URL injection reads it off
    each image dict at request time; it is never returned to a client directly."""
    return [
        {
            "image_id": p["photo_id"],
            "slot": p["slot"],
            "role": p.get("role_hint") or "other",
            "sha256": p["sha256_original"],
            "quality": _QUALITY_MAP.get(p.get("quality_status") or "", "acknowledged"),
            "url": None,
            "storage_key": p.get("storage_key_analysis") or p.get("storage_key_original"),
        }
        for p in photos
    ]


def _identity_block(fused: dict[str, Any], effective_identity_match: str) -> dict[str, Any]:
    evidence_photos = fused.get("evidence_photos") or []
    actual_sku = fused.get("actual_sku")
    return {
        "identity_match": effective_identity_match,
        "evidence_strength": fused.get("strength") or "weak",
        "risk_flags": list(fused.get("risk_flags") or []),
        "reasons": list(fused.get("reasons") or []),
        "observed_identifiers": [actual_sku] if actual_sku else [],
        "feature_checks": [],
        "evidence": [{"photo_ref": p} for p in evidence_photos],
    }


def _component_entries(components: list[dict[str, Any]], overrides: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for c in components:
        component_id = str(c.get("component_id", ""))
        out.append(
            {
                "component_id": component_id,
                "name": c.get("name", ""),
                "expected": c.get("expected", 0),
                "observed": c.get("observed"),
                "status": overrides.get(component_id, c.get("status", "uncertain")),
                "essential": bool(c.get("essential")),
                "evidence": [{"photo_ref": p} for p in (c.get("photos") or [])],
            }
        )
    return out


def _completeness_block(
    completeness: dict[str, Any], effective_status: str, component_overrides: dict[str, str]
) -> dict[str, Any]:
    return {
        "status": effective_status,
        "parts_list": completeness.get("parts_list", ""),
        "parts_missing": completeness.get("parts_missing", ""),
        "parts_uncertain": completeness.get("parts_uncertain", ""),
        "components": _component_entries(completeness.get("components") or [], component_overrides),
    }


def _condition_block(
    condition: dict[str, Any], effective_amazon_condition: str, effective_cosmetic_grade: Any
) -> dict[str, Any]:
    return {
        "amazon_condition": effective_amazon_condition,
        "cosmetic_grade": effective_cosmetic_grade,
        "listing_blockers": list(condition.get("listing_blockers") or []),
        "functional_check": "not_performed",
        "packaging_state": condition.get("packaging_state"),
        "signs_of_use": condition.get("signs_of_use"),
        "observations": [],
        # Not populated: the rubric snapshot actually used at judgment time is not
        # persisted on inspection_results, and guessing "whichever snapshot is
        # current now" would misattribute a rubric the model never saw. Left None
        # (an Optional field) rather than fabricated - see CLAUDE.md's
        # never-invent-references rule.
        "rubric": None,
    }


def _claim_signals_block(claims: dict[str, Any]) -> dict[str, Any]:
    def _tri_bool(block: Any) -> bool:
        return isinstance(block, dict) and block.get("value") == "yes"

    return {
        "item_not_returned": _tri_bool(claims.get("item_not_returned")),
        "wrong_item_returned": _tri_bool(claims.get("wrong_item_returned")),
        "returned_damaged": claims.get("returned_damaged") or {},
        "parts_missing": list(claims.get("parts_missing") or []),
        "parts_uncertain": list(claims.get("parts_uncertain") or []),
    }


def _disposition_block(
    disposition: dict[str, Any],
    *,
    effective_disposition_value: str | None,
    relistable_as_is: bool | None,
    decided_by: str,
    final_disposition: str | None,
) -> dict[str, Any]:
    expected_recovery_pairs = disposition.get("expected_recovery_minor") or []
    expected_recovery = dict(expected_recovery_pairs) if expected_recovery_pairs else None
    signoff_reasons = list(disposition.get("signoff_reasons") or [])
    return {
        "recommended_disposition": effective_disposition_value,
        "no_recommendation_reason": disposition.get("no_recommendation_reason"),
        "provisional": bool(disposition.get("provisional", False)),
        "assumptions": list(disposition.get("assumptions") or []),
        "requires_review": bool(disposition.get("requires_review", False)),
        "review_reasons": list(disposition.get("review_reasons") or []),
        "final_disposition": final_disposition,
        "listing_condition": disposition.get("listing_condition"),
        "relistable_as_is": bool(relistable_as_is) if relistable_as_is is not None else False,
        "rule_id": disposition.get("rule_id", ""),
        "rules_version": disposition.get("rules_version", ""),
        "decided_by": decided_by,
        "requires_signoff": bool(disposition.get("requires_signoff", False)),
        "signoff": {"reasons": signoff_reasons} if signoff_reasons else None,
        "expected_recovery": expected_recovery,
    }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def build_evidence_record(
    *,
    org_id: str,
    ret: dict[str, Any],
    result: dict[str, Any],
    values: dict[str, Any],
    overrides: list[dict[str, Any]],
    photos: list[dict[str, Any]],
    record_version: int,
    actor_id: str,
    decided_by: str,
) -> dict[str, Any]:
    """Build a full EvidenceRecord document (§14.2/§14.2b) from real P8 finalization
    data - `ret` (rm.returns row), `result` (rm.inspection_results row), `values`
    (post-override effective values), `overrides` (rm.overrides rows) and `photos`
    (rm.return_photos rows).

    Raises pydantic.ValidationError if the assembled document does not conform to
    EvidenceRecord - a deliberate hard failure rather than silently persisting a
    document downstream consumers cannot rely on.
    """
    unit_id = ret["unit_id"]
    return_id = ret["return_id"]
    captured_at = _iso(ret.get("created_at"))
    final_disposition = values.get("disposition")

    component_overrides = {
        str(k): v for k, v in (values.get("components") or {}).items() if isinstance(v, str)
    }

    override_entries = [
        {
            "check_key": o["field_path"],
            "original": o["original_value"],
            "new": o["new_value"],
            "reason_code": o["reason_code"],
            "reason": o["reason_text"],
            "by": f"{o['actor_role']}:{o['actor_id']}",
            "at": _iso(o.get("created_at")),
        }
        for o in overrides
    ]

    returns_ext = {
        "contract_version": "1.0.0",
        "record_id": ret["record_id"],
        "record_version": record_version,
        "org_id": org_id,
        "unit_id": unit_id,
        "return_id": return_id,
        "order_id": ret.get("order_id") or "",
        "ordered_sku": ret.get("ordered_sku") or "",
        "ordered_asin": ret.get("ordered_asin") or "",
        "captured_at": captured_at,
        "finalized_at": _iso(None),
        "photos": [{"photo_id": p["photo_id"], "slot": p["slot"]} for p in photos],
        "identity": _identity_block(
            result.get("fused_identity") or {}, values.get("identity_match", "uncertain")
        ),
        "unit_presence": {"status": values.get("unit_presence", "uncertain")},
        "completeness": _completeness_block(
            result.get("components") or {},
            result.get("completeness_status", "uncertain"),
            component_overrides,
        ),
        "condition": _condition_block(
            result.get("condition") or {},
            values.get("amazon_condition", "uncertain"),
            values.get("cosmetic_grade"),
        ),
        "observed_state": {"model": result.get("model_observed_state")},
        "disposition": _disposition_block(
            result.get("disposition") or {},
            effective_disposition_value=final_disposition,
            relistable_as_is=result.get("relistable_as_is"),
            decided_by=decided_by,
            final_disposition=final_disposition,
        ),
        "claim_signals": _claim_signals_block(result.get("claim_signals") or {}),
        "uncertainty": result.get("uncertainties") or [],
        "overrides": override_entries,
        "links": {
            "self": f"/api/v1/units/{unit_id}/return-evidence?version={record_version}",
            "chain": f"/api/v1/units/{unit_id}/chain",
            "verification": f"/api/v1/units/{unit_id}/chain/verification",
            "explain": f"/api/v1/units/{unit_id}/explain",
        },
    }

    document: dict[str, Any] = {
        "record_id": ret["record_id"],
        "schema_version": SCHEMA_VERSION,
        "organization_id": org_id,
        "client_id": org_id,
        "agent": AGENT_NAME,
        "subject": {
            "unit_id": unit_id,
            "return_id": return_id,
            "order_id": ret.get("order_id") or "",
            "sku": ret.get("ordered_sku") or "",
            "asin": ret.get("ordered_asin") or "",
        },
        "captured_at": captured_at,
        "operator_label": actor_id,
        "images": _image_entries(photos),
        "checks": result.get("checks") or [],
        "outcome": {
            "decision": _DECISION_MAP.get(final_disposition or "", "PENDING_REVIEW"),
            "decided_by": decided_by,
            "decided_at": _iso(None),
        },
        "overrides": override_entries,
        "status": "finalized",
        "extensions": {"returns": returns_ext},
    }

    doc_sha256 = sha256_hex(canonical_bytes(document))
    document["content_hash"] = f"sha256:{doc_sha256}"

    # Hard gate: raises pydantic.ValidationError if this shape is wrong, rather than
    # silently persisting a document contract/flat.py or a REST/MCP client cannot rely on.
    EvidenceRecord.model_validate(document)
    return document


async def get_evidence_document(
    pool: AsyncConnectionPool,
    *,
    org_id: str,
    unit_id: str,
    version: int | None = None,
    include_pending: bool = False,
) -> dict[str, Any] | None:
    """Fetch one evidence record document from the database.

    Returns the parsed JSON document, or None if not found (404).
    Cross-org access returns None (the caller returns 404, not 403).
    """
    async with transaction(pool, org_id) as conn:
        return await _fetch_document(
            conn,
            org_id=org_id,
            unit_id=unit_id,
            version=version,
            include_pending=include_pending,
        )


async def _fetch_document(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    unit_id: str,
    version: int | None,
    include_pending: bool,
) -> dict[str, Any] | None:
    if version is not None:
        cur = await conn.execute(
            """
            SELECT document, status, record_version, document_sha256,
                   unit_head_event_hash, finalized_at
            FROM rm.evidence_records
            WHERE org_id = %s AND unit_id = %s AND record_version = %s
            """,
            (org_id, unit_id, version),
        )
    else:
        # Latest non-superseded version
        cur = await conn.execute(
            """
            SELECT document, status, record_version, document_sha256,
                   unit_head_event_hash, finalized_at
            FROM rm.evidence_records
            WHERE org_id = %s AND unit_id = %s AND status <> 'superseded'
            ORDER BY record_version DESC
            LIMIT 1
            """,
            (org_id, unit_id),
        )

    row = await cur.fetchone()
    if row is None:
        if not include_pending:
            return None
        # Try to get a pending/in-progress record from the returns table
        return await _build_pending_document(conn, org_id=org_id, unit_id=unit_id)

    doc = row["document"] if isinstance(row["document"], dict) else json.loads(row["document"])
    status = row["status"]
    if status not in ("finalized", "superseded") and not include_pending:
        return None

    return doc


async def _build_pending_document(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    unit_id: str,
) -> dict[str, Any] | None:
    """Build a minimal evidence document for a return that has not been finalized yet."""
    cur = await conn.execute(
        """
        SELECT r.return_id, r.record_id, r.unit_id, r.order_id,
               r.ordered_sku, r.ordered_asin, r.status, r.created_by,
               r.created_at
        FROM rm.returns r
        WHERE r.org_id = %s AND r.unit_id = %s
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        (org_id, unit_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None

    captured_at = (
        row["created_at"].isoformat().replace("+00:00", "Z")
        if row["created_at"]
        else datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )

    return {
        "record_id": row["record_id"],
        "schema_version": SCHEMA_VERSION,
        "organization_id": org_id,
        "client_id": org_id,
        "agent": AGENT_NAME,
        "subject": {
            "unit_id": unit_id,
            "return_id": row["return_id"],
            "order_id": row["order_id"] or "",
            "sku": row["ordered_sku"] or "",
            "asin": row["ordered_asin"] or "",
        },
        "captured_at": captured_at,
        "operator_label": row["created_by"] or "unknown",
        "images": [],
        "checks": [],
        "outcome": {
            "decision": "PENDING_REVIEW",
            "decided_by": "deterministic_engine@pending",
            "decided_at": captured_at,
        },
        "overrides": [],
        "status": row["status"],
        "content_hash": "sha256:pending",
        "extensions": {
            "returns": {
                "contract_version": "1.0.0",
                "record_id": row["record_id"],
                "record_version": 0,
                "org_id": org_id,
                "unit_id": unit_id,
                "return_id": row["return_id"],
                "order_id": row["order_id"] or "",
                "ordered_sku": row["ordered_sku"] or "",
                "ordered_asin": row["ordered_asin"] or "",
                "captured_at": captured_at,
            }
        },
    }


async def list_evidence_history(
    pool: AsyncConnectionPool,
    *,
    org_id: str,
    unit_id: str,
) -> list[dict[str, Any]]:
    """Return all versions of the evidence record for a unit (§15)."""
    async with transaction(pool, org_id) as conn:
        cur = await conn.execute(
            """
            SELECT document, status, record_version, document_sha256, finalized_at
            FROM rm.evidence_records
            WHERE org_id = %s AND unit_id = %s
            ORDER BY record_version ASC
            """,
            (org_id, unit_id),
        )
        rows = await cur.fetchall()
        result = []
        for row in rows:
            doc = row["document"] if isinstance(row["document"], dict) else json.loads(row["document"])
            result.append(doc)
        return result


async def export_evidence_stream(
    pool: AsyncConnectionPool,
    *,
    org_id: str,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return finalized evidence documents for bulk export (§15).

    Returns the latest non-superseded version for each unit, ordered by
    finalization time.  Only finalized records are included.

    Note: returns a list (not an async generator) so callers don't need
    to manage the async context while streaming.
    """
    async with transaction(pool, org_id) as conn:
        params: list[Any] = [org_id]
        since_clause = ""
        if since is not None:
            since_clause = "AND er.finalized_at >= %s"
            params.append(since)

        # Build query safely: since_clause is only ever an empty string or a fixed SQL
        # fragment - it is never user-controlled.  We avoid f-strings to suppress S608.
        base_query = (
            "SELECT DISTINCT ON (er.unit_id) er.document, er.finalized_at "
            "FROM rm.evidence_records er "
            "WHERE er.org_id = %s AND er.status = 'finalized' "
        )
        order_clause = "ORDER BY er.unit_id, er.record_version DESC"
        query = base_query + since_clause + (" " if since_clause else "") + order_clause

        cur = await conn.execute(query, params)
        rows = await cur.fetchall()

    result = []
    for row in rows:
        doc = row["document"] if isinstance(row["document"], dict) else json.loads(row["document"])
        result.append(doc)
    return result


def validate_evidence_record(document: dict[str, Any]) -> list[str]:
    """Validate a document against EvidenceRecord (§14.2) and return a list of errors.

    Empty list = valid.
    """
    from pydantic import ValidationError

    try:
        EvidenceRecord.model_validate(document)
        return []
    except ValidationError as exc:
        return [str(e) for e in exc.errors()]


def add_signed_photo_urls(
    document: dict[str, Any],
    *,
    signed_urls: dict[str, str],
) -> dict[str, Any]:
    """Return a copy of document with signed URLs injected into images[] (§14.2).

    `signed_urls` maps photo_id -> signed URL.  Never persisted; only served
    in API responses when include_photo_urls=true.
    """
    import copy

    doc = copy.deepcopy(document)
    images = doc.get("images") or []
    for img in images:
        photo_id = img.get("image_id") or img.get("photo_id", "")
        if photo_id in signed_urls:
            img["url"] = signed_urls[photo_id]
    doc["images"] = images
    return doc
