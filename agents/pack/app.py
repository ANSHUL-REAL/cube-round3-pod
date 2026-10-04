"""Pack Manager: agent entry point.

========================  REPLACE ME  ========================
ORGANISER STUB replaying the Round 2 sample CSV.
Member 3: bring your Round 2 Pack Manager here and make `handle()` call it.
Only merchant-fulfilled / 3PL units reach Pack (route == "mfn"); Amazon packs FBA boxes.
Run:  uvicorn agents.pack.app:app --port 8103
===============================================================
"""
from shared.utils import sample_data
from shared.utils.records import build_output, build_record, check
from shared.utils.server import make_app
from shared.utils.stubs import STUB_MODEL, photos

STAGE = "pack"
AGENT_ID = "pack-stub@0"


def parse_lines(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in filter(None, text.split(";")):
        sku, _, qty = part.partition(":")
        out[sku] = out.get(sku, 0) + int(qty or 1)
    return out


def handle(request: dict) -> dict:
    s = request["subject"]
    r = sample_data.row("pack", s["subject_id"], s["org_id"])
    refs = [p["ref"] for p in photos(r)]
    want, got = parse_lines(r["order_lines"]), parse_lines(r["observed_in_box"])
    missing = sorted(k for k in want if k not in got)
    short = sorted(k for k in want if k in got and got[k] != want[k])
    extra = sorted(k for k in got if k not in want)
    checks = [
        check("items_present", "FAIL" if missing else "PASS", None, expected=sorted(want), observed=sorted(got),
              detail=f"missing: {missing}" if missing else "", evidence_refs=refs),
        check("quantities_correct", "FAIL" if short else "PASS", None, expected=want,
              observed={k: got[k] for k in want if k in got}, evidence_refs=refs),
        check("no_extra_items", "FAIL" if extra else "PASS", None, expected=[], observed=extra, evidence_refs=refs),
    ]
    pack_out = "seal" if all(c["verdict"] == "PASS" for c in checks) else "stop_and_fix"
    record = build_record(
        request, agent_id=AGENT_ID, record_id=r["record_id"], captured_at=r["captured_at"], operator_id=r["operator_id"],
        unit_scope="order", refs={"order_id": r["order_id"]}, checks=checks, outcome=pack_out, model=STUB_MODEL,
        inputs=photos(r), reason=f"stub replay of sample row; agent says {pack_out}",
        payload={"channel": r["channel"], "operator_verdict": r["operator_verdict"],
                 "agent_agrees_with_operator": r["operator_verdict"] == pack_out},
    )
    return build_output(record)


app = make_app(STAGE, handle)
