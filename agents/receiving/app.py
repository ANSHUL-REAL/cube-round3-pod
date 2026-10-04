"""Receiving Manager: agent entry point.

========================  REPLACE ME  ========================
This file currently contains an ORGANISER STUB that replays the Round 2 sample CSV.
Member 1: bring your Round 2 Receiving Manager here and make `handle()` call it.
Keep the contract: take an Agent Input, return an Agent Output (EVIDENCE-CONTRACT.md).
Run:  uvicorn agents.receiving.app:app --port 8101
===============================================================
"""
from shared.utils import sample_data
from shared.utils.records import build_output, build_record, check
from shared.utils.server import make_app
from shared.utils.stubs import STUB_MODEL, photos, verdict_from

STAGE = "receiving"
AGENT_ID = "receiving-stub@0"
DAMAGE_OK, DAMAGE_BAD = {"none"}, {"crushing", "water", "tears"}


def handle(request: dict) -> dict:
    s = request["subject"]
    r = sample_data.row("receiving", s["subject_id"], s["org_id"])  # LookupError -> 404 (tenancy)
    refs = [p["ref"] for p in photos(r)]
    flags = [f for f in r["quality_flags"].split(";") if f]
    qo, qr = int(r["qty_ordered"]), int(r["qty_received"])
    co, cr = int(r["cartons_ordered"]), int(r["cartons_received"])

    checks = [
        check("identity_match", verdict_from(r["identity_match"], {"yes"}, {"no"}), None,
              expected=f"{r['sku']} ({r['product_title']})", observed=r["identity_match"],
              evidence_refs=refs, uncertain_reason="poor_image"),
        check("carton_count", "PASS" if co == cr else "FAIL", None, expected=co, observed=cr, evidence_refs=refs),
        check("quantity", "PASS" if qo == qr else "FAIL", None, expected=qo, observed=qr, evidence_refs=refs),
        check("carton_damage", verdict_from(r["carton_damage"], DAMAGE_OK, DAMAGE_BAD), None,
              expected="none", observed=r["carton_damage"], evidence_refs=refs, uncertain_reason="poor_image"),
        check("unit_damage", verdict_from(r["unit_damage"], DAMAGE_OK, DAMAGE_BAD), None,
              expected="none", observed=r["unit_damage"], evidence_refs=refs, uncertain_reason="poor_image"),
        check("quality_flags", "FAIL" if flags else "PASS", None, expected=[], observed=flags, evidence_refs=refs),
    ]
    verdict = "FAIL" if any(c["verdict"] == "FAIL" for c in checks) else (
        "UNCERTAIN" if any(c["verdict"] == "UNCERTAIN" for c in checks) else "PASS")
    outcome = {"PASS": "accept", "FAIL": "accept_with_exceptions", "UNCERTAIN": "pending_review"}[verdict]
    record = build_record(
        request, agent_id=AGENT_ID, record_id=r["record_id"], captured_at=r["captured_at"], operator_id=r["operator_id"],
        unit_scope="po_line", refs={"po_number": r["po_number"], "po_line": r["po_line"], "sku": r["sku"], "asin": r["asin"]},
        checks=checks, outcome=outcome, model=STUB_MODEL, inputs=photos(r),
        reason=f"stub replay of sample row; {sum(c['verdict'] == 'FAIL' for c in checks)} failed check(s)",
        payload={"supplier": r["supplier"], "qty_ordered": qo, "qty_received": qr, "shortfall_units": max(qo - qr, 0),
                 "quality_flags": flags},
    )
    return build_output(record)


app = make_app(STAGE, handle)
