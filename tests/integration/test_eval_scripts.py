"""The evaluation scripts: the per-check scorer's arithmetic, and the tenancy probe."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts import evaluate, score_checks

REAL_AGENTS = {"receiving", "prep", "pack", "returns", "recovery"}


def _store(tmp: Path) -> Path:
    (tmp / "workflows").mkdir(parents=True)
    (tmp / "evidence").mkdir()
    checks = [{"check_key": k, "verdict": v} for k, v in
              [("a", "FAIL"), ("b", "FAIL"), ("c", "PASS"), ("d", "PASS"), ("e", "UNCERTAIN"), ("f", "PASS")]]
    rec = {"record_id": "PCK-1", "stage": "pack", "subject": {"org_id": "o", "subject_id": "U1"},
           "inputs": [{"kind": "image", "ref": "U1/pack/1.jpg"}], "checks": checks}
    (tmp / "evidence" / "PCK-1.json").write_text(json.dumps(rec))
    wf = {"stage_results": [{"stage": "pack", "state": "completed", "record_id": "PCK-1"},
                            {"stage": "recovery", "state": "completed", "record_id": "RCY-1"}]}
    (tmp / "workflows" / "WF-1.json").write_text(json.dumps(wf))
    return tmp


def test_sheet_hides_the_agents_verdict_and_lists_every_check(tmp_path):
    store = _store(tmp_path / "out")
    dest = tmp_path / "labels.csv"
    assert score_checks.sheet(store, dest) == 6
    text = dest.read_text()
    assert "FAIL" not in text and "UNCERTAIN" not in text and "U1/pack/1.jpg" in text


def test_score_counts_tp_fp_fn_tn_uncertain_and_disagreements(tmp_path):
    store = _store(tmp_path / "out")
    labels = tmp_path / "labels.csv"
    score_checks.sheet(store, labels)
    rows = list(csv.DictReader(labels.open()))
    answers = {"a": ("FAIL", "FAIL"), "b": ("PASS", "PASS"), "c": ("FAIL", "FAIL"), "d": ("PASS", "PASS"),
               "e": ("FAIL", "FAIL"), "f": ("PASS", "FAIL")}
    for r in rows:
        r["label_a"], r["label_b"] = answers[r["check_key"]]
    with labels.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=score_checks.FIELDS)
        w.writeheader()
        w.writerows(rows)
    res = score_checks.score(labels, store)
    pc = res["per_check"]
    assert pc["pack:a"]["TP"] == 1 and pc["pack:b"]["FP"] == 1 and pc["pack:c"]["FN"] == 1 and pc["pack:d"]["TN"] == 1
    assert pc["pack:e"]["UNCERTAIN"] == 1 and pc["pack:e"]["UNCERTAIN_when_label_FAIL"] == 1  # never dropped
    assert "pack:f" not in pc and len(res["disagreements"]) == 1  # people disagreed: listed, not scored
    assert res["labelled_by_both"] == 6 and res["human_kappa"] is not None and res["human_kappa"] < 1


def test_kappa_edges():
    assert score_checks.kappa([("PASS", "PASS"), ("FAIL", "FAIL")]) == 1.0
    assert score_checks.kappa([]) is None


def test_every_real_agent_refuses_another_organisations_unit():
    assert all(v.startswith("refused") for v in evaluate.tenancy().values())


def test_evaluation_doc_matches_the_committed_numbers():
    """docs/evaluation.md is written from docs/eval/system.json; if either changes, they must change together."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "evaluation.md").read_text(encoding="utf-8")
    d = json.loads((root / "docs" / "eval" / "system.json").read_text(encoding="utf-8"))
    r = d["replay"]
    for outcome, n in r["final_outcome"].items():
        assert f"| {outcome} | {n} |" in doc, outcome
    assert f"**Needing a person: {r['needs_a_person']} of {r['workflows']}.**" in doc
    injected = sum(v["workflows_hit"] for v in d["faults"].values())
    assert f"({injected:,} workflows): 0 crashes, 0 reported as clean" in doc
    assert all(v["crashes"] == 0 and v["ended_clean"] == 0 for v in d["faults"].values())
    ref = r["vs_reference"]
    assert f"on {ref['same_status_and_outcome']} of {ref['compared']} units" in doc
    assert f"${r['recovery']['claimed_usd']:.2f} in total" in doc
