"""A deployed console (orchestration/web/access.py, web/admin.py, orchestration/faults.py): sign-in, one org's code
never reaching another org, admin-only pages, revoking a code, fault switches, and the audit trail.

Every client here is a remote one (PHONE): a request from the machine itself counts as the admin by design."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestration import api, faults
from orchestration.store import FileStore
from orchestration.web import access, brand, station

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
    brand._MEM.clear()
    yield tmp_path
    faults._MEM.clear()
    brand._MEM.clear()


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
    home = c.get("/")  # the public website: how it works and one way in, no data
    assert home.status_code == 200 and 'href="/join"' in home.text and 'href="/login"' not in home.text
    assert 'href="/login"' in c.get("/join").text, "admins reach the password from the one sign-in page"
    assert A_UNIT not in home.text and "org_demo_alpha" not in home.text
    assert c.get("/ui/sim").headers["location"].startswith("/join")
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


def test_a_station_code_runs_only_its_own_step_and_the_step_says_who(deployed, monkeypatch):
    boss = admin()
    r = boss.post("/admin/codes", data={"label": "Priya", "org": ALPHA, "role": "operator", "stage": "receiving"})
    import re

    code = re.search(r'class="bigcode-inline mono"[^>]*>(\d{8})<', r.text).group(1)
    assert access.list_codes()[0]["stage"] == "receiving"
    priya = remote()
    landed = priya.post("/join", data={"code": code})
    assert landed.headers["location"] == "/ui/station/receiving", "straight to her own station"
    # her own step: allowed (the station runs the real Receiving agent; no key here, so it answers pending)
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 90)).save(buf, "JPEG")
    ok = priya.post(f"/ui/station/receiving/{ALPHA}/{A_UNIT}", files=[("files", ("p.jpg", buf.getvalue(), "image/jpeg"))])
    assert ok.status_code == 303
    # anyone else's step, the whole-unit run, overrides and the API: refused
    assert priya.post(f"/ui/station/prep/{ALPHA}/{A_UNIT}").status_code == 403
    assert priya.post("/ui/run", data={"org": ALPHA, "unit": A_UNIT}).status_code == 403
    assert priya.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).status_code == 403
    assert priya.get(f"/ui/w/WF-{ALPHA}-{A_UNIT}").status_code == 200, "she can still read"
    wf = api.STORE.load_workflow(f"WF-{ALPHA}-{A_UNIT}")
    done = [t for t in wf["transitions"] if t["event"] in ("stage_completed", "stage_error") and t["stage"] == "receiving"]
    assert done and done[-1]["by"] == "Priya", "the step records who ran it"
    assert "👤 Priya" in priya.get(f"/ui/w/WF-{ALPHA}-{A_UNIT}").text


def test_a_pasted_model_key_is_tidied(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", '  GEMINI_API_KEY="AIzaFAKEFAKEFAKE"  ')
    api._tidy_key()
    import os

    assert os.environ["GEMINI_API_KEY"] == "AIzaFAKEFAKEFAKE"


def test_the_admin_page_says_when_the_model_key_is_missing(deployed):
    boss = admin()
    assert "no GEMINI_API_KEY" in boss.get("/admin").text


def test_an_admin_adds_a_seller_and_its_people_see_only_that_seller(deployed):
    boss = admin()
    r = boss.post("/admin/sellers", data={"name": "  Gamma   Goods "})
    assert r.status_code == 303 and "bad=1" not in r.headers["location"]
    assert brand.org_name("org_gamma_goods") == "Gamma Goods"
    page = boss.get("/admin").text
    assert "Gamma Goods" in page and "no units yet" in page
    code = issue(boss, "org_gamma_goods", "Gita")
    gamma = remote()
    assert gamma.post("/join", data={"code": code, "next": "/"}).status_code == 303
    assert gamma.get("/whoami").json()["orgs"] == ["org_gamma_goods"]
    home = gamma.get("/").text
    assert "No units yet" in home and A_UNIT not in home and "Gamma Goods" in home
    assert gamma.get("/workflows").json()["count"] == 0
    assert gamma.post("/workflows", json={"org_id": ALPHA, "unit_id": A_UNIT}).status_code == 404
    assert gamma.post("/admin/sellers", data={"name": "Mine now"}).status_code == 403, "only an admin adds sellers"
    assert any(a["action"] == "seller_added" and a["target"] == "Gamma Goods" for a in access.audit_log())


def test_a_seller_name_must_be_new_and_sensible(deployed):
    boss = admin()
    for name in ("alpha retail", "x", ""):
        r = boss.post("/admin/sellers", data={"name": name})
        assert "bad=1" in r.headers["location"], name
    assert boss.post("/admin/sellers", data={"name": "Gamma Goods"}).status_code == 303
    assert "bad=1" in boss.post("/admin/sellers", data={"name": "GAMMA goods"}).headers["location"]
    assert set(brand.org_names()) == {ALPHA, BRAVO, "org_gamma_goods"}


def test_renaming_a_seller_keeps_its_data_and_its_codes(deployed):
    boss = admin()
    code = issue(boss, BRAVO, "Bo")
    r = boss.post(f"/admin/sellers/{BRAVO}/rename", data={"name": "Bravo Wholesale"})
    assert r.status_code == 303 and "bad=1" not in r.headers["location"]
    assert brand.org_name(BRAVO) == "Bravo Wholesale"
    bo = remote()
    bo.post("/join", data={"code": code, "next": "/"})
    assert bo.get("/whoami").json()["orgs"] == [BRAVO], "the id, and so the data and codes, did not change"
    assert "Bravo Wholesale" in bo.get("/").text
    assert boss.post("/admin/sellers/org_nobody/rename", data={"name": "Ghost"}).status_code == 404
    assert "bad=1" in boss.post(f"/admin/sellers/{BRAVO}/rename", data={"name": "Alpha Retail"}).headers["location"]


def test_a_new_seller_id_is_stable_and_never_reused():
    assert brand.new_org_id("Gamma Goods!", set()) == "org_gamma_goods"
    assert brand.new_org_id("Gamma Goods", {"org_gamma_goods"}) == "org_gamma_goods_2"
    assert brand.new_org_id("???", set()) == "org_seller"


def test_an_admin_action_says_what_it_did_on_the_page_it_returns_to(deployed):
    boss = admin()
    r = boss.post("/admin/sellers", data={"name": "Gamma Goods"})
    loc = r.headers["location"]
    assert loc.startswith("/admin?msg=") and loc.endswith("#sellers"), "the message must come before the '#'"
    assert "Seller added: Gamma Goods" in boss.get(loc.split("#")[0]).text
    from orchestration.web import _back

    assert _back("/ui/w/X?tab=1#top", "ok", bad=True).headers["location"] == "/ui/w/X?tab=1&msg=ok&bad=1#top"
