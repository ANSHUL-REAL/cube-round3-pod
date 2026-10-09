"""A unit's receipt (/ui/w/<id>/receipt and receipt.json): every record it cites, each hash re-checked, one fingerprint
over them all, and nothing for a seller the reader may not see."""
from __future__ import annotations

import hashlib
import json

import orchestration.api as api
from shared.utils.hashing import canonical_json
from tests.integration.test_console import ALPHA, WF, run, ui  # noqa: F401  (ui is a fixture)
from tests.integration.test_deployment_access import BRAVO, admin, deployed, issue, remote  # noqa: F401


def test_the_receipt_lists_every_record_with_its_hash_checked(ui):  # noqa: F811
    run(ui)
    wf = api.STORE.load_workflow(WF)
    page = ui.get(f"/ui/w/{WF}/receipt")
    assert page.status_code == 200
    assert f"All {len(wf['evidence_references'])} record hashes check out" in page.text
    for rid in wf["evidence_references"]:
        assert api.STORE.get_evidence(rid)["content_hash"] in page.text
    assert f"/ui/w/{WF}/receipt" in ui.get(f"/ui/w/{WF}").text, "the unit page links to its receipt"


def test_the_json_receipt_downloads_and_its_fingerprint_can_be_recomputed(ui):  # noqa: F811
    run(ui)
    r = ui.get(f"/ui/w/{WF}/receipt.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.json()
    rc = body["receipt"]
    assert rc["hashes_ok"] is True and rc["records"] == len(body["workflow"]["evidence_references"])
    chain = [[rid, rec["content_hash"]] for rid, rec in body["evidence"].items()]
    assert rc["fingerprint"] == hashlib.sha256(canonical_json({"workflow_id": WF, "records": chain})).hexdigest()
    assert "not a signature" in rc["how_to_check"]


def test_a_record_changed_after_sealing_is_flagged(ui, tmp_path):  # noqa: F811
    run(ui)
    rid = api.STORE.load_workflow(WF)["evidence_references"][0]
    path = tmp_path / "out" / "evidence" / f"{rid}.json"
    rec = json.loads(path.read_text())
    rec["decision"]["reason"] = "edited by hand"
    path.write_text(json.dumps(rec))
    rc = ui.get(f"/ui/w/{WF}/receipt.json").json()["receipt"]
    assert rc["hashes_ok"] is False and rc["hash_check"][rid] is False
    assert "does not match" in ui.get(f"/ui/w/{WF}/receipt").text


def test_another_sellers_receipt_is_not_found(deployed, monkeypatch):  # noqa: F811
    monkeypatch.setenv("POD_UI_STUBS", "1")
    boss = admin()
    assert boss.post("/ui/run", data={"org": ALPHA, "unit": "UNIT-0014"}).status_code == 303
    bravo = remote()
    bravo.post("/join", data={"code": issue(boss, BRAVO, "Ben"), "next": "/"})
    assert bravo.get(f"/ui/w/{WF}/receipt").status_code == 404
    assert bravo.get(f"/ui/w/{WF}/receipt.json").status_code == 404
    assert boss.get(f"/ui/w/{WF}/receipt.json").status_code == 200
