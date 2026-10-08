"""`eval run` orchestration (§21.8): given an already-assembled list of `EvaluatedUnit`,
compute every §21 section and write `manifest.json`, `metrics.json`, `report.md`, and
`per_unit_table.csv` under `eval/runs/<run_id>/`.

This module is deliberately DB- and model-free: it takes `Sequence[EvaluatedUnit]` and
writes files. That split is what makes `--dev-mini` a genuine end-to-end proof rather than
a mock - the exact same function that would process 50+ sealed units with real human
labels and real agent results processes a handful of hand-built dev-mini units, with
nothing swapped out or stubbed along the way.

Building `EvaluatedUnit`s from a REAL sealed run (fetching `eval/labels/*.json` and the
matching `rm.inspection_results` rows) needs real human-labelled data, which does not
exist in this repository yet (build-log.md OQ-5). That loader is out of scope for this
phase; `--dev-mini` is the only mode wired to a caller here. The CLI command's
`--dev-mini` path builds its own small set of hand-crafted units, clearly marked
synthetic, and calls exactly this function.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from returns_manager.eval import manifest as manifest_io
from returns_manager.eval.agreement import agreement_report
from returns_manager.eval.confusion import (
    completeness_fp_fn,
    condition_fp_fn,
    disposition_confusion,
    identity_fp_fn,
    tag_failure_modes,
    unit_presence_fp_fn,
)
from returns_manager.eval.models import CONDITION_GRADE_ORDER, EvaluatedUnit, RunManifest
from returns_manager.eval.per_unit_table import build_rows, to_csv
from returns_manager.eval.report import ReportSections, build_report
from returns_manager.eval.selective import selective_prediction_report


def _field(units: list[EvaluatedUnit], getter: Any) -> list[str]:
    return [getter(u) for u in units]


def run_eval(
    units: list[EvaluatedUnit],
    *,
    run_id: str,
    eval_root: Path,
    dev_mini: bool,
    units_requested: int | None = None,
    seed: int = 20260925,
    spend_preflight: dict[str, object] | None = None,
) -> RunManifest:
    """Computes every §21 section over `units` and writes the run's four output files.
    Returns the written `RunManifest`. Raises `ValueError` if `units` is empty."""
    if not units:
        raise ValueError("run_eval: at least one evaluated unit is required")

    started_at = datetime.now(UTC).isoformat()

    tagged_units, failure_modes = tag_failure_modes(units)

    agreement: dict[str, dict[str, Any]] = {}
    for check, ordinal in (
        ("identity", None),
        ("completeness", None),
        ("condition", CONDITION_GRADE_ORDER),
        ("unit_presence", None),
    ):

        def get(u: EvaluatedUnit, c: str = check) -> str:
            return str(getattr(u.agent, c))

        def get_gold(u: EvaluatedUnit, c: str = check) -> str:
            return str(getattr(u.gold, c))

        def get_a(u: EvaluatedUnit, c: str = check) -> str:
            return str(getattr(u.human_a, c))

        def get_b(u: EvaluatedUnit, c: str = check) -> str:
            return str(getattr(u.human_b, c))

        a_labels = [get_a(u) for u in tagged_units]
        b_labels = [get_b(u) for u in tagged_units]
        gold_labels = [get_gold(u) for u in tagged_units]
        agent_labels = [get(u) for u in tagged_units]

        agreement[check] = {
            "labeller_a_vs_b": agreement_report(a_labels, b_labels, ordinal_labels=ordinal, seed=seed),
            "model_vs_gold": agreement_report(agent_labels, gold_labels, ordinal_labels=ordinal, seed=seed),
        }
        # Flatten the two AgreementResult dicts into one per-pairing dict the report
        # renderer expects: {pairing_label: AgreementResult} per statistic name merged.
        flat: dict[str, Any] = {}
        for pairing, stats in agreement[check].items():
            for stat_name, result in stats.items():
                flat[f"{pairing}/{stat_name}"] = result
        agreement[check] = flat

    agent_presence = _field(tagged_units, lambda u: u.agent.unit_presence)
    gold_presence = _field(tagged_units, lambda u: u.gold.unit_presence)
    agent_identity = _field(tagged_units, lambda u: u.agent.identity)
    gold_identity = _field(tagged_units, lambda u: u.gold.identity)
    agent_completeness = _field(tagged_units, lambda u: u.agent.completeness)
    gold_completeness = _field(tagged_units, lambda u: u.gold.completeness)
    agent_condition = _field(tagged_units, lambda u: u.agent.condition)
    gold_condition = _field(tagged_units, lambda u: u.gold.condition)
    agent_disposition = _field(tagged_units, lambda u: u.agent.disposition)
    gold_disposition = _field(tagged_units, lambda u: u.gold.disposition)

    selective = {
        "identity": selective_prediction_report(agent_identity, gold_identity),
        "completeness": selective_prediction_report(agent_completeness, gold_completeness),
        "condition": selective_prediction_report(agent_condition, gold_condition),
    }

    fp_fn = {
        "unit_presence": unit_presence_fp_fn(agent_presence, gold_presence),
        "identity": identity_fp_fn(agent_identity, gold_identity),
        "completeness": completeness_fp_fn(agent_completeness, gold_completeness),
        "condition": condition_fp_fn(agent_condition, gold_condition),
    }

    dc = disposition_confusion(agent_disposition, gold_disposition)

    completed_at = datetime.now(UTC).isoformat()
    run_manifest = RunManifest(
        run_id=run_id,
        dev_mini=dev_mini,
        units_requested=units_requested if units_requested is not None else len(units),
        units_evaluated=len(units),
        seed=seed,
        spend_preflight=spend_preflight or {},
        actual_cost_usd=sum(u.agent.cost_usd or 0.0 for u in tagged_units),
        actual_requests=0,  # eval/run.py makes no model calls itself; see module docstring
        started_at=started_at,
        completed_at=completed_at,
    )

    metrics: dict[str, Any] = {
        "manifest": asdict(run_manifest),
        "agreement": {
            check: {pairing: asdict(result) for pairing, result in stats.items()}
            for check, stats in agreement.items()
        },
        "selective_prediction": {check: asdict(r) for check, r in selective.items()},
        "fp_fn": {check: asdict(r) for check, r in fp_fn.items()},
        "disposition_confusion": asdict(dc),
        "failure_modes": asdict(failure_modes),
    }

    rows = build_rows(tagged_units)
    csv_text = to_csv(rows)
    report_md = build_report(
        ReportSections(
            manifest=run_manifest,
            units=tagged_units,
            agreement=agreement,
            selective=selective,
            fp_fn=fp_fn,
            disposition_confusion=dc,
            failure_modes=failure_modes,
        )
    )

    manifest_io.write_manifest(eval_root, run_manifest)
    manifest_io.write_metrics(eval_root, run_id, metrics)
    manifest_io.write_report(eval_root, run_id, report_md)
    manifest_io.write_per_unit_table(eval_root, run_id, csv_text)

    return run_manifest
