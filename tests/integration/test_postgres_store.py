"""The Postgres side of a deployment (shared/utils/db.py, orchestration/store.py PgStore, access codes, faults, audit).

Runs only with TEST_DATABASE_URL set (a real Postgres, e.g. the Supabase project). Each run works in its own schema
(pod12_test_<random>) and drops it afterwards, so it never touches the deployment's own `pod12` tables.

    TEST_DATABASE_URL=postgresql://... pytest tests/integration/test_postgres_store.py
"""
from __future__ import annotations

import os
import secrets

import pytest
from fastapi.testclient import TestClient

from orchestration import api, faults
from orchestration.store import EvidenceConflict, PgStore
from orchestration.web import access, station
from shared.utils import db
from shared.utils.hashing import seal

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL (a real Postgres)")
ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"
PHONE = ("203.0.113.30", 40000)
ADMIN_PW = "pg-test-admin-password-not-real"


@pytest.fixture
def pg(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr(db, "SCHEMA", f"pod12_test_{secrets.token_hex(4)}")
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("PACK_LEDGER_PATH", str(tmp_path / "out" / "pack-ledger.json"))
    db.close()
    store = PgStore()
    yield store
    db.execute(f"drop schema if exists {db.SCHEMA} cascade")
    db.close()


def wf(org: str, unit: str, status: str = "COMPLETED") -> dict:
    return {"workflow_id": f"WF-{org}-{unit}", "org_id": org, "subject_id": unit, "status": status,
            "stage_results": [], "final_outcome": {"outcome": "CLEAN"}}


def record(rid: str, org: str, reason: str = "ok") -> dict:
    return seal({"record_id": rid, "workflow_id": f"WF-{org}-U1", "stage": "receiving", "subject": {"org_id": org},
                 "decision": {"verdict": "PASS", "reason": reason}})


def test_workflows_round_trip_and_lists_are_scoped_by_org(pg):
    pg.save_workflow(wf(ALPHA, "U1"))
    pg.save_workflow(wf(BRAVO, "U2", "BLOCKED"))
    pg.save_workflow({**wf(ALPHA, "U1"), "status": "FAILED"})  # an update, not a second row
    assert pg.load_workflow(f"WF-{ALPHA}-U1")["status"] == "FAILED"
    assert pg.load_workflow("WF-nobody") is None and pg.load_workflow("../etc") is None
    assert [w["org_id"] for w in pg.list_workflows({BRAVO})] == [BRAVO]
    assert len(pg.list_workflows(None)) == 2 and pg.list_workflows(set()) == []
    row = db.fetchone(f"select org_id, status, outcome from {db.SCHEMA}.workflows where workflow_id=%s", (f"WF-{BRAVO}-U2",))
    assert row == (BRAVO, "BLOCKED", "CLEAN"), "queryable columns, not only a JSON blob"


def test_evidence_is_write_once(pg):
    r = record("RCV-1", ALPHA)
    pg.put_evidence(r)
    pg.put_evidence(r)  # the same content again is fine
    with pytest.raises(EvidenceConflict):
        pg.put_evidence(record("RCV-1", ALPHA, reason="changed after the fact"))
    assert pg.get_evidence("RCV-1")["decision"]["reason"] == "ok"
    assert db.fetchone(f"select org_id from {db.SCHEMA}.evidence where record_id='RCV-1'")[0] == ALPHA


def test_photos_survive_a_wiped_disk(pg, tmp_path):
    folder = tmp_path / "input" / "UNIT-0014" / "receiving"
    folder.mkdir(parents=True)
    (folder / "01-a.jpg").write_bytes(b"photo-a")
    (folder / "02-b.jpg").write_bytes(b"photo-b")
    os.utime(folder / "01-a.jpg", (1_700_000_000, 1_700_000_000))
    db.sync_folder("input", folder, ALPHA)
    (folder / "02-b.jpg").unlink()  # a deleted photo leaves the database too
    db.sync_folder("input", folder, ALPHA)
    for p in folder.iterdir():  # the container restarts: its disk is empty
        p.unlink()
    assert db.restore_files() == {"input": 1}
    assert (folder / "01-a.jpg").read_bytes() == b"photo-a" and not (folder / "02-b.jpg").exists()
    assert int((folder / "01-a.jpg").stat().st_mtime) == 1_700_000_000, "Prep reads the capture time from it"
    assert db.restore_files() == {}, "nothing to do when the disk already matches"


def test_the_pack_ledger_is_kept_in_the_database(pg, tmp_path):
    from agents.pack import ledger

    ledger._STATE.clear()
    ledger.remember(ALPHA, ["abc"], "ORD-1", "PCK-1")
    path = tmp_path / "out" / "pack-ledger.json"
    path.unlink()
    ledger._STATE.clear()
    db.restore_files()
    assert ledger.earlier_uses(ALPHA, ["abc"]) == [{"order_id": "ORD-1", "record_id": "PCK-1"}]
    ledger._STATE.clear()


def test_codes_faults_and_audit_live_in_the_database(pg, monkeypatch):
    monkeypatch.setattr(api, "STORE", pg)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.delenv("POD_LAN_CODE", raising=False)
    station._WRONG.clear()
    access._REVOKED_CACHE.clear()
    boss = TestClient(api.app, client=PHONE, follow_redirects=False)
    assert boss.post("/login", data={"password": ADMIN_PW}).status_code == 303
    page = boss.post("/admin/codes", data={"label": "bravo judges", "org": BRAVO, "role": "operator"}).text
    import re

    code = re.search(r'class="bigcode-inline mono"[^>]*>(\d{8})<', page).group(1)
    stored = db.fetchone(f"select code_sha256, org_id from {db.SCHEMA}.access_codes")
    assert stored[1] == BRAVO and code not in stored[0], "only the hash is stored"
    judge = TestClient(api.app, client=("198.51.100.30", 1), headers={"Authorization": f"Bearer {code}"})
    assert judge.get("/whoami").json()["orgs"] == [BRAVO]

    boss.post("/admin/faults/pack", data={"mode": "down"})
    assert faults.active()["pack"]["mode"] == "down"
    assert boss.get("/health").json()["agents"]["pack"]["status"] == "down"
    boss.post("/admin/faults/pack", data={"mode": "ok"})
    assert faults.active() == {}

    actions = {r[0] for r in db.fetchall(f"select action from {db.SCHEMA}.audit")}
    assert {"admin_signed_in", "code_issued", "agent_faulted", "agent_restored"} <= actions
    assert boss.get("/health").json()["database"]["status"] == "ok"
