"""Per-check numbers for the photo agents against two people's labels (docs/evaluation.md, "Per check").

    # 1. after a session with real photos (for example `serve.py --data D:/pod12-demo`), write a labelling sheet:
    python scripts/score_checks.py sheet --store D:/pod12-demo/out --out data/labels/labels.csv
    # 2. two people fill label_a and label_b independently (PASS or FAIL, blank = cannot tell), looking only at the photos
    # 3. score:
    python scripts/score_checks.py score data/labels/labels.csv --store D:/pod12-demo/out

The sheet never shows the agent's verdict, so the labellers are not anchored by it. "Positive" means a problem (FAIL):
agent FAIL & label FAIL = TP, FAIL & PASS = FP, PASS & FAIL = FN, PASS & PASS = TN. An UNCERTAIN answer is never dropped:
it is counted on its own, split by what the label was. Rows where the two people disagree are listed, not scored.
Agreement between the two people is reported as Cohen's kappa. Only the latest record of each stage is scored, and a
record's checks are scored as the agent wrote them (overrides are a person's decision, not the agent's).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

PHOTO_STAGES = ("receiving", "prep", "pack", "returns")
FIELDS = ["org_id", "unit_id", "stage", "record_id", "check_key", "photos", "what_to_judge", "label_a", "label_b", "notes"]


def latest_records(store: Path) -> list[dict]:
    """The latest completed photo-stage record of every workflow in a file store."""
    out = []
    for p in sorted((store / "workflows").glob("*.json")):
        wf = json.loads(p.read_text(encoding="utf-8"))
        for sr in wf["stage_results"]:
            if sr["stage"] not in PHOTO_STAGES or sr["state"] != "completed" or not sr.get("record_id"):
                continue
            rec_p = store / "evidence" / f"{sr['record_id']}.json"
            if rec_p.exists():
                out.append(json.loads(rec_p.read_text(encoding="utf-8")))
    return out


def sheet(store: Path, dest: Path) -> int:
    rows = []
    for rec in latest_records(store):
        photos = ";".join(i["ref"] for i in rec.get("inputs", []) if i.get("kind") == "image")
        if not photos:
            continue
        for c in rec.get("checks", []):
            rows.append({"org_id": rec["subject"]["org_id"], "unit_id": rec["subject"]["subject_id"], "stage": rec["stage"],
                         "record_id": rec["record_id"], "check_key": c["check_key"], "photos": photos,
                         "what_to_judge": c.get("question") or c["check_key"].replace("_", " "),
                         "label_a": "", "label_b": "", "notes": ""})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    agree = sum(a == b for a, b in pairs) / n
    cats = {x for p in pairs for x in p}
    chance = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in cats)
    return 1.0 if chance == 1 else round((agree - chance) / (1 - chance), 3)


def score(labels: Path, store: Path) -> dict:
    verdicts = {(r["record_id"], c["check_key"]): c["verdict"] for r in latest_records(store) for c in r.get("checks", [])}
    per = collections.defaultdict(lambda: collections.Counter())
    disagreements, unmatched, both = [], 0, []
    with labels.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            a, b = row["label_a"].strip().upper(), row["label_b"].strip().upper()
            if a not in ("PASS", "FAIL") or b not in ("PASS", "FAIL"):
                continue  # not labelled by both people
            both.append((a, b))
            if a != b:
                disagreements.append({k: row[k] for k in ("unit_id", "stage", "check_key", "label_a", "label_b", "notes")})
                continue
            v = verdicts.get((row["record_id"], row["check_key"]))
            if v is None:
                unmatched += 1
                continue
            key = f"{row['stage']}:{row['check_key']}"
            if v == "UNCERTAIN":
                per[key][f"UNCERTAIN_when_{a}"] += 1
            else:
                per[key][{("FAIL", "FAIL"): "TP", ("FAIL", "PASS"): "FP", ("PASS", "FAIL"): "FN", ("PASS", "PASS"): "TN"}[(v, a)]] += 1
    table = {}
    for key, c in sorted(per.items()):
        tp, fp, fn, tn = c["TP"], c["FP"], c["FN"], c["TN"]
        unsure = c["UNCERTAIN_when_PASS"] + c["UNCERTAIN_when_FAIL"]
        table[key] = {"TP": tp, "TN": tn, "FP": fp, "FN": fn, "UNCERTAIN": unsure,
                      "UNCERTAIN_when_label_PASS": c["UNCERTAIN_when_PASS"], "UNCERTAIN_when_label_FAIL": c["UNCERTAIN_when_FAIL"],
                      "n": tp + fp + fn + tn + unsure, "uncertain_rate": round(unsure / max(tp + fp + fn + tn + unsure, 1), 3),
                      "precision_on_FAIL": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall_on_FAIL": round(tp / (tp + fn), 3) if tp + fn else None}
    return {"labelled_by_both": len(both), "human_kappa": kappa(both), "disagreements": disagreements,
            "rows_without_a_record": unmatched, "per_check": table}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("sheet", help="write a blank labelling sheet")
    s1.add_argument("--store", required=True, type=Path)
    s1.add_argument("--out", type=Path, default=Path("data/labels/labels.csv"))
    s2 = sub.add_parser("score", help="score the agents against a filled sheet")
    s2.add_argument("labels", type=Path)
    s2.add_argument("--store", required=True, type=Path)
    s2.add_argument("--json", type=Path, default=Path("docs/eval/checks.json"))
    args = ap.parse_args(argv)
    if args.cmd == "sheet":
        n = sheet(args.store, args.out)
        print(f"{n} checks to label -> {args.out}")
        return 0 if n else 1
    result = score(args.labels, args.store)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(f"labelled by both people: {result['labelled_by_both']}, human kappa: {result['human_kappa']}, "
          f"disagreements: {len(result['disagreements'])}")
    print(f"{'check':40} {'TP':>4} {'TN':>4} {'FP':>4} {'FN':>4} {'UNC':>4}")
    for k, v in result["per_check"].items():
        print(f"{k:40} {v['TP']:>4} {v['TN']:>4} {v['FP']:>4} {v['FN']:>4} {v['UNCERTAIN']:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
