"""System evaluation for docs/evaluation.md: measured on this repo, nothing typed in by hand.

    python scripts/evaluate.py                 # writes docs/eval/system.json and prints a summary

What it runs (no model key and no network are needed):
  1. Replay   all sample units through the orchestrator; Receiving, Prep, Pack and Returns replay the organisers'
              recorded evidence (labelled stubs), Recovery is OUR real agent. Outcomes, statuses, the share needing a
              person, every claim with the records it rests on, Recovery's per-charge positions, latency per stage.
  2. Faults   the same units with one stage broken at a time (down, timeout, invalid output, crash, wrong tenant):
              how many workflows end FAILED/INCOMPLETE, how many crash, how many wrongly end clean.
  3. Tenancy  every real agent asked about another organisation's unit: refused or not.
  4. Live, no photos   our five real agents on every unit before any photo exists: every photo stage must say
              `no_capture` and invent nothing.
Per-check accuracy of the vision agents against human labels is NOT measured here: it needs labelled photos
(scripts/score_checks.py and docs/evaluation.md say how). This script only measures what the repo can prove by itself.
"""
from __future__ import annotations

import collections
import copy
import importlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestration.clients import AgentRejected, AgentTimeout, AgentUnavailable  # noqa: E402
from orchestration.orchestrator import load_flow, run_workflow  # noqa: E402
from orchestration.store import MemoryStore  # noqa: E402

PHOTO_STAGES = ("receiving", "prep", "pack", "returns")
STAGES = (*PHOTO_STAGES, "recovery")


class InProc:
    def __init__(self, handle):
        self.handle = handle

    def run(self, request, timeout_s):
        try:
            return self.handle(request)
        except LookupError as exc:
            raise AgentRejected(str(exc)) from exc


def stub_clients() -> dict:
    return {s: InProc(importlib.import_module(f"tests.stubs.{s}_stub").handle) for s in PHOTO_STAGES}


def real_handle(stage: str):
    return importlib.import_module(f"agents.{stage}.app").handle


class Broken:
    """One way an agent can fail. The orchestrator must record it, never crash and never report success."""

    def __init__(self, kind: str, inner=None):
        self.kind, self.inner = kind, inner

    def run(self, request, timeout_s):
        if self.kind == "down":
            raise AgentUnavailable("connection refused (injected)")
        if self.kind == "timeout":
            raise AgentTimeout(f"no answer within {timeout_s}s (injected)")
        if self.kind == "crash":
            raise RuntimeError("agent raised (injected)")
        if self.kind == "invalid_output":
            return {"schema_version": "1.0", "nonsense": True}
        if self.kind == "wrong_tenant":
            out = copy.deepcopy(self.inner.run(request, timeout_s))
            out["evidence"]["subject"]["org_id"] = "org_someone_else"
            return out
        raise ValueError(self.kind)


def cases() -> list[dict]:
    return json.loads((ROOT / "data" / "sample" / "cases.json").read_text())


def run_all(clients_for, flow) -> tuple[list[dict], MemoryStore, int]:
    store, wfs, crashes = MemoryStore(), [], 0
    for case in cases():
        try:
            wfs.append(run_workflow(case, flow, store, clients_for(case)))
        except Exception:  # noqa: BLE001 - counted, that is the point
            crashes += 1
    return wfs, store, crashes


def replay(flow) -> dict:
    clients = stub_clients()
    t0 = time.perf_counter()
    wfs, store, crashes = run_all(lambda c: clients, flow)
    wall = time.perf_counter() - t0
    by_status = collections.Counter(w["status"] for w in wfs)
    by_outcome = collections.Counter((w.get("final_outcome") or {}).get("outcome") for w in wfs)
    by_route = collections.defaultdict(collections.Counter)
    for w in wfs:
        route = f"{w['context'].get('route')}{' + returned' if w['context'].get('returned') else ''}"
        by_route[route][(w.get("final_outcome") or {}).get("outcome")] += 1
    needs_human = sum(1 for w in wfs if (w.get("final_outcome") or {}).get("needs_human") or w["status"] == "BLOCKED")
    latency = collections.defaultdict(list)
    for w in wfs:
        for sr in w["stage_results"]:
            if sr.get("duration_ms") is not None:
                latency[sr["stage"]].append(sr["duration_ms"])
    claims, positions, checks = [], collections.Counter(), collections.Counter()
    for w in wfs:
        rec_sr = next(sr for sr in w["stage_results"] if sr["stage"] == "recovery")
        rec = store.get_evidence(rec_sr["record_id"]) if rec_sr.get("record_id") else None
        if not rec:
            continue
        for c in rec.get("checks", []):
            checks[c["verdict"]] += 1
        for ch in rec.get("payload", {}).get("charges", []):
            positions[ch.get("position")] += 1
        for cl in rec.get("payload", {}).get("claims", []):
            claims.append({"unit": w["subject_id"], "org": w["org_id"], "line_id": cl.get("line_id"),
                           "charge_type": cl.get("charge_type"), "amount_usd": cl.get("claim_usd"), "basis_kind": cl.get("basis_kind"),
                           "rests_on": cl.get("evidence_record_ids") or cl.get("evidence") or [], "record_id": rec_sr["record_id"]})
    # The organisers' reference roll-up (their stubs + their Recovery). Agreement is NOT accuracy: the sample is
    # unlabelled; it only shows where our Recovery and roll-up differ from theirs, and why (named in the doc).
    expected = json.loads((ROOT / "data" / "expected" / "final-outcomes.sample.json").read_text())
    differs = []
    for w in wfs:
        ref = expected.get(w["workflow_id"])
        ours = w.get("final_outcome") or {}
        if ref is None or ref.get("outcome") != ours.get("outcome") or ref.get("status") != w["status"]:
            differs.append({"workflow": w["workflow_id"], "reference": ref and {"status": ref.get("status"), "outcome": ref.get("outcome"),
                            "claimable_usd": ref.get("claimable_usd")}, "ours": {"status": w["status"], "outcome": ours.get("outcome")}})
    return {
        "vs_reference": {"compared": len(wfs), "same_status_and_outcome": len(wfs) - len(differs), "differs": differs},
        "units": len(cases()), "workflows": len(wfs), "crashes": crashes, "wall_seconds": round(wall, 2),
        "status": dict(by_status), "final_outcome": {str(k): v for k, v in by_outcome.items()},
        "outcome_by_route": {k: {str(o): n for o, n in v.items()} for k, v in sorted(by_route.items())},
        "needs_a_person": needs_human, "needs_a_person_share": round(needs_human / max(len(wfs), 1), 3),
        "latency_ms": {s: {"n": len(v), "median": statistics.median(v), "max": max(v)} for s, v in latency.items()},
        "recovery": {"check_verdicts": dict(checks), "charge_positions": {str(k): v for k, v in positions.items()},
                     "claims": claims, "claimed_usd": round(sum(float(c["amount_usd"] or 0) for c in claims), 2)},
    }


def faults(flow) -> dict:
    out = {}
    for stage in STAGES:
        for kind in ("down", "timeout", "invalid_output", "crash", "wrong_tenant"):
            base = stub_clients()
            inner = base.get(stage) or InProc(real_handle(stage))

            def clients_for(case, stage=stage, kind=kind, base=base, inner=inner):
                return {**base, stage: Broken(kind, inner)}

            wfs, _, crashes = run_all(clients_for, flow)
            hit = [w for w in wfs if any(sr["stage"] == stage and sr["state"] != "skipped" for sr in w["stage_results"])]
            failed = sum(1 for w in hit if w["status"] == "FAILED" or (w.get("final_outcome") or {}).get("outcome") == "INCOMPLETE")
            clean = sum(1 for w in hit if (w.get("final_outcome") or {}).get("outcome") == "CLEAN")
            recorded = sum(1 for w in hit if any(e.get("stage") == stage for e in w["errors"]))
            out[f"{stage}:{kind}"] = {"workflows_hit": len(hit), "failed_or_incomplete": failed, "error_recorded": recorded,
                                      "ended_clean": clean, "crashes": crashes}
    return out


def tenancy() -> dict:
    out = {}
    for stage in STAGES:
        req = {"schema_version": "1.0", "request_id": f"EVAL-tenant-{stage}", "workflow_id": "WF-eval", "stage": stage,
               "subject": {"org_id": "org_demo_bravo", "subject_id": "UNIT-0001", "route": "fba"},  # UNIT-0001 is alpha's
               "inputs": [], "previous_evidence": [], "context": {"overrides": [], "case": {}}}
        try:
            real_handle(stage)(req)
            out[stage] = "ANSWERED (not refused)"
        except LookupError:
            out[stage] = "refused (LookupError)"
        except Exception as exc:  # noqa: BLE001
            out[stage] = f"error {type(exc).__name__}"
    return out


def live_no_photos(flow) -> dict:
    wfs, store, crashes = run_all(lambda c: None, flow)
    codes, invented = collections.Counter(), 0
    for w in wfs:
        for sr in w["stage_results"]:
            if sr["stage"] in PHOTO_STAGES and sr["state"] != "skipped":
                rec = store.get_evidence(sr["record_id"]) if sr.get("record_id") else None
                err = (rec or {}).get("error") or sr.get("error") or {}
                codes[err.get("code") or sr["state"]] += 1
                if rec and rec["decision"]["verdict"] in ("PASS", "FAIL"):
                    invented += 1
    return {"workflows": len(wfs), "crashes": crashes, "photo_stage_answers": dict(codes),
            "verdicts_without_a_photo": invented, "status": dict(collections.Counter(w["status"] for w in wfs))}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["INPUT_DIR"] = str(Path(tmp) / "input")  # no photos at all, so "live" really has none
        os.environ["PACK_LEDGER_PATH"] = str(Path(tmp) / "ledger.json")
        os.environ.pop("GEMINI_API_KEY", None)
        flow = load_flow()
        result = {"replay": replay(flow), "faults": faults(flow), "tenancy": tenancy(), "live_no_photos": live_no_photos(flow)}
    dest = ROOT / "docs" / "eval"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "system.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    r = result["replay"]
    print(f"replay: {r['workflows']} workflows, {r['crashes']} crashes, outcomes {r['final_outcome']}")
    print(f"        claims {len(r['recovery']['claims'])} totalling ${r['recovery']['claimed_usd']}, needs a person {r['needs_a_person']}")
    bad = {k: v for k, v in result["faults"].items() if v["crashes"] or v["ended_clean"] or v["error_recorded"] < v["workflows_hit"]}
    print(f"faults: {len(result['faults'])} injections, problems: {bad or 'none'}")
    print(f"tenancy: {result['tenancy']}")
    print(f"live, no photos: {result['live_no_photos']}")


if __name__ == "__main__":
    main()
