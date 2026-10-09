"""A deployed console (orchestration/web/access.py, web/admin.py, orchestration/faults.py): sign-in, one org's code
never reaching another org, admin-only pages, revoking a code, fault switches, and the audit trail.

Every client here is a remote one (PHONE): a request from the machine itself counts as the admin by design."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestration import api, faults
from orchestration.store import FileStore
from orchestration.web import access, station

ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"
A_UNIT, B_UNIT = "UNIT-0014", None  # B_UNIT: the first bravo unit, found below
PHONE = ("203.0.113.20", 50000)
ADMIN_PW = "test-admin-password-not-real"


def _bravo_unit() -> str:
    from orchestration.web import _all_cases

    return next(c["unit_id"] for c in _all_cases() if c["org_id"] == BRAVO)


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PW)
    for v in ("POD_LAN_CODE", "POD_UI_STUBS", "DATABASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    station._WRONG.clear()
    access._CODES.clear()
    access._AUDIT.clear()
    access._REVOKED_CACHE.clear()
    faults._MEM.clear()
    yield tmp_path
    faults._MEM.clear()


def remote() -> TestClient:
    return TestClient(api.app, client=PHONE, follow_redirects=False)


def admin() -> TestClient:
    c = remote()
    r = c.post("/login", data={"password": ADMIN_PW})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    return c


def issue(c: TestClient, org: str, label: str = "judges") -> str:
    r = c.post("/admin/codes", data={"label": label, "org": org, "role": "operator"})
    assert r.status_code == 200
    import re

    return re.search(r'class="bigcode-inline mono"[^>]*>(\d{8})<', r.text).group(1)


def test_a_stranger_gets_nothing_but_health(deployed):
    c = remote()
    assert c.get("/health").status_code == 200, "organisers' /health stays public"
    assert c.get("/").headers["location"].startswith("/join")
    assert c.get("/admin").headers["location"].startswith("/login"), "the admin page asks for the admin password"
    assert c.head("/health").status_code == 200, "uptime monitors use HEAD"
    assert c.get("/whoami").status_code == 401
    assert c.get("/workflows").status_code == 401
    assert c.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).status_code == 401
    assert c.get("/docs").status_code == 401


def test_admin_signs_in_and_a_wrong_password_is_counted(deployed):
    c = remote()
    bad = c.post("/login", data={"password": "nope"})
    assert "bad=1" in bad.headers["location"] and station._WRONG[PHONE[0]] == 1
    c = admin()
    assert c.get("/admin").status_code == 200
    assert c.get("/whoami").json() == {"role": "admin", "actor": "admin", "orgs": "all", "access_control": True}
    actions = [a["action"] for a in access.audit_log()]
    assert "admin_signed_in" in actions and "admin_sign_in_failed" in actions


def test_one_orgs_code_never_reaches_the_other_org(deployed):
    boss = admin()
    boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT})  # alpha has a workflow (and evidence)
    alpha_wf = f"WF-{ALPHA}-{A_UNIT}"
    rec = api.STORE.load_workflow(alpha_wf)["evidence_references"][0]
    code = issue(boss, BRAVO, "bravo judges")

    bravo = remote()
    assert bravo.post("/join", data={"code": code, "next": "/"}).status_code == 303
    assert bravo.get("/whoami").json()["orgs"] == [BRAVO]
    # every way in to alpha's data answers "not found", exactly like something that does not exist
    assert bravo.get(f"/workflows/{alpha_wf}").status_code == 404
    assert bravo.get(f"/workflows/{alpha_wf}/evidence").status_code == 404
    assert bravo.post(f"/workflows/{alpha_wf}/resume").status_code == 404
    assert bravo.get(f"/evidence/{rec}").status_code == 404
    assert bravo.get(f"/ui/w/{alpha_wf}").status_code == 404
    assert bravo.get(f"/ui/capture/{ALPHA}/{A_UNIT}").status_code == 404
    assert bravo.get(f"/ui/photo/{A_UNIT}/receiving/01-p.jpg").status_code == 404
    assert bravo.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).status_code == 404
    assert bravo.get("/workflows").json()["count"] == 0
    assert A_UNIT not in bravo.get("/").text, "alpha's units are not even listed"
    assert bravo.get("/admin").status_code == 303, "not the admin page"
    assert bravo.post("/admin/codes", data={"label": "x"}).status_code == 403
    # and its own org works, over the API with the code as a bearer token
    unit = _bravo_unit()
    api_client = TestClient(api.app, client=("198.51.100.4", 1), headers={"Authorization": f"Bearer {code}"})
    r = api_client.post("/workflows", json={"org_id": BRAVO, "unit_id": unit})
    assert r.status_code == 200 and r.json()["org_id"] == BRAVO
    assert [w["org_id"] for w in api_client.get("/workflows").json()["workflows"]] == [BRAVO]
    assert boss.get("/workflows").json()["count"] == 2, "the admin sees both orgs"


def test_a_revoked_code_signs_its_holder_out(deployed):
    boss = admin()
    code = issue(boss, ALPHA)
    judge = remote()
    judge.post("/join", data={"code": code})
    assert judge.get("/ui/station").status_code == 200
    cid = access.list_codes()[0]["id"]
    assert boss.post(f"/admin/codes/{cid}/revoke").status_code == 303
    assert judge.get("/ui/station").status_code == 303, "the session ends with its code"
    assert remote().post("/join", data={"code": code}).headers["location"].startswith("/join?msg=")
    assert all(c.get("sha") is None for c in access.list_codes()), "the code itself is never listed"


def test_an_agent_switched_off_fails_the_workflow_then_resume_finishes_it(deployed):
    boss = admin()
    assert boss.post("/admin/faults/receiving", data={"mode": "down"}).status_code == 303
    health = boss.get("/health").json()
    assert health["status"] == "degraded" and health["agents"]["receiving"]["status"] == "down"
    wf = boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).json()
    rcv = next(s for s in wf["stage_results"] if s["stage"] == "receiving")
    assert rcv["state"] == "error" and rcv["error"]["code"] == "agent_unavailable"
    assert wf["status"] not in ("COMPLETED",) and (wf.get("final_outcome") or {}).get("outcome") != "CLEAN"
    assert boss.post("/admin/faults/receiving", data={"mode": "ok"}).status_code == 303
    after = boss.post(f"/workflows/{wf['workflow_id']}/resume").json()
    assert next(s for s in after["stage_results"] if s["stage"] == "receiving")["state"] == "completed"
    actions = [a["action"] for a in access.audit_log()]
    assert "agent_faulted" in actions and "agent_restored" in actions


def test_garbage_from_an_agent_is_refused_not_used(deployed):
    boss = admin()
    boss.post("/admin/faults/receiving", data={"mode": "garbage"})
    wf = boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).json()
    rcv = next(s for s in wf["stage_results"] if s["stage"] == "receiving")
    assert rcv["state"] == "error" and rcv["error"]["code"] == "invalid_output"
    assert not any(r.startswith("BAD-") for r in wf["evidence_references"]), "the broken answer is never stored"


def test_an_override_is_recorded_against_who_signed_in(deployed):
    boss = admin()
    code = issue(boss, ALPHA, "Judges alpha")
    wf = boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).json()
    rec = wf["evidence_references"][0]
    judge = TestClient(api.app, client=("198.51.100.7", 1), headers={"Authorization": f"Bearer {code}"})
    r = judge.post(f"/workflows/{wf['workflow_id']}/overrides",
                   json={"record_id": rec, "new_verdict": "PASS", "actor": "admin", "reason": "checked by hand"})
    assert r.status_code == 200
    assert r.json()["overrides"][-1]["actor"] == "code:Judges alpha (admin)", "a typed name never replaces the identity"


def test_every_change_is_in_the_audit_log(deployed):
    boss = admin()
    boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT})
    log = access.audit_log()
    assert any(a["action"] == "request" and a["target"] == "POST /workflows" and a["actor"] == "admin" for a in log)
    page = boss.get("/admin").text
    assert "POST /workflows" in page and "Audit log" in page


def test_the_evidence_endpoint_checks_the_seal(deployed):
    boss = admin()
    wf = boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).json()
    body = boss.get(f"/evidence/{wf['evidence_references'][0]}").json()
    assert body["content_hash_ok"] is True and body["record"]["workflow_id"] == wf["workflow_id"]


def test_the_review_queue_lists_only_what_needs_a_person(deployed):
    boss = admin()
    boss.post("/admin/faults/pack", data={"mode": "down"})
    boss.post("/admin/faults/prep", data={"mode": "down"})
    boss.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT})
    queue = boss.get("/workflows?needs=review").json()
    assert queue["count"] >= 1 and all(w["status"] in api.REVIEW for w in queue["workflows"])
    assert A_UNIT in boss.get("/ui/review").text


def test_without_settings_a_laptop_stays_open(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    for v in ("ADMIN_PASSWORD", "POD_LAN_CODE", "DATABASE_URL"):
        monkeypatch.delenv(v, raising=False)
    c = remote()
    assert c.get("/").status_code == 200 and c.get("/whoami").json()["access_control"] is False


def test_a_wrong_value_in_database_url_stops_the_server_without_printing_it(monkeypatch):
    from shared.utils import db

    monkeypatch.setenv("DATABASE_URL", "AIzaSy-this-is-an-api-key-not-a-database")
    with pytest.raises(RuntimeError) as exc:
        db.url()
    assert "AIza" not in str(exc.value) and "not printed" in str(exc.value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.x:[YOUR-PASSWORD]@h:5432/postgres")
    with pytest.raises(RuntimeError, match="YOUR-PASSWORD"):
        db.url()
