"""Prep Manager: agent entry point.

========================  REPLACE ME  ========================
ORGANISER STUB replaying the Round 2 sample CSV.
Member 2: bring your Round 2 Prep Manager here and make `handle()` call it.
Recovery has asked Prep for measured weight and dimensions (payload.measurements);
69% of sample fee lines are weight-tier fees with no upstream evidence (docs/decisions.md, finding F-07).
Run:  uvicorn agents.prep.app:app --port 8102
===============================================================
"""
from shared.utils import sample_data
from shared.utils.records import build_output, build_record, check
from shared.utils.server import make_app
from shared.utils.stubs import STUB_MODEL, photos, verdict_from

STAGE = "prep"
AGENT_ID = "prep-stub@0"
# (check_key, csv column, passing values, failing values). "not_required" rows produce no check.
RULES = [
    ("polybag_sealed", "polybag_present_sealed", {"yes"}, {"not_sealed", "missing"}),
    ("suffocation_warning", "suffocation_warning", {"legible"}, {"obscured_by_fold", "missing"}),
    ("fnsku_label_placement", "fnsku_label_placement", {"flat"}, {"on_seam", "on_curve", "on_edge", "missing"}),
    ("original_barcode_covered", "original_barcode_covered", {"yes"}, {"no"}),
    ("expiry_legible", "expiry_date", {"legible"}, {"illegible_after_wrap"}),
    ("handling_marks", "handling_marks", {"all_present"}, {"some_missing"}),
]


def handle(request: dict) -> dict:
    s = request["subject"]
    r = sample_data.row("prep", s["subject_id"], s["org_id"])
    refs = [p["ref"] for p in photos(r)]
    checks = [
        check(key, verdict_from(r[col], ok, bad), None, expected=sorted(ok)[0], observed=r[col],
              evidence_refs=refs, uncertain_reason="poor_image")
        for key, col, ok, bad in RULES if r[col] != "not_required"
    ]
    verdict = "FAIL" if any(c["verdict"] == "FAIL" for c in checks) else (
        "UNCERTAIN" if any(c["verdict"] == "UNCERTAIN" for c in checks) or not checks else "PASS")
    outcome = {"PASS": "compliant", "FAIL": "non_compliant", "UNCERTAIN": "pending_review"}[verdict]
    record = build_record(
        request, agent_id=AGENT_ID, record_id=r["record_id"], captured_at=r["captured_at"], operator_id=r["operator_id"],
        refs={"work_order_id": r["work_order_id"], "fba_shipment_id": r["fba_shipment_id"], "sku": r["sku"],
              "asin": r["asin"], "fnsku": r["fnsku"]},
        checks=checks, outcome=outcome, model=STUB_MODEL, inputs=photos(r),
        reason=f"stub replay of sample row; {sum(c['verdict'] == 'FAIL' for c in checks)} failed check(s)",
        payload={"prep_price_usd": float(r["prep_price_usd"]), "measurements": None},
    )
    return build_output(record)


app = make_app(STAGE, handle)
