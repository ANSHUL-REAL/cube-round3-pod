"""Recovery Manager: agent entry point.

========================  REPLACE ME  ========================
ORGANISER STUB. It reads the previous evidence in `request["previous_evidence"]` plus the sample fee report, and
labels each charge CONTRADICTS / SUPPORTS / SILENT. The matching rules below are ILLUSTRATIVE ONLY, not claim
logic. In particular they ignore the open Round 2 findings (docs/decisions.md, F-07 to F-12).
Member 5: bring your Round 2 Recovery Manager here.

Check semantics for Recovery: the condition is "this charge is supported by evidence".
  PASS      evidence supports the charge     -> no claim
  FAIL      evidence contradicts the charge  -> claim
  UNCERTAIN evidence is silent / insufficient -> cannot claim; say why
A wrongly filed claim costs standing with the channel, so SILENT must never become a claim.
Recovery reads the accumulated evidence; it does not rewrite it or the workflow state.
Run:  uvicorn agents.recovery.app:app --port 8105
===============================================================
"""
from shared.utils import sample_data
from shared.utils.records import build_output, build_record, check, utcnow
from shared.utils.server import make_app
from shared.utils.stubs import STUB_MODEL, effective_verdict, previous

STAGE = "recovery"
AGENT_ID = "recovery-stub@0"


def position(line: dict, request: dict) -> tuple[str, str, list[str]]:
    """(CONTRADICTS | SUPPORTS | SILENT, detail, evidence record ids). Uses EFFECTIVE verdicts (overrides applied)."""
    ctype = line["charge_type"]
    if ctype == "inbound_defect_fee":
        prep = previous(request, "prep")
        if not prep or prep["status"] != "completed":
            return "SILENT", "no usable Prep record for this subject", []
        v = effective_verdict(request, prep)
        if v == "PASS":
            return "CONTRADICTS", "Prep evidence shows the unit compliant", [prep["record_id"]]
        if v == "FAIL":
            return "SUPPORTS", "Prep evidence shows a defect", [prep["record_id"]]
        return "SILENT", "Prep evidence is uncertain", [prep["record_id"]]
    if ctype == "refund_issued_item_not_returned":
        ret = previous(request, "returns")
        if ret and ret["status"] == "completed" and ret["checks"] and ret["checks"][0]["verdict"] == "PASS":
            return "CONTRADICTS", "Returns record shows the right item came back", [ret["record_id"]]
        return "SILENT", "no usable Returns record", []
    if ctype == "fulfilment_fee_weight_tier":
        return "SILENT", "no measured weight/dimensions upstream (finding F-07)", []
    if ctype == "lost_inbound":
        return "SILENT", "receiving shortfall is supplier-side, not channel-side loss (finding F-10)", []
    return "SILENT", f"no rule for {ctype}", []


def handle(request: dict) -> dict:
    s = request["subject"]
    if not sample_data.has("receiving", s["subject_id"], s["org_id"]):
        raise LookupError(f"unknown subject {s['subject_id']} in {s['org_id']}")  # tenancy: refuse, don't say "no claim"
    lines = sample_data.fee_lines(s["subject_id"], s["org_id"])
    checks, charges, claimable = [], [], 0.0
    for line in lines:
        pos, why, ids = position(line, request)
        amount = float(line["amount_usd"])
        if pos == "CONTRADICTS" and amount <= 0:
            pos, why = "SILENT", "amount is 0.00: nothing to claim, or the amount is missing (finding F-09)"
        verdict = {"CONTRADICTS": "FAIL", "SUPPORTS": "PASS", "SILENT": "UNCERTAIN"}[pos]
        checks.append(check(f"charge_{line['line_id'].lower().replace('-', '_')}", verdict, None,
                            expected="charge supported by evidence", observed=pos, detail=why,
                            evidence_refs=ids, uncertain_reason="insufficient_evidence"))
        if pos == "CONTRADICTS":
            claimable += amount
        charges.append({"line_id": line["line_id"], "charge_type": line["charge_type"], "amount_usd": amount,
                        "position": pos, "reason": why, "evidence_record_ids": ids})
    claim = any(c["position"] == "CONTRADICTS" for c in charges)
    silent = any(c["position"] == "SILENT" for c in charges)
    verdict = "FAIL" if claim else ("UNCERTAIN" if silent else "PASS")
    outcome = "claim_recommended" if claim else ("insufficient_evidence" if silent else "no_claim")
    record = build_record(
        request, agent_id=AGENT_ID, record_id=f"RCY-{s['subject_id']}", model=STUB_MODEL,
        captured_at=max((l["posted_date"] + "T00:00:00Z" for l in lines), default=utcnow()),
        checks=checks, outcome=outcome, verdict=verdict,
        # SILENT means "cannot claim", not "a human must look": do not flood reviewers.
        needs_human=False,
        reason=f"stub: {len(charges)} charge(s), {sum(c['position'] == 'CONTRADICTS' for c in charges)} contradicted",
        payload={"charges": charges, "claimable_usd": round(claimable, 2),
                 "unclaimable": [c for c in charges if c["position"] != "CONTRADICTS"]},
    )
    return build_output(record, next_step="complete")


app = make_app(STAGE, handle)
