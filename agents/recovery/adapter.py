"""Turn the positions into a Round 3 Evidence Record: one `charge_<line_id>` check per fee line, plus the payload that
lets a person file (or not file) the claims: per-charge position, claims with their evidence and dollar figure, and
the explicit list of what cannot be claimed and why.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal

from shared.utils.hashing import seal
from shared.utils.records import build_record, check, rollup

from . import __version__
from .fees import FeeLine, check_keys
from .policy import CODE_FINDING
from .rules import CENT, Ctx, Position

STAGE = "recovery"
AGENT_ID = f"recovery-manager@{__version__}"
MODEL = {"name": "rules", "version": "recovery-rules/1", "provider": None, "prompt_version": None, "calls": 0,
         "cost_usd": 0}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def record_id_for(request: dict) -> str:
    """Same request in, same record_id out (the contract's idempotency rule)."""
    return "RCY-" + re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def usd(d: Decimal) -> float:
    return float(d.quantize(CENT))


def _refs_for(line: FeeLine, pos: Position) -> list[str]:
    seen: list[str] = []
    for ref in [line.ref, *pos.fee_refs, *pos.refs]:
        if ref not in seen:
            seen.append(ref)
    return seen


def _captured_at(lines: list[FeeLine], ctx: Ctx, fallback: str) -> str:
    dates = [ln.posted_date for ln in lines if _DATE.match(ln.posted_date)]
    if dates:
        return f"{max(dates)}T00:00:00Z"
    stamps = [str(u.record.get("captured_at")) for u in ctx.ev.every() if u.record.get("captured_at")]
    return max(stamps) if stamps else fallback


def decide(lines: list[FeeLine], positions: list[Position]) -> tuple[str, str, bool]:
    """(verdict, outcome, needs_human). Never claims on SILENT; asks a person only where records conflict over money,
    or where a claim lacks the Receiving record it would rest on."""
    claims = [p for p in positions if p.claim > 0]
    conflicts = [p for p, ln in zip(positions, lines) if p.conflict and ln.amount is not None and ln.amount > 0]
    if claims:
        return "FAIL", "claim_recommended", bool(conflicts)
    if conflicts:
        return "UNCERTAIN", "pending_review", True
    if not positions:
        return "UNCERTAIN", "no_claim", False
    if any(p.position == "SILENT" for p in positions):
        return "UNCERTAIN", "insufficient_evidence", False
    return "PASS", "no_claim", False


def build(request: dict, lines: list[FeeLine], positions: list[Position], ctx: Ctx, *, stamp: str,
          latency_ms: int | None) -> dict:
    keys = check_keys(lines)
    checks, charges, claims = [], [], []
    for i, (ln, pos) in enumerate(zip(lines, positions)):
        refs = _refs_for(ln, pos)
        checks.append(check(keys[i], pos.verdict, None, expected="charge supported by evidence", observed=pos.position,
                            detail=pos.detail, evidence_refs=refs, uncertain_reason=pos.uncertain_reason))
        findings = sorted({CODE_FINDING[c] for c in pos.codes if c in CODE_FINDING})
        entry = {"line_id": ln.line_id, "check_key": keys[i], "report_type": ln.report_type, "charge_type": ln.charge_type,
                 "amount_usd": float(ln.amount) if ln.amount is not None else None, "quantity": ln.quantity,
                 "posted_date": ln.posted_date, "position": pos.position, "assessment": pos.assessment,
                 "codes": pos.codes, "findings": findings, "reason": pos.detail, "claim_usd": usd(pos.claim),
                 "evidence_record_ids": list(pos.refs), "fee_line_refs": [ln.ref, *pos.fee_refs], "basis": pos.basis,
                 "underlying_position": pos.underlying, "needs_person": pos.conflict,
                 "what_would_settle_it": pos.settle}
        charges.append(entry)
        if pos.claim > 0:
            kind = "duplicate" if pos.assessment == "DUPLICATE" else "evidence"
            claims.append({"claim_id": f"CLM-{ln.line_id}", "line_id": ln.line_id, "charge_type": ln.charge_type,
                           "claim_usd": usd(pos.claim), "basis_kind": kind, "assessment": pos.assessment,
                           "evidence_record_ids": list(pos.refs), "evidence_refs": refs,
                           "fee_line_refs": [ln.ref, *pos.fee_refs], "rationale": pos.detail})
    claimable = sum((p.claim for p in positions), Decimal("0"))
    verdict, outcome, needs_human = decide(lines, positions)
    if verdict != rollup(checks):  # the roll-up must agree with the checks; never ship a contradiction
        raise RuntimeError(f"decision {verdict} disagrees with the checks' roll-up {rollup(checks)}")
    unclaimable = [{k: c[k] for k in ("line_id", "charge_type", "amount_usd", "position", "assessment", "codes",
                                       "findings", "reason", "evidence_record_ids", "needs_person", "what_would_settle_it")}
                   for c in charges if c["claim_usd"] == 0]
    by_type: dict[str, dict] = {}
    for c in charges:
        t = by_type.setdefault(c["charge_type"], {"lines": 0, "claimable_usd": 0.0, "by_assessment": Counter()})
        t["lines"] += 1
        t["claimable_usd"] = round(t["claimable_usd"] + c["claim_usd"], 2)
        t["by_assessment"][c["assessment"]] += 1
    findings: dict[str, list[str]] = {}
    for c in charges:
        for f in c["findings"]:
            findings.setdefault(f, []).append(c["line_id"])
    n_claims = len(claims)
    reason = (f"{len(charges)} charge(s): {n_claims} contradicted (${usd(claimable):.2f} claimable, evidence attached), "
              f"{sum(p.position == 'SUPPORTS' for p in positions)} supported or settled, "
              f"{sum(p.position == 'SILENT' for p in positions)} silent (never claimed)."
              if charges else "No fee-report lines for this unit: nothing to dispute, and absence of lines is not "
              "evidence that the unit was charged fairly.")
    sources = [dict(t) for t in {tuple(sorted(s.items())) for s in ctx.rule_sources}]
    payload = {
        "charges": charges,
        "claims": claims,
        "claimable_usd": usd(claimable),
        "unclaimable": unclaimable,
        "summary": {"lines": len(charges), "claims": n_claims,
                    "by_position": dict(Counter(c["position"] for c in charges)),
                    "by_assessment": dict(Counter(c["assessment"] for c in charges)),
                    "by_charge_type": {k: {**v, "by_assessment": dict(v["by_assessment"])} for k, v in by_type.items()}},
        "upstream": [u.summary() for u in ctx.ev.every()],
        "overrides_applied": ctx.ev.overrides_applied(),
        "ignored_evidence": ctx.ev.ignored,
        "findings": findings,
        "policy": ctx.policy.snapshot(),
        "rule_source": {"fee_schedule": sources or None,
                        "note": "No channel fee schedule or policy text is encoded in this agent. Weight tiers are judged "
                                "only against a schedule the operator supplies with its source and retrieval date."},
        "fee_report": {"lines_read": len(lines), "unit_id": request["subject"]["subject_id"],
                       "org_id": request["subject"]["org_id"],
                       "source": "organiser sample fee report (dummy values)"},
        "flow_stages": ctx.flow,
        "request_inputs_ignored": [i.get("ref") for i in request.get("inputs") or []],
    }
    common = {k: getattr(lines[0], k) for k in ("sku", "fnsku", "fba_shipment_id")
              if lines and len({getattr(ln, k) for ln in lines}) == 1}
    record = build_record(
        request, agent_id=AGENT_ID, record_id=record_id_for(request), model=MODEL, unit_scope="unit",
        refs=common, captured_at=_captured_at(lines, ctx, stamp), checks=checks, outcome=outcome, verdict=verdict,
        needs_human=needs_human, reason=reason, payload=payload, latency_ms=latency_ms,
        inputs=[{"ref": ln.ref, "sha256": ln.sha256, "kind": "csv_row"} for ln in lines],
        upstream_refs=list(ctx.ev.all_ids))
    # build_record falls back to "every previous record" when the list is empty; Recovery cites only what it consumed,
    # and it pins produced_at so the same request over the same inputs yields the identical, same-hash record.
    return seal({**record, "produced_at": stamp, "upstream_refs": list(ctx.ev.all_ids)})
