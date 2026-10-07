"""The organisers' Evidence Contract 1.1: the record shape Recovery Manager reads.

Pack Manager keeps its own, richer record (models.EvidenceRecord: per-SKU checks, the model's
object list, the override chain with prior hashes). `to_contract` writes that record in exactly
the contract's shape. Nothing is renamed or dropped from the contract, and everything that is
ours goes under `checks[].detail`, the one place the contract lets a Manager extend.

Check keys are stable, lowercase with underscores, and every record carries all of them:

    image_quality          the local photo checks (blur, light, glare, size)
    photo_reuse            the same photo wasn't already used for another order
    scene_coverage         the photo shows the whole inside of the box
    all_items_present      every ordered product was seen
    quantities_correct     every ordered product is there in the ordered number
    no_extra_items         nothing outside the order is in the box (wrong or extra products)
    order_matches_manifest the agent's box decision: pass = SEAL, fail = STOP_AND_FIX,
                           uncertain = a person must check (UNCERTAIN or PENDING)

A person's decision on the box is an override of `order_matches_manifest`; a person can also
correct any other check (evidence.apply_check_override), which leaves the box decision as it is.

`content_hash` is computed as the contract defines it: SHA-256 over the image hashes,
concatenated in order, followed by the serialised checks array (JSON, sorted keys, no
whitespace). It identifies the photos and checks a record was written from. It is not
tamper-evident, immutable or anchored: whoever can change a record can recompute the hash.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .models import OVERRIDE_REASONS, Catalogue, Check, Decision, EvidenceRecord, RecordStatus, Verdict

CONTRACT_VERSION = "1.1"
AGENT = "pack"
CHECK_KEYS = [
    "image_quality",
    "photo_reuse",
    "scene_coverage",
    "all_items_present",
    "quantities_correct",
    "no_extra_items",
    "order_matches_manifest",
]
# Our ids are text ("org_demo_alpha"); the contract wants UUIDs. Each maps to the same UUID
# every time, so a record written today and one read next week carry the same organisation.
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/ANSHUL-REAL/cube26-pck-0076-anshul-real")

_VERDICT = {Verdict.PASS: "pass", Verdict.FAIL: "fail", Verdict.UNCERTAIN: "uncertain",
            Verdict.NOT_CHECKED: "uncertain"}
_DECISION_VERDICT = {Decision.SEAL: "pass", Decision.STOP_AND_FIX: "fail",
                     Decision.UNCERTAIN: "uncertain", Decision.PENDING: "uncertain"}
_WORST = {"pass": 0, "uncertain": 1, "fail": 2}


def as_uuid(value: str | None) -> str | None:
    """The value itself if it is a UUID, otherwise a fixed UUID made from it."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(uuid.uuid5(ID_NAMESPACE, str(value)))


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_hash(images: list[dict], checks: list[dict]) -> str:
    """Section 2: SHA-256 over the concatenated image hashes plus the serialised checks array."""
    text = "".join(img["sha256"] for img in images) + canonical(checks)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source(c: Check) -> dict:
    return c.model_dump(mode="json")


def _rollup(key: str, parts: list[Check], summary: str, model_version: str, latency_ms: int,
            not_checked: bool, extra: dict | None = None) -> dict:
    """One contract check from several of ours: fail if any failed, uncertain if any couldn't be
    judged (or none ran), pass only if all passed."""
    verdicts = [_VERDICT[c.verdict] for c in parts]
    verdict = "uncertain" if not_checked or not verdicts else max(verdicts, key=_WORST.get)
    confidence = None
    if verdict == "pass":  # as sure as the least sure part
        known = [c.confidence for c in parts if c.confidence is not None]
        confidence = min(known) if known else None
    elif verdict == "fail":  # as sure as the surest failing part
        known = [c.confidence for c in parts if c.confidence is not None and _VERDICT[c.verdict] == "fail"]
        confidence = max(known) if known else None
    detail = {"summary": summary, "source_checks": [_source(c) for c in parts]}
    if not_checked:
        detail["not_checked"] = "The vision model didn't answer, so this wasn't checked."
    detail.update(extra or {})
    return {"check_key": key, "verdict": verdict, "confidence": confidence, "detail": detail,
            "model_version": model_version, "latency_ms": latency_ms}


def _agent_outcome(record: EvidenceRecord):
    """The agent's own outcome, before any person's decision."""
    first = record.overrides[0] if record.overrides else None
    if first and first.prior_outcome is not None:
        return first.prior_outcome
    if first:
        return record.outcome.model_copy(update={"decision": first.original_decision})
    return record.outcome


def contract_checks(record: EvidenceRecord, catalogue: Catalogue | None = None) -> list[dict]:
    by_key: dict[str, list[Check]] = {}
    for c in record.checks:
        by_key.setdefault(c.check_key.split(":", 1)[0], []).append(c)
    obs = record.observations or {}
    agent_outcome = _agent_outcome(record)
    not_checked = agent_outcome.decision == Decision.PENDING and "vision" in by_key
    model = next((c.model_version for c in record.checks if c.check_key in ("scene_coverage", "vision")
                  and c.model_version), None) or record.agent.get("model_version") or "unknown"
    latency = next((c.latency_ms for c in record.checks if c.latency_ms is not None), None) or 0
    local = next((c.model_version for c in by_key.get("image_quality", []) if c.model_version), None) \
        or "local-quality-gate/1"

    quality = by_key.get("image_quality", [])
    reuse = by_key.get("photo_reuse", [])
    lines = [{"sku": c.check_key.split(":", 1)[1], "check": c.check_key.split(":", 1)[0],
              "verdict": _VERDICT[c.verdict], "text": c.detail}
             for c in record.checks if c.check_key.startswith(("line_present:", "line_quantity:"))]
    images = [{"key": i.image_id, "role": i.role, "original_sha256": i.original_sha256, "mime": i.mime,
               "width": i.width, "height": i.height, "quality": i.quality.model_dump(mode="json")}
              for i in record.images]
    asins = {}
    if catalogue:
        for line in record.subject.get("expected_lines", []):
            item = catalogue.get(line["sku"])
            if item and item.asin:
                asins[line["sku"]] = item.asin

    def per_product(kind: str) -> dict:
        """The verdict for each ordered product, so a charge on one SKU or ASIN (Recovery data
        contract, section 3) can be matched to it even when another line of the order failed.
        A product the agent didn't judge is uncertain, which Recovery treats as no evidence."""
        judged = {c.check_key.split(":", 1)[1]: _VERDICT[c.verdict] for c in by_key.get(kind, [])}
        skus = [line["sku"] for line in record.subject.get("expected_lines", [])]
        by_sku = {sku: "uncertain" if not_checked else judged.get(sku, "uncertain") for sku in skus}
        out = {"by_sku": by_sku}
        if asins:
            out["by_asin"] = {asins[sku]: v for sku, v in by_sku.items() if sku in asins}
        return out

    checks = [
        _rollup("image_quality", quality, quality[0].detail if quality else "No photo checks ran.",
                local, 0, False, {"images": images}),
        _rollup("photo_reuse", reuse, reuse[0].detail if reuse else "Not checked: no earlier photos were looked up.",
                local, 0, False),
        _rollup("scene_coverage", by_key.get("scene_coverage", []),
                by_key["scene_coverage"][0].detail if "scene_coverage" in by_key else "Not checked.",
                model, latency, not_checked, {"scene": obs.get("scene")} if obs.get("scene") else None),
        _rollup("all_items_present", by_key.get("line_present", []),
                f"{len(by_key.get('line_present', []))} ordered product(s) looked for.",
                model, latency, not_checked,
                {**per_product("line_present"),
                 "lines": [x for x in lines if x["check"] == "line_present"], "missing": obs.get("missing", [])}),
        _rollup("quantities_correct", by_key.get("line_quantity", []),
                f"{len(by_key.get('line_quantity', []))} ordered product(s) counted.",
                model, latency, not_checked,
                {**per_product("line_quantity"),
                 "expected_vs_observed": obs.get("expected_vs_observed", []),
                 "over_quantity": obs.get("over_quantity", [])}),
        _rollup("no_extra_items", by_key.get("wrong_item", []) + by_key.get("extra_item", []),
                "Wrong products (a look-alike instead of the ordered one) and products outside the order.",
                model, latency, not_checked, {"wrong": obs.get("wrong", []), "extra": obs.get("extra", [])}),
    ]
    # The box decision. Its parts are the checks above; the rules that combine them are in
    # pack_manager/decision.py.
    verdict = _DECISION_VERDICT[agent_outcome.decision]
    checks.append({
        "check_key": "order_matches_manifest",
        "verdict": verdict,
        "confidence": None,
        "detail": {
            "decision": agent_outcome.decision.value,
            "reasons": agent_outcome.reasons,
            "fix_instructions": agent_outcome.fix_instructions,
            "decided_by": "rules over the model's object list (the model never sees the order)",
            "subject": {k: record.subject.get(k) for k in ("type", "unit_id", "channel", "expected_lines")},
            "asins": asins,
            "detected_items": obs.get("detected_items", []),
            "unclear": obs.get("unclear", []),
            "non_product_items": obs.get("non_product_items", []),
            "uncertainty": obs.get("uncertainty"),
            "agent": record.agent,
            "usage": obs.get("usage"),
            "cost_usd": obs.get("cost_usd"),
            "error": obs.get("error"),
            "retry_of": obs.get("retry_of"),
            # The hash of our record as the agent wrote it, before any override: fixed, so these
            # checks (and the contract hash) don't change when a person decides.
            "internal_record": {"record_id": record.record_id, "schema_version": record.schema_version,
                                "content_hash": record.overrides[0].prior_content_hash if record.overrides
                                else record.content_hash},
        },
        "model_version": model,
        "latency_ms": latency,
    })
    for c in checks:
        c["detail"] = {k: v for k, v in c["detail"].items() if v is not None}
    return checks


def current_verdicts(record: EvidenceRecord, catalogue: Catalogue | None = None) -> dict[str, str]:
    """Each check's verdict now: the agent's, then each person's override in turn. The checks
    in the record never change; this is what they say once the overrides are applied."""
    verdicts = {c["check_key"]: c["verdict"] for c in contract_checks(record, catalogue)}
    for o in record.overrides:
        if o.check_key:
            verdicts[o.check_key] = o.to_verdict
        else:
            verdicts["order_matches_manifest"] = _DECISION_VERDICT[o.new_decision]
    return verdicts


def _override(o) -> dict:
    reason = f"{o.reason_code} ({OVERRIDE_REASONS.get(o.reason_code, o.reason_code)})" + (f": {o.note}" if o.note else "")
    if o.check_key:
        return {"check_key": o.check_key, "from_verdict": o.from_verdict, "to_verdict": o.to_verdict,
                "reason": reason, "by": o.operator_label, "at": o.at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"check_key": "order_matches_manifest", "from_verdict": _DECISION_VERDICT[o.original_decision],
            "to_verdict": _DECISION_VERDICT[o.new_decision], "reason": reason, "by": o.operator_label,
            "at": o.at.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _observed_units(record: EvidenceRecord) -> int | None:
    """Units of product the agent clearly counted in the box, ordered or not. None when it
    couldn't count: the model didn't answer, or some items were unclear or may be hidden."""
    obs = record.observations or {}
    rows = obs.get("expected_vs_observed")
    if rows is None or obs.get("unclear") or any(r.get("unclear") for r in rows):
        return None
    if any(c.check_key.startswith("line_quantity:") and c.verdict == Verdict.UNCERTAIN for c in record.checks):
        return None
    counted = sum(r.get("found", 0) for r in rows)  # includes units over the ordered number
    counted += len(obs.get("extra", [])) + len(obs.get("wrong", []))  # one object each
    return counted


def to_contract(record: EvidenceRecord, image_bytes: dict[str, int] | None = None,
                catalogue: Catalogue | None = None) -> dict:
    """The record in Evidence Contract 1.1 shape. `image_bytes` maps image_id to the stored
    photo's size in bytes (from the images table)."""
    lines = record.subject.get("expected_lines", [])
    single = lines[0]["sku"] if len(lines) == 1 else None
    item = catalogue.get(single) if catalogue and single else None
    images = [{"key": i.image_id, "sha256": i.sha256,
               "bytes": int((image_bytes or {}).get(i.image_id, 0)),
               "taken_at": record.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ")} for i in record.images]
    checks = contract_checks(record, catalogue)
    overrides = [_override(o) for o in record.overrides]
    decided_at = record.outcome.decided_at or record.captured_at
    return {
        "record_id": as_uuid(record.record_id),
        "schema_version": CONTRACT_VERSION,
        "organization_id": as_uuid(record.organization_id),
        "client_id": as_uuid(record.client_id),
        "agent": AGENT,
        "subject": {
            "type": "order",
            "asin": item.asin if item else None,
            "sku": single,
            "order_id": record.subject.get("order_id"),
            "po_line_id": None,
            "shipment_id": record.subject.get("shipment_id"),
            "quantity_expected": sum(int(line["qty"]) for line in lines) if lines else None,
            "quantity_observed": _observed_units(record),
        },
        "captured_at": record.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operator_label": record.operator_label,
        "images": images,
        "checks": checks,
        "outcome": {
            "decision": record.outcome.decision.value,
            "decided_by": "operator" if record.box_overrides else "agent",
            "decided_at": decided_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "overrides": overrides,
        # pending: the model didn't answer and nobody has decided yet. We never write "failed":
        # a capture that can't be read is refused before any record exists.
        "status": "pending" if record.status == RecordStatus.PENDING else "complete",
        "content_hash": contract_hash(images, checks),
    }
