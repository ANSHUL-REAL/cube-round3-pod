"""Units added from the app (orchestration/web/units.py, /ui/units/new): a typed-in order becomes a unit every agent
checks against, scoped to its seller, with no sample-CSV row behind it."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from orchestration import api, faults
from orchestration.orchestrator import new_workflow, load_flow
from orchestration.store import FileStore
from orchestration.web import access, brand, station, units

ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"
PHONE = ("203.0.113.40", 50000)
ADMIN_PW = "test-admin-password-not-real"
MFN = {"org": ALPHA, "sku": "SKU-CANDLE-3", "cartons": "2", "per_carton": "12", "route": "mfn", "in_box": "1",
       "channel": "shopify"}


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PW)
    for v in ("POD_LAN_CODE", "POD_UI_STUBS", "DATABASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    cat = tmp_path / "catalogue"  # a new product is written here, never into the repo's catalogue
    (cat / "sample").mkdir(parents=True)
    (cat / "sample" / "catalogue.json").write_text((units.ROOT / "agents/pack/catalogue/sample/catalogue.json").read_text())
    monkeypatch.setattr(units, "CATALOGUE_DIR", cat)
    for mem in (station._WRONG, access._CODES, access._AUDIT, access._REVOKED_CACHE, faults._MEM, brand._MEM, units._MEM):
        mem.clear()
    yield tmp_path
    for mem in (faults._MEM, brand._MEM, units._MEM):
        mem.clear()


def remote() -> TestClient:
    return TestClient(api.app, client=PHONE, follow_redirects=False)


def admin() -> TestClient:
    c = remote()
    assert c.post("/login", data={"password": ADMIN_PW}).status_code == 303
    return c


def code_for(boss: TestClient, org: str, label: str, stage: str = "") -> TestClient:
    r = boss.post("/admin/codes", data={"label": label, "org": org, "role": "operator", "stage": stage})
    code = re.search(r'class="bigcode-inline mono"[^>]*>(\d{8})<', r.text).group(1)
    c = remote()
    assert c.post("/join", data={"code": code, "next": "/"}).status_code == 303
    return c


def added(c: TestClient, form: dict) -> str:
    r = c.post("/ui/units", data=form)
    assert r.status_code == 303 and "bad=1" not in r.headers["location"], r.headers["location"]
    return re.search(r"/ui/capture/[^/]+/(UNIT-\d+)", r.headers["location"]).group(1)


def test_an_admin_adds_a_unit_and_only_its_seller_sees_it(deployed):
    boss = admin()
    assert "＋ Add a unit" in boss.get("/").text
    assert boss.get("/ui/units/new").status_code == 200
    unit = added(boss, MFN)
    assert unit == "UNIT-0101", "one past the highest unit number"
    photos = boss.get(f"/ui/capture/{ALPHA}/{unit}").text
    assert "The open box from above" in photos and "Soy Candle Trio" in photos and "2 carton(s)" in photos
    assert unit in boss.get("/").text
    assert any(a["action"] == "unit_added" and a["target"] == unit and a["org_id"] == ALPHA for a in access.audit_log())

    alpha, bravo = code_for(boss, ALPHA, "Ana"), code_for(boss, BRAVO, "Ben")
    assert unit in alpha.get("/").text
    assert unit not in bravo.get("/").text
    assert bravo.get(f"/ui/capture/{ALPHA}/{unit}").status_code == 404
    assert bravo.post("/ui/units", data=MFN).status_code == 404, "a seller's code cannot add to another seller"
    assert added(bravo, {**MFN, "org": BRAVO, "sku": "SKU-MUG-11"}) == "UNIT-0102"


def test_a_seller_adds_a_new_product_for_fba(deployed):
    boss = admin()
    boss.post("/admin/sellers", data={"name": "Gamma Goods"})
    gita = code_for(boss, "org_gamma_goods", "Gita")
    form = {"org": "org_gamma_goods", "sku": "__new__", "title": "Ceramic Planter", "colour": "terracotta",
            "variant": "15 cm", "components": "planter, saucer", "looks": "a pot on a saucer", "cartons": "1",
            "per_carton": "6", "route": "fba", "polybag": "1", "mark_fragile": "1", "returned": "1"}
    unit = added(gita, form)
    case = next(c for c in units.stored() if c["unit_id"] == unit)
    assert case["route"] == "fba" and case["returned"] is True
    assert case["order"]["sku"] == "SKU-CERAMIC-PLANTER" and case["order"]["spec_components"] == ["planter", "saucer"]
    assert case["order"]["requirements"] == {"polybag": True, "suffocation_warning": True, "expiry_date": False,
                                             "handling_marks": ["fragile"], "cover_original_barcode": True}
    assert case["return"]["ordered_sku"] == "SKU-CERAMIC-PLANTER"
    own = json.loads((units.CATALOGUE_DIR / "org_gamma_goods" / "catalogue.json").read_text())
    assert [i["sku"] for i in own["items"]] == ["SKU-CERAMIC-PLANTER"]
    assert "Ceramic Planter · SKU-CERAMIC-PLANTER" in gita.get("/ui/units/new").text, "the seller's own product is offered next time"
    assert "SKU-CERAMIC-PLANTER" not in boss.get(f"/ui/units/new?org={ALPHA}").text, "and never to another seller"
    page = gita.get(f"/ui/capture/org_gamma_goods/{unit}").text
    assert "FNSKU label" in page and "Returns" in page
    assert "No units yet" not in gita.get("/").text


def test_what_cannot_make_a_unit_is_refused_with_a_reason(deployed):
    boss = admin()
    for bad in ({"cartons": "0"}, {"per_carton": "lots"}, {"route": "air"}, {"sku": "SKU-NOPE"},
                {"cartons": "500", "per_carton": "10000"}, {"sku": "__new__", "title": "x"}):
        r = boss.post("/ui/units", data={**MFN, **bad})
        assert r.status_code == 303 and "bad=1" in r.headers["location"], bad
    assert units.stored() == [] and not (units.CATALOGUE_DIR / ALPHA).exists(), "nothing half-made is kept"
    assert boss.post("/ui/units", data={**MFN, "org": "org_nobody"}).status_code == 404


def test_a_station_code_cannot_add_units(deployed):
    boss = admin()
    packer = code_for(boss, ALPHA, "Pat", stage="pack")
    assert packer.get("/ui/units/new").status_code == 403
    assert packer.post("/ui/units", data=MFN).status_code == 403
    assert "＋ Add a unit" not in packer.get("/").text


@pytest.mark.parametrize("route", ["mfn", "fba"])
def test_every_agent_reads_the_added_order_and_only_for_its_own_seller(route):
    item = next(p for p in units.products(ALPHA) if p["sku"] == "SKU-CANDLE-3")
    case = units.build_case(ALPHA, "UNIT-0900", item, {**MFN, "route": route, "returned": "1"}, "test")
    wf = new_workflow(case, load_flow())
    ours = {"subject": {"org_id": ALPHA, "subject_id": "UNIT-0900"}, "context": {"case": wf["context"]}}
    theirs = {"subject": {"org_id": BRAVO, "subject_id": "UNIT-0900"}, "context": {"case": wf["context"]}}

    from agents.pack.orders import resolve_order as pack_order
    from agents.prep.workorders import resolve as prep_order
    from agents.receiving.orders import resolve_order as po_line
    from agents.recovery.app import _ordered_here
    from agents.returns.returned import resolve_return

    po, _ = po_line(ours)
    assert (po.sku, po.qty_ordered, po.spec_components) == ("SKU-CANDLE-3", 24, ["candle x3", "gift box"])
    if route == "mfn":
        order, _ = pack_order(ours)
        assert [(ln.sku, ln.qty) for ln in order.lines] == [("SKU-CANDLE-3", 1)]
    else:
        assert prep_order(ours).refs["sku"] == "SKU-CANDLE-3"
    assert resolve_return(ours).ordered_sku == "SKU-CANDLE-3"
    assert _ordered_here(ours) and not _ordered_here(theirs)
    for resolver in (po_line, resolve_return, *((pack_order,) if route == "mfn" else (prep_order,))):
        with pytest.raises(LookupError):
            resolver(theirs)  # another seller's subject never gets this order


def test_recovery_runs_on_an_added_unit_and_claims_nothing_without_charges():
    from agents.recovery.app import handle

    item = next(p for p in units.products(ALPHA) if p["sku"] == "SKU-CABLE-USBC")
    case = units.build_case(ALPHA, "UNIT-0901", item, MFN, "test")
    ctx = new_workflow(case, load_flow())["context"]
    wf = f"WF-{ALPHA}-UNIT-0901"
    out = handle({"schema_version": "1.0", "request_id": f"{wf}:recovery", "workflow_id": wf, "stage": "recovery",
                  "subject": {"org_id": ALPHA, "subject_id": "UNIT-0901", "route": "mfn"}, "inputs": [],
                  "previous_evidence": [], "context": {"overrides": [], "case": ctx}})
    assert out["evidence"]["payload"]["claimable_usd"] in (0, "0", "0.00", 0.0)


def test_unit_ids_count_on_from_the_highest():
    assert units.next_unit_id({"UNIT-0100", "UNIT-0007", "X"}) == "UNIT-0101"
    assert units.next_unit_id(set()) == "UNIT-0001"
