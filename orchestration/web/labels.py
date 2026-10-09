"""People label the agents' checks from the photos (/ui/label), and the Accuracy page scores the agents against them
(/ui/accuracy). The method is scripts/score_checks.py's, which this reuses: a labeller never sees the agent's verdict
or what it observed (only the photos, the question and what was ordered), two different people label each check, and
the agents are scored only where the two agree. Where they disagree it is listed, not scored; their agreement is
Cohen's kappa. An UNCERTAIN answer is counted on its own, never dropped.

Only the latest completed record of each photo stage is labelled, and only one with photos that exist (a replay of
recorded evidence has none). Labels are kept in the database when there is one, else in this process.
"""
from __future__ import annotations

import collections
import importlib
import threading

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import STAGES, _api, _back, _photo_exists, _render, _same_origin

router = APIRouter(dependencies=[Depends(_same_origin)])
PHOTO_STAGES = ("receiving", "prep", "pack", "returns")
VALUES = {"PASS": "Looks right", "FAIL": "A problem", "UNSURE": "Can't tell"}
_MEM: dict[tuple[str, str, str], dict] = {}  # (record_id, check_key, labeller) -> label, without a database
_LOCK = threading.Lock()


def _score_rows():
    return importlib.import_module("scripts.score_checks").score_rows


def items(scope: set[str] | None) -> list[dict]:
    """Every check there is to label, for the sellers in `scope` (None = all), newest unit first."""
    store = _api().STORE
    out = []
    wfs = sorted(store.list_workflows(scope), key=lambda w: w.get("updated_at") or "", reverse=True)
    for wf in wfs:
        for sr in wf["stage_results"]:
            if sr["stage"] not in PHOTO_STAGES or sr["state"] != "completed" or not sr.get("record_id"):
                continue
            rec = store.get_evidence(sr["record_id"])
            if not rec:
                continue
            photos = [i["ref"] for i in rec.get("inputs", []) if i.get("kind") == "image" and _photo_exists(i["ref"])]
            if not photos:
                continue
            for c in rec.get("checks", []):
                out.append({"org_id": wf["org_id"], "unit_id": wf["subject_id"], "stage": sr["stage"],
                            "record_id": rec["record_id"], "check_key": c["check_key"], "verdict": c["verdict"],
                            "question": c.get("question") or c["check_key"].replace("_", " ").capitalize() + "?",
                            "expected": c.get("expected"), "photos": photos})
    return out


def all_labels(scope: set[str] | None) -> list[dict]:
    from shared.utils import db

    if not db.enabled():
        with _LOCK:
            rows = list(_MEM.values())
    else:
        rows = [{"record_id": r, "check_key": k, "labeller": who, "org_id": o, "label": v, "notes": n or "", "at": str(at)}
                for r, k, who, o, v, n, at in db.fetchall(
                    f"select record_id, check_key, labeller, org_id, label, notes, at from {db.SCHEMA}.labels order by at")]
    return [r for r in rows if scope is None or r["org_id"] in scope]


def save(item: dict, labeller: str, label: str, notes: str) -> None:
    from shared.utils import db

    row = {"record_id": item["record_id"], "check_key": item["check_key"], "labeller": labeller,
           "org_id": item["org_id"], "label": label, "notes": notes, "at": db.now()}
    if db.enabled():
        db.execute(f"insert into {db.SCHEMA}.labels (record_id, check_key, labeller, org_id, label, notes) "
                   f"values (%s,%s,%s,%s,%s,%s) on conflict (record_id, check_key, labeller) do update set "
                   f"label=excluded.label, notes=excluded.notes, at=now()",
                   (row["record_id"], row["check_key"], labeller, row["org_id"], label, notes))
    else:
        with _LOCK:
            _MEM[(row["record_id"], row["check_key"], labeller)] = row


def _by_check(labels: list[dict]) -> dict[tuple[str, str], list[dict]]:
    by: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for lb in labels:
        by[(lb["record_id"], lb["check_key"])].append(lb)
    return by


def accuracy(scope: set[str] | None) -> dict:
    """scripts/score_checks.py's numbers over the labels given in the app: label_a and label_b are the first two
    different people who labelled a check ("Can't tell" counts as not labelled)."""
    its = items(scope)
    by = _by_check(all_labels(scope))
    rows = []
    for it in its:
        firsts = [lb for lb in by.get((it["record_id"], it["check_key"]), []) if lb["label"] in ("PASS", "FAIL")][:2]
        rows.append({**{k: it[k] for k in ("record_id", "check_key", "stage", "unit_id")},
                     "label_a": firsts[0]["label"] if firsts else "", "label_b": firsts[1]["label"] if len(firsts) > 1 else "",
                     "notes": "; ".join(lb["notes"] for lb in firsts if lb["notes"])})
    result = _score_rows()(rows, {(i["record_id"], i["check_key"]): i["verdict"] for i in its})
    labelled = [r for r in rows if r["label_a"] and r["label_b"]]
    result.update({
        "checks_to_label": len(its), "checks_labelled_once": sum(1 for r in rows if r["label_a"] and not r["label_b"]),
        "units": len({(i["org_id"], i["unit_id"]) for i in its}),
        "units_labelled_by_two": len({r["unit_id"] for r in labelled}),
        "labellers": sorted({lb["labeller"] for lbs in by.values() for lb in lbs}),
        "method": "Two different people label each check from the photos alone (never the agent's verdict). Scored "
                  "only where they agree; FAIL is the positive class (a problem found). scripts/score_checks.py.",
    })
    return result


def _next_for(labeller: str, scope: set[str] | None) -> tuple[dict | None, dict]:
    """The next check this person has not labelled: first one somebody else has labelled once (to complete a pair)."""
    its = items(scope)
    by = _by_check(all_labels(scope))
    mine = sum(1 for lbs in by.values() for lb in lbs if lb["labeller"] == labeller)
    open_ = [i for i in its if not any(lb["labeller"] == labeller for lb in by.get((i["record_id"], i["check_key"]), []))]
    pairs = [i for i in open_ if len(by.get((i["record_id"], i["check_key"]), [])) == 1]
    fresh = [i for i in open_ if not by.get((i["record_id"], i["check_key"]))]
    nxt = (pairs or fresh or [None])[0]
    return nxt, {"mine": mine, "total": len(its), "left": len([i for i in open_ if len(by.get((i["record_id"], i["check_key"]), [])) < 2])}


# ---------------------------------------------------------------- pages
@router.get("/ui/label", response_class=HTMLResponse)
def label_page(request: Request):
    from .access import current

    who = current()
    item, progress = _next_for(who.name, who.scope)
    return _render(request, "label.html", item=item, progress=progress, values=VALUES, stage_meta=STAGES, nav="accuracy")


@router.post("/ui/label")
def label_post(record_id: str = Form(...), check_key: str = Form(...), label: str = Form(...), notes: str = Form("")):
    from .access import current

    who = current()
    if label not in VALUES:
        raise HTTPException(422, "label is PASS, FAIL or UNSURE")
    item = next((i for i in items(who.scope) if i["record_id"] == record_id and i["check_key"] == check_key), None)
    if item is None:  # another seller's record, or not a labelled photo check: as if it did not exist
        raise HTTPException(404, "no such check to label")
    save(item, who.name, label, notes.strip()[:300])
    return _back("/ui/label", f"Saved: {VALUES[label].lower()}.")


@router.get("/ui/accuracy", response_class=HTMLResponse)
def accuracy_page(request: Request):
    from .access import current

    return _render(request, "accuracy.html", acc=accuracy(current().scope), stage_meta=STAGES, nav="accuracy")


@router.get("/ui/accuracy.json")
def accuracy_json():
    from .access import current

    return JSONResponse(accuracy(current().scope))
