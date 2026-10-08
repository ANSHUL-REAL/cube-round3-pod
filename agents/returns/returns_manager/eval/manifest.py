"""Run manifest (§18.4, §21.8): what a run requested, what it actually spent, and where
its outputs live - `eval/runs/<run_id>/{manifest.json, metrics.json, report.md,
per_unit_table.csv}`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from returns_manager.eval.models import RunManifest


def run_dir(eval_root: Path, run_id: str) -> Path:
    return eval_root / "runs" / run_id


def write_manifest(eval_root: Path, manifest: RunManifest) -> Path:
    d = run_dir(eval_root, manifest.run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "manifest.json"
    path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_manifest(eval_root: Path, run_id: str) -> RunManifest:
    path = run_dir(eval_root, run_id) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return RunManifest(**data)


def write_metrics(eval_root: Path, run_id: str, metrics: dict[str, object]) -> Path:
    d = run_dir(eval_root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "metrics.json"
    path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def write_report(eval_root: Path, run_id: str, report_md: str) -> Path:
    d = run_dir(eval_root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "report.md"
    path.write_text(report_md, encoding="utf-8")
    return path


def write_per_unit_table(eval_root: Path, run_id: str, csv_text: str) -> Path:
    d = run_dir(eval_root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "per_unit_table.csv"
    # Newline handling is the CSV module's job (per_unit_table.to_csv already produced
    # \r\n line endings per RFC 4180); write bytes verbatim so nothing re-translates them.
    path.write_bytes(csv_text.encode("utf-8"))
    return path
