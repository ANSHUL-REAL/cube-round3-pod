"""Returns Manager: agent entry point.

========================  REPLACE ME  ========================
ORGANISER STUB replaying the Round 2 sample CSV.
Member 4: bring your Round 2 Returns Manager here and make `handle()` call it.
The stub does NOT grade condition: Amazon's published condition scale must be looked up
(Round 2 data leaves `amazon_condition` empty on purpose). payload.condition_graded says so.
Run:  uvicorn agents.returns.app:app --port 8104
===============================================================
"""
from shared.utils import sample_data
from shared.utils.records import build_output, build_record, check
from shared.utils.server import make_app
from shared.utils.stubs import STUB_MODEL, photos, previous, verdict_from

STAGE = "returns"
AGENT_ID = "returns-stub@0"


def handle(request: dict) -> dict:
    s = request["subject"]
    r = sample_data.row("returns", s["subject_id"], s["org_id"])
    refs = [p["ref"] for p in photos(r)]
    missing = [p for p in r["parts_missing"].split(";") if p]
    checks = [
        check("identity_match", verdict_from(r["identity_match"], {"yes"}, {"no"}), None,
              expected=r["ordered_sku"], observed=r["identity_match"], evidence_refs=refs, uncertain_reason="poor_image"),
        check("completeness", "FAIL" if missing else "PASS", None, expected=r["parts_list"].split(";"),
              observed={"missing": missing}, evidence_refs=refs),
    ]
    verdict = "FAIL" if any(c["verdict"] == "FAIL" for c in checks) else (
        "UNCERTAIN" if any(c["verdict"] == "UNCERTAIN" for c in checks)
        or r["operator_disposition"] == "pending_review" else "PASS")
    record = build_record(
        request, agent_id=AGENT_ID, record_id=r["record_id"], captured_at=r["captured_at"], operator_id=r["operator_id"],
        refs={"order_id": r["order_id"], "sku": r["ordered_sku"], "asin": r["ordered_asin"]},
        checks=checks, outcome=r["operator_disposition"], verdict=verdict, model=STUB_MODEL, inputs=photos(r),
        reason="stub replay: disposition copied from the operator column, not decided by an agent",
        payload={"observed_state": r["observed_state"], "condition_graded": False, "amazon_condition": None,
                 "sent_contents_seen": previous(request, "pack") is not None},
    )
    return build_output(record)


app = make_app(STAGE, handle)
