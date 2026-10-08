"""Assembles `report.md` (§21.8): every number carries its own `n`, method, and CI where
one was computed. This module only renders already-computed pieces - it never computes a
statistic itself, so the numbers in the report are provably the same ones the tests check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from returns_manager.eval.agreement import AgreementResult
from returns_manager.eval.confusion import DispositionConfusion, FailureModeCounts, FpFn
from returns_manager.eval.models import EvaluatedUnit, RunManifest
from returns_manager.eval.per_unit_table import build_rows, summary_block, to_markdown
from returns_manager.eval.selective import SelectivePredictionReport


@dataclass(frozen=True)
class ReportSections:
    """Everything `build_report` needs. Every field is already-computed - assembly, not
    computation, happens in this module."""

    manifest: RunManifest
    units: Sequence[EvaluatedUnit]
    agreement: dict[str, dict[str, AgreementResult]]  # check -> pairing label -> result
    selective: dict[str, SelectivePredictionReport]  # check -> report
    fp_fn: dict[str, FpFn]  # check -> FpFn
    disposition_confusion: DispositionConfusion
    failure_modes: FailureModeCounts
    kill_condition_note: str = ""


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _agreement_section(agreement: dict[str, dict[str, AgreementResult]]) -> str:
    lines = ["## Agreement (§21.3)", ""]
    lines.append("| Check | Pairing | Statistic | Value | n | 95% CI | Method |")
    lines.append("|---|---|---|---|---|---|---|")
    for check, pairings in agreement.items():
        for pairing, result in pairings.items():
            lines.append(
                f"| {check} | {pairing} | {result.statistic} | {result.value:.3f} | {result.n} | "
                f"[{result.ci_low:.3f}, {result.ci_high:.3f}] | {result.method} |"
            )
    return "\n".join(lines)


def _selective_section(selective: dict[str, SelectivePredictionReport]) -> str:
    lines = ["## Selective prediction (§21.2)", ""]
    lines.append(
        "| Check | Strict accuracy | Coverage | Selective accuracy | Unnecessary-uncertain rate | n |"
    )
    lines.append("|---|---|---|---|---|---|")
    for check, r in selective.items():
        lines.append(
            f"| {check} | {_fmt_pct(r.strict_accuracy)} | {_fmt_pct(r.coverage)} | "
            f"{_fmt_pct(r.selective_accuracy)} | {_fmt_pct(r.unnecessary_uncertain_rate)} | {r.n} |"
        )
    return "\n".join(lines)


def _fp_fn_section(fp_fn: dict[str, FpFn]) -> str:
    lines = ["## Per-check false positives / false negatives (§21.1)", ""]
    lines.append("| Check | Positive means | FP | FP means | FN (dangerous) | FN means | n |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in fp_fn.values():
        lines.append(
            f"| {r.check} | {r.positive_meaning} | {r.fp} | {r.fp_meaning} | {r.fn} | "
            f"{r.fn_meaning} | {r.n} |"
        )
    return "\n".join(lines)


def _disposition_section(dc: DispositionConfusion) -> str:
    lines = ["## Disposition confusion matrix (§21.1)", ""]
    routes = sorted(dc.matrix.keys())
    lines.append("| gold \\ agent | " + " | ".join(routes) + " |")
    lines.append("|---" * (len(routes) + 1) + "|")
    for g in routes:
        row = " | ".join(str(dc.matrix[g][a]) for a in routes)
        lines.append(f"| {g} | {row} |")
    lines.append("")
    lines.append(f"- `restock` when gold != restock (dangerous): **{dc.restock_when_gold_not_restock}**")
    lines.append(
        f"- `dispose` when gold was recoverable (value loss): **{dc.dispose_when_gold_recoverable}**"
    )
    lines.append(f"- n = {dc.n}")
    return "\n".join(lines)


def _failure_modes_section(fm: FailureModeCounts) -> str:
    lines = ["## Failure modes (§21.4)", ""]
    if not fm.counts:
        lines.append("No disagreements were tagged with a failure mode.")
        return "\n".join(lines)
    lines.append("| Mode | Count | Example unit_ids |")
    lines.append("|---|---|---|")
    for mode, count in sorted(fm.counts.items(), key=lambda kv: -kv[1]):
        examples = ", ".join(fm.examples.get(mode, ()))
        lines.append(f"| {mode} | {count} | {examples} |")
    return "\n".join(lines)


def _manifest_section(m: RunManifest) -> str:
    lines = ["# Eval report", ""]
    kind = "DEV-MINI (tooling check only - not a reported eval result)" if m.dev_mini else "sealed eval"
    lines.append(f"- **Run:** `{m.run_id}` ({kind})")
    lines.append(f"- **Units evaluated:** {m.units_evaluated} / {m.units_requested} requested")
    lines.append(f"- **Seed:** {m.seed}")
    lines.append(f"- **Started / completed:** {m.started_at} / {m.completed_at}")
    lines.append(f"- **Actual requests / cost:** {m.actual_requests} / ${m.actual_cost_usd:.4f}")
    return "\n".join(lines)


def build_report(sections: ReportSections) -> str:
    units = sections.units
    rows = build_rows(units)
    summary = summary_block(rows)

    summary_lines = ["## Per-unit summary (§21.9)", ""]
    for check, counts in summary.items():
        summary_lines.append(
            f"- **{check}:** agree {counts['agree']}, disagree {counts['disagree']}, "
            f"uncertain {counts['uncertain']} (n={sum(counts.values())})"
        )

    parts = [
        _manifest_section(sections.manifest),
        "",
        _agreement_section(sections.agreement),
        "",
        _selective_section(sections.selective),
        "",
        _fp_fn_section(sections.fp_fn),
        "",
        _disposition_section(sections.disposition_confusion),
        "",
        _failure_modes_section(sections.failure_modes),
        "",
        "\n".join(summary_lines),
        "",
        "## Per-unit table (§21.9)",
        "",
        "Disagreements first, then uncertain, then agreements.",
        "",
        to_markdown(rows),
    ]
    if sections.kill_condition_note:
        parts.extend(["", "## Kill-condition evaluation", "", sections.kill_condition_note])
    return "\n".join(parts) + "\n"
