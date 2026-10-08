"""The per-unit evaluation table (§21.9; `per_unit_table.csv`).

The handbook's own words: "test unit -> human label -> agent result -> agreement/
disagreement -> uncertainty -> failure-mode notes." One row per unit; disagreements
first, then uncertain, then agreements, so a reviewer sees the interesting rows first
without scrolling. CSV is RFC 4180, UTF-8, no BOM; the same rows also render as a
Markdown table for `report.md`.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from returns_manager.eval.confusion import condition_error_label
from returns_manager.eval.models import EvaluatedUnit

COLUMNS: tuple[str, ...] = (
    "unit_id",
    "scenario_codes",
    "lighting",
    "angle",
    "blur",
    "ambiguity",
    "product_seen_in_dev",
    "human_a_identity",
    "human_b_identity",
    "gold_identity",
    "agent_identity",
    "identity_agree",
    "human_a_completeness",
    "human_b_completeness",
    "gold_completeness",
    "agent_completeness",
    "completeness_agree",
    "gold_parts_missing",
    "agent_parts_missing",
    "human_a_condition",
    "human_b_condition",
    "gold_condition",
    "agent_condition",
    "condition_agree",
    "condition_error",
    "gold_disposition",
    "agent_disposition",
    "agent_requires_review",
    "disposition_agree",
    "agent_uncertain_checks",
    "uncertainty_reasons",
    "latency_ms",
    "cost_usd",
    "failure_mode",
    "notes",
)


def _agree(agent: str, gold: str) -> str:
    """`*_agree` (§21.9): yes | no | agent_uncertain."""
    if agent == "uncertain":
        return "agent_uncertain"
    return "yes" if agent == gold else "no"


def _agree_optional(agent: str | None, gold: str | None) -> str:
    if agent is None:
        return "agent_uncertain"
    return "yes" if agent == gold else "no"


def build_row(unit: EvaluatedUnit) -> dict[str, str]:
    a, g = unit.agent, unit.gold
    return {
        "unit_id": unit.meta.unit_id,
        "scenario_codes": ";".join(unit.meta.scenario_codes),
        "lighting": unit.meta.lighting,
        "angle": unit.meta.angle,
        "blur": unit.meta.blur,
        "ambiguity": unit.meta.ambiguity,
        "product_seen_in_dev": "yes" if unit.meta.product_seen_in_dev else "no",
        "human_a_identity": unit.human_a.identity,
        "human_b_identity": unit.human_b.identity,
        "gold_identity": g.identity,
        "agent_identity": a.identity,
        "identity_agree": _agree(a.identity, g.identity),
        "human_a_completeness": unit.human_a.completeness,
        "human_b_completeness": unit.human_b.completeness,
        "gold_completeness": g.completeness,
        "agent_completeness": a.completeness,
        "completeness_agree": _agree(a.completeness, g.completeness),
        "gold_parts_missing": ";".join(g.parts_missing),
        "agent_parts_missing": ";".join(a.parts_missing),
        "human_a_condition": unit.human_a.condition,
        "human_b_condition": unit.human_b.condition,
        "gold_condition": g.condition,
        "agent_condition": a.condition,
        "condition_agree": _agree(a.condition, g.condition),
        "condition_error": condition_error_label(a.condition, g.condition),
        "gold_disposition": g.disposition or "",
        "agent_disposition": a.disposition or "",
        "agent_requires_review": "yes" if a.requires_review else "no",
        "disposition_agree": _agree_optional(a.disposition, g.disposition),
        "agent_uncertain_checks": ";".join(a.uncertain_checks),
        "uncertainty_reasons": ";".join(a.uncertainty_reasons),
        "latency_ms": "" if a.latency_ms is None else str(a.latency_ms),
        "cost_usd": "" if a.cost_usd is None else f"{a.cost_usd:.6f}",
        "failure_mode": unit.failure_mode or "",
        "notes": unit.notes,
    }


def _sort_key(row: dict[str, str]) -> tuple[int, str]:
    """§21.9 row order: disagreements first, then uncertain, then agreements."""
    agree_cols = ("identity_agree", "completeness_agree", "condition_agree", "disposition_agree")
    values = [row[c] for c in agree_cols]
    if any(v == "no" for v in values):
        bucket = 0
    elif any(v == "agent_uncertain" for v in values):
        bucket = 1
    else:
        bucket = 2
    return bucket, row["unit_id"]


def build_rows(units: Sequence[EvaluatedUnit]) -> list[dict[str, str]]:
    rows = [build_row(u) for u in units]
    rows.sort(key=_sort_key)
    return rows


def summary_block(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, int]]:
    """§21.9: "a summary block above the table" - counts of agree/disagree/uncertain per check."""
    checks = ("identity", "completeness", "condition", "disposition")
    out: dict[str, dict[str, int]] = {}
    for check in checks:
        col = f"{check}_agree"
        out[check] = {
            "agree": sum(1 for r in rows if r[col] == "yes"),
            "disagree": sum(1 for r in rows if r[col] == "no"),
            "uncertain": sum(1 for r in rows if r[col] == "agent_uncertain"),
        }
    return out


def to_csv(rows: Sequence[dict[str, str]]) -> str:
    """RFC 4180, UTF-8, no BOM. `csv.writer`'s default dialect already is RFC 4180
    (\\r\\n line terminators, double-quote escaping) - no manual dialect tuning needed."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def to_markdown(rows: Sequence[dict[str, str]]) -> str:
    header = "| " + " | ".join(COLUMNS) + " |"
    separator = "| " + " | ".join("---" for _ in COLUMNS) + " |"
    lines = [header, separator]
    for row in rows:
        lines.append("| " + " | ".join(row[c].replace("|", "\\|") for c in COLUMNS) + " |")
    return "\n".join(lines)
