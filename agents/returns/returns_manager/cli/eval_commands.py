"""P12 CLI commands: `eval seal`, `eval run`, `eval report` (§20, §21).

`eval/sealed/` and `eval/labels/` are off-limits except to `eval run` (CLAUDE.md); this
module's other commands (`seal`, `report`) never open those two directories - `seal` only
ever WRITES to `eval/sealed/`, and `report` only reads a run's own already-generated
`report.md` under `eval/runs/<run_id>/`, never the sealed unit set or its labels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from returns_manager.config import REPO_ROOT
from returns_manager.eval.models import AgentResult, EvaluatedUnit, GoldLabel, HumanLabel, UnitMeta

eval_app = typer.Typer(
    help="Evaluation tooling (never reads sealed data outside `eval run`).", no_args_is_help=True
)

EVAL_ROOT = REPO_ROOT / "eval"


def _load_unit_meta(
    units_dir: Path,
) -> tuple[list[UnitMeta], dict[str, str], dict[str, frozenset[str]]]:
    """Reads `eval/units/<unit_id>.json`. Besides the coverage-quota fields (§21.0),
    each file also carries `product_sku` and `photo_sha256` for the fixture-overlap
    check (§21.0: "any unit whose product *and* photos appear in fixtures/")."""
    units: list[UnitMeta] = []
    sku_by_unit: dict[str, str] = {}
    photo_hashes_by_unit: dict[str, frozenset[str]] = {}
    for f in sorted(units_dir.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        unit_id = data["unit_id"]
        units.append(
            UnitMeta(
                unit_id=unit_id,
                scenario_codes=tuple(data.get("scenario_codes", [])),
                lighting=data.get("lighting", "normal"),
                angle=data.get("angle", "square"),
                blur=data.get("blur", "none"),
                ambiguity=data.get("ambiguity", "clear"),
                product_seen_in_dev=bool(data.get("product_seen_in_dev", False)),
            )
        )
        if data.get("product_sku"):
            sku_by_unit[unit_id] = data["product_sku"]
        photo_hashes_by_unit[unit_id] = frozenset(data.get("photo_sha256", []))
    return units, sku_by_unit, photo_hashes_by_unit


def _fixture_overlap_sets(fixtures_dir: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Convention: `fixtures/<org>/<sku>/photos/*`. Returns (skus, photo sha256 hashes)."""
    import hashlib

    if not fixtures_dir.exists():
        return frozenset(), frozenset()
    skus: set[str] = set()
    hashes: set[str] = set()
    for photo in fixtures_dir.glob("*/*/photos/*"):
        skus.add(photo.parents[1].name)
        hashes.add(hashlib.sha256(photo.read_bytes()).hexdigest())
    return frozenset(skus), frozenset(hashes)


@eval_app.command("seal")
def eval_seal(
    dev_mini: Annotated[
        bool, typer.Option("--dev-mini", help="Dev tooling check only; skips the size/coverage bar.")
    ] = False,
    units_dir: Annotated[str, typer.Option("--units-dir", help="Candidate unit metadata JSON files.")] = str(
        EVAL_ROOT / "units"
    ),
) -> None:
    """Hash and seal the eval set: coverage-quota checks, fixture-overlap rejection, size
    guard (>= 50 units unless --dev-mini)."""
    from returns_manager.eval.seal import SealRefused, find_fixture_overlap, seal

    src = Path(units_dir)
    if not src.exists():
        typer.echo(f"error: {src} does not exist - nothing to seal", err=True)
        raise typer.Exit(2)

    units, sku_by_unit, photo_hashes_by_unit = _load_unit_meta(src)
    if not units:
        typer.echo(f"error: no unit metadata files found under {src}", err=True)
        raise typer.Exit(2)

    fixture_skus, fixture_hashes = _fixture_overlap_sets(REPO_ROOT / "fixtures")
    overlap = find_fixture_overlap(units, sku_by_unit, photo_hashes_by_unit, fixture_skus, fixture_hashes)
    if overlap:
        typer.echo(f"error: {len(overlap)} unit(s) overlap fixtures/: {', '.join(overlap)}", err=True)
        raise typer.Exit(3)

    try:
        result = seal(units, dev_mini=dev_mini)
    except SealRefused as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(3) from exc

    sealed_dir = EVAL_ROOT / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = sealed_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "unit_ids": sorted(result.unit_ids),
                "dev_mini": result.dev_mini,
                "content_sha256": result.content_sha256,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    typer.echo(f"sealed {len(result.unit_ids)} unit(s) -> {manifest_path}")
    typer.echo(f"coverage: {result.quota_result.missing_summary}")
    typer.echo(f"content_sha256: {result.content_sha256}")


def _build_dev_mini_units() -> list[EvaluatedUnit]:
    """A small, hand-crafted, clearly synthetic set - proves the §21 pipeline end to end
    without touching eval/sealed/ or eval/labels/ (§8.10, CLAUDE.md 'eval data is
    off-limits'). Never claim this is the eval; --dev-mini exists so no caller can."""
    scenarios = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
    units: list[EvaluatedUnit] = []
    for i, scenario in enumerate(scenarios):
        agree = i % 3 != 0  # a mix of agreement and disagreement, deterministically
        meta = UnitMeta(
            unit_id=f"DEV-MINI-{i:02d}",
            scenario_codes=(scenario,),
            lighting="poor" if i % 4 == 0 else "normal",
            angle="oblique" if i % 5 == 0 else "square",
            blur="slight" if i % 3 == 1 else "none",
            ambiguity="genuinely_ambiguous" if i % 6 == 0 else "clear",
            product_seen_in_dev=i % 2 == 0,
        )
        human = HumanLabel(
            labeller="dev-mini-a",
            unit_presence="product_present",
            identity="yes",
            completeness="complete",
            parts_missing=(),
            condition="Used - Good",
            disposition="restock",
        )
        gold = GoldLabel(
            unit_presence="product_present",
            identity="yes",
            completeness="complete",
            parts_missing=(),
            condition="Used - Good",
            disposition="restock",
        )
        agent = AgentResult(
            unit_presence="product_present",
            identity="yes" if agree else "uncertain",
            completeness="complete",
            parts_missing=(),
            condition="Used - Good" if agree else "Used - Very Good",
            disposition="restock",
            requires_review=not agree,
            uncertain_checks=() if agree else ("identity",),
            uncertainty_reasons=() if agree else ("photo_quality",),
            latency_ms=1500 + i * 50,
            cost_usd=0.0015,
        )
        units.append(EvaluatedUnit(meta=meta, human_a=human, human_b=human, gold=gold, agent=agent))
    return units


@eval_app.command("run")
def eval_run_command(
    run_id: Annotated[str, typer.Option("--run-id", help="A name for this run's output directory.")],
    dev_mini: Annotated[
        bool,
        typer.Option(
            "--dev-mini",
            help="Dev tooling check only, on a small hand-built set - NEVER the sealed set.",
        ),
    ] = False,
    confirm_spend: Annotated[bool, typer.Option("--confirm-spend")] = False,
) -> None:
    """Run the eval pipeline (§21) and write manifest.json, metrics.json, report.md,
    per_unit_table.csv under eval/runs/<run_id>/."""
    from returns_manager.eval.run import run_eval

    if not dev_mini:
        # A real run needs a sealed set (eval/sealed/) and >= 2 independent labels per
        # unit (eval/labels/) - see seal.labels_before_run_guard - plus real agent
        # results fetched from rm.inspection_results for those exact units. Neither a
        # real sealed set nor real human labels exist in this repository yet
        # (build-log.md OQ-5), and building that loader against data that does not
        # exist cannot be verified, so it is not wired here. Fail clearly rather than
        # silently falling back to a smaller or fake run.
        typer.echo(
            "error: a non-dev-mini `eval run` needs a real sealed set (eval/sealed/) and "
            "real human labels (eval/labels/), neither of which exist in this repository "
            "yet. Use --dev-mini to exercise the tooling, or run `eval seal` first once "
            "real candidate units and labels are available.",
            err=True,
        )
        raise typer.Exit(1)

    units = _build_dev_mini_units()
    manifest = run_eval(
        units,
        run_id=run_id,
        eval_root=EVAL_ROOT,
        dev_mini=True,
        spend_preflight={"confirm_spend": confirm_spend, "note": "dev-mini spends no quota"},
    )
    run_dir = EVAL_ROOT / "runs" / run_id
    typer.echo(f"dev-mini run '{run_id}' complete: {manifest.units_evaluated} unit(s)")
    for name in ("manifest.json", "metrics.json", "report.md", "per_unit_table.csv"):
        typer.echo(f"  {run_dir / name}")


@eval_app.command("report")
def eval_report_command(
    run_id: Annotated[str, typer.Option("--run-id", help="The run to show the report for.")],
    out: Annotated[str, typer.Option("--out", help="Output file; stdout if omitted.")] = "-",
) -> None:
    """Show the report.md already generated by `eval run` for --run-id."""
    report_path = EVAL_ROOT / "runs" / run_id / "report.md"
    if not report_path.exists():
        typer.echo(f"error: no report found for run {run_id!r} at {report_path}", err=True)
        raise typer.Exit(1)
    content = report_path.read_text(encoding="utf-8")
    if out == "-":
        typer.echo(content)
    else:
        Path(out).write_text(content, encoding="utf-8")
        typer.echo(f"report written to {out}")
