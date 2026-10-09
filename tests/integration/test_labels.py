"""Labelling in the app (/ui/label) and the Accuracy page (/ui/accuracy): blind labels from two people score the
agents the way scripts/score_checks.py does, scoped to the sellers each person may see."""
from __future__ import annotations

import pytest

import orchestration.api as api
from orchestration.web import labels
from tests.integration.test_deployment_access import BRAVO, admin, deployed, issue, remote  # noqa: F401

ALPHA, UNIT = "org_demo_alpha", "UNIT-0008"  # alpha, merchant-fulfilled: receiving, pack


@pytest.fixture
def ran(deployed, monkeypatch):  # noqa: F811
    """A unit run on the replay stubs, with the photos its records cite put in place (the stubs cite files that were
    never shipped), so its checks can be labelled."""
    monkeypatch.setenv("POD_UI_STUBS", "1")
    labels._MEM.clear()
    boss = admin()
    assert boss.post("/ui/run", data={"org": ALPHA, "unit": UNIT}).status_code == 303
    wf = api.STORE.load_workflow(f"WF-{ALPHA}-{UNIT}")
    for rid in wf["evidence_references"]:
        for i in api.STORE.get_evidence(rid).get("inputs", []):
            if i.get("kind") == "image":
                f = deployed / "input" / i["ref"]
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"\xff\xd8\xff photo")
    yield boss
    labels._MEM.clear()


def person(boss, name: str, org: str = ALPHA, stage: str = ""):
    c = remote()
    code = issue(boss, org, name) if not stage else None
    if stage:
        import re

        r = boss.post("/admin/codes", data={"label": name, "org": org, "role": "operator", "stage": stage})
        code = re.search(r'class="bigcode-inline mono"[^>]*>(\d{8})<', r.text).group(1)
    assert c.post("/join", data={"code": code, "next": "/"}).status_code == 303
    return c


def label_all(c, value: str) -> int:
    n = 0
    while True:
        item, _ = labels._next_for(c.get("/whoami").json()["actor"].removeprefix("code:"), {ALPHA})
        if item is None:
            return n
        r = c.post("/ui/label", data={"record_id": item["record_id"], "check_key": item["check_key"], "label": value})
        assert r.status_code == 303, r.text
        n += 1


def test_the_label_page_never_shows_the_agents_answer(ran):
    items = labels.items({ALPHA})
    assert items, "the run's checks are there to label"
    page = person(ran, "Ana").get("/ui/label").text
    shown = next(i for i in items if i["record_id"] in page and i["check_key"] in page)
    rec = api.STORE.get_evidence(shown["record_id"])
    check = next(c for c in rec["checks"] if c["check_key"] == shown["check_key"])
    assert f'chip {check["verdict"].lower()}' not in page
    if check.get("observed") not in (None, "", check.get("expected")):
        assert str(check["observed"]) not in page, "what the agent observed would anchor the labeller"
    assert 'value="PASS"' in page and 'value="FAIL"' in page and "/ui/photo/" in page


def test_two_people_who_agree_score_the_agent_and_disagreements_are_listed(ran):
    ana, ben = person(ran, "Ana"), person(ran, "Ben")
    n = label_all(ana, "PASS")
    assert n == len(labels.items({ALPHA}))
    assert labels.accuracy({ALPHA})["labelled_by_both"] == 0, "one person is not a pair"
    label_all(ben, "PASS")
    acc = ana.get("/ui/accuracy.json").json()
    assert acc["labelled_by_both"] == n and acc["human_kappa"] == 1.0 and not acc["disagreements"]
    assert acc["labellers"] == ["Ana", "Ben"]
    scored = sum(v["n"] for v in acc["per_check"].values())
    assert scored == n
    # the stubs replay a clean unit: agent PASS and label PASS is a true negative
    assert all(v["FP"] == 0 and v["FN"] == 0 for v in acc["per_check"].values())
    assert "How often the agents are right" in ana.get("/ui/accuracy").text

    item = labels.items({ALPHA})[0]
    ben.post("/ui/label", data={"record_id": item["record_id"], "check_key": item["check_key"], "label": "FAIL",
                                "notes": "label torn"})
    acc = labels.accuracy({ALPHA})
    assert len(acc["disagreements"]) == 1 and acc["disagreements"][0]["notes"] == "label torn"
    assert acc["labelled_by_both"] == n, "changing your label replaces it; it is still one person"


def test_another_seller_cannot_label_or_see_these_checks(ran):
    bo = person(ran, "Bo", org=BRAVO)
    item = labels.items({ALPHA})[0]
    assert bo.post("/ui/label", data={"record_id": item["record_id"], "check_key": item["check_key"],
                                      "label": "PASS"}).status_code == 404
    assert bo.get("/ui/accuracy.json").json()["checks_to_label"] == 0
    assert "Nothing left for you to label" in bo.get("/ui/label").text


def test_a_station_code_may_label(ran):
    packer = person(ran, "Pat", stage="pack")
    item = labels.items({ALPHA})[0]
    r = packer.post("/ui/label", data={"record_id": item["record_id"], "check_key": item["check_key"], "label": "UNSURE"})
    assert r.status_code == 303
    assert packer.post("/ui/label", data={"record_id": item["record_id"], "check_key": item["check_key"],
                                          "label": "MAYBE"}).status_code == 422
