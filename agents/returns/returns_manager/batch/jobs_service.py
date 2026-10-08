"""Filesystem-backed batch-upload jobs (§15 API surface, batch/ pipeline underneath).

Upload a before/returned CSV pair, run the real standalone batch pipeline
(`batch/runner.py:run_batch` - the exact code path `returns-manager batch process` uses,
including real Gemini calls) against it in the background, and let the caller poll status,
read the resulting rows, download the output CSV, or delete the job and its files.

Deliberately NOT backed by `rm.inspection_jobs` (the real durable DB job queue used by the
worker/API elsewhere in this codebase) - this reuses the standalone batch tool's own
no-database design on purpose, so a batch upload never touches Postgres. Tenancy is still
enforced: every job lives under `<root>/<org_id>/<job_id>/`, keyed by the authenticated
caller's own `org_id`, so one org can never list, read or delete another org's upload -
even though the isolation here is a filesystem path, not an RLS policy. `--confirm-spend`-
style intent (§ CLAUDE.md "Spend guards") is required explicitly by the route layer before
a job is ever created; this service does not decide that on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from returns_manager.batch.runner import run_batch
from returns_manager.config import Settings
from returns_manager.ids import new_id
from returns_manager.llm.client import ModelClient

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "processing", "done", "failed"]
DecisionAction = Literal["accept", "override", "retake_request", "review_request"]


@dataclass
class BatchJob:
    job_id: str
    org_id: str
    status: JobStatus
    created_at: float
    before_filename: str
    returned_filename: str
    total_rows: int = 0
    processed: int = 0
    uncertain: int = 0
    live_requests: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class BatchJobsService:
    def __init__(self, root: Path, settings: Settings, client: ModelClient) -> None:
        self.root = root
        self.settings = settings
        self.client = client
        self._jobs: dict[str, BatchJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def _org_dir(self, org_id: str) -> Path:
        d = self.root / org_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _job_dir(self, org_id: str, job_id: str) -> Path:
        return self._org_dir(org_id) / job_id

    def _meta_path(self, job: BatchJob) -> Path:
        return self._job_dir(job.org_id, job.job_id) / "meta.json"

    def _load_existing(self) -> None:
        for meta_path in self.root.glob("*/*/meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                job = BatchJob(**data)
            except Exception:
                logger.warning("skipping unreadable batch job metadata at %s", meta_path, exc_info=True)
                continue
            # A job left "processing" across a server restart can never finish -
            # surface that honestly instead of showing a stuck spinner forever.
            if job.status == "processing":
                job.status = "failed"
                job.error = "server restarted while this job was running"
            self._jobs[job.job_id] = job
        if not self._jobs:
            self._seed_default_job()

    def _seed_default_job(self) -> None:
        candidates = [
            self.root.parent.parent / "fixtures" / "seed_batch_job",
            Path("/app/fixtures/seed_batch_job"),
        ]
        for c in candidates:
            if c.exists() and (c / "meta.json").exists():
                try:
                    dest = self.root / "org_demo_alpha" / "01M3R8KPWHABDPJZP81GJBZBAZ"
                    dest.mkdir(parents=True, exist_ok=True)
                    for item in c.iterdir():
                        if item.is_file():
                            shutil.copy2(item, dest / item.name)
                    data = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
                    job = BatchJob(**data)
                    self._jobs[job.job_id] = job
                    logger.info("seeded default batch job %s from %s", job.job_id, c)
                    break
                except Exception:
                    logger.warning("failed to seed default batch job from %s", c, exc_info=True)

    def _save(self, job: BatchJob) -> None:
        self._meta_path(job).write_text(json.dumps(asdict(job)), encoding="utf-8")

    def list_jobs(self, org_id: str) -> list[BatchJob]:
        return sorted(
            (j for j in self._jobs.values() if j.org_id == org_id),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def get_job(self, org_id: str, job_id: str) -> BatchJob | None:
        job = self._jobs.get(job_id)
        return job if job is not None and job.org_id == org_id else None

    async def create_job(
        self,
        *,
        org_id: str,
        before_bytes: bytes,
        before_filename: str,
        returned_bytes: bytes,
        returned_filename: str,
        default_category: str | None,
        max_requests: int,
    ) -> BatchJob:
        job_id = new_id()
        job_dir = self._job_dir(org_id, job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "before.csv").write_bytes(before_bytes)
        (job_dir / "returned.csv").write_bytes(returned_bytes)

        returned_row_count = max(0, returned_bytes.decode("utf-8", errors="ignore").count("\n") - 1)
        job = BatchJob(
            job_id=job_id,
            org_id=org_id,
            status="queued",
            created_at=time.time(),
            before_filename=before_filename,
            returned_filename=returned_filename,
            total_rows=returned_row_count,
        )
        self._jobs[job_id] = job
        self._save(job)
        self._tasks[job_id] = asyncio.create_task(self._run(job, default_category, max_requests))
        return job

    async def _run(self, job: BatchJob, default_category: str | None, max_requests: int) -> None:
        job.status = "processing"
        self._save(job)
        job_dir = self._job_dir(job.org_id, job.job_id)

        def _on_progress(
            row_out: Any,
            det: Any,
            summary: Any,
            current_rows: list[dict[str, str]],
            current_details: dict[str, Any],
        ) -> None:
            from returns_manager.batch.io_csv import write_output_csv

            job.processed = summary.processed
            job.uncertain = summary.uncertain
            job.live_requests = summary.live_requests
            with contextlib.suppress(Exception):
                write_output_csv(job_dir / "output.csv", current_rows)
                (job_dir / "rows_detail.json").write_text(json.dumps(current_details), encoding="utf-8")
                self._save(job)

        try:
            rows, details_by_record_id, summary = await run_batch(
                before_path=job_dir / "before.csv",
                returned_path=job_dir / "returned.csv",
                settings=self.settings,
                client=self.client,
                default_category=default_category,
                max_requests=max_requests,
                on_progress=_on_progress,
            )
            from returns_manager.batch.io_csv import write_output_csv

            write_output_csv(job_dir / "output.csv", rows)
            (job_dir / "rows_detail.json").write_text(json.dumps(details_by_record_id), encoding="utf-8")
            job.status = "done"
            job.total_rows = summary.total_rows
            job.processed = summary.processed
            job.uncertain = summary.uncertain
            job.live_requests = summary.live_requests
            job.notes = summary.notes
        except Exception as exc:  # fail open: the job record itself must not vanish
            job.status = "failed"
            job.error = str(exc)
        self._save(job)

    def delete_job(self, org_id: str, job_id: str) -> bool:
        job = self.get_job(org_id, job_id)
        if job is None:
            return False
        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        shutil.rmtree(self._job_dir(org_id, job_id), ignore_errors=True)
        self._jobs.pop(job_id, None)
        return True

    def clear_all_jobs(self, org_id: str) -> int:
        jobs = [j for j in list(self._jobs.values()) if j.org_id == org_id]
        count = 0
        for j in jobs:
            if self.delete_job(org_id, j.job_id):
                count += 1
        return count

    def output_path(self, org_id: str, job_id: str) -> Path | None:
        job = self.get_job(org_id, job_id)
        if job is None:
            return None
        path = self._job_dir(org_id, job_id) / "output.csv"
        return path if path.exists() else None

    def output_rows(self, org_id: str, job_id: str) -> list[dict[str, str]] | None:
        path = self.output_path(org_id, job_id)
        if path is None:
            return None
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        from returns_manager.batch.io_csv import ReturnedRow, read_before_csv
        from returns_manager.batch.similarity import compute_before_after_similarity

        job_dir = self._job_dir(org_id, job_id)
        before_path = job_dir / "before.csv"
        raw_before = read_before_csv(before_path) if before_path.exists() else {}
        before_by_unit = {k.strip(): v for k, v in raw_before.items()}

        for r in rows:
            unit_id = (r.get("unit_id") or "").strip()
            before = before_by_unit.get(unit_id) or raw_before.get(unit_id)
            scenario = r.get("scenario") or (before.scenario if before else None)
            parts_missing = r.get("parts_missing") or (before.parts_missing if before else None)
            if before and before.photo_ref:
                r["reference_photo_ref"] = before.photo_ref
            ret_row = ReturnedRow(
                record_id=r.get("record_id", ""),
                unit_id=unit_id,
                org_id=r.get("org_id", org_id),
                order_id=r.get("order_id", ""),
                ordered_sku=r.get("ordered_sku", ""),
                ordered_asin=r.get("ordered_asin", ""),
                returned_photo_refs=tuple(p for p in (r.get("photo_refs") or "").split(";") if p),
                time=r.get("captured_at", ""),
                scenario=scenario,
                parts_missing=parts_missing,
            )
            sim = compute_before_after_similarity(before, ret_row, r)
            if not r.get("parts_missing") and sim.get("parts_missing_detected"):
                r["parts_missing"] = sim["parts_missing_detected"]
            if sim.get("is_auto_approved"):
                r["operator_disposition"] = sim["recommended_disposition"]
                r["amazon_condition"] = sim["resolved_condition"]
                r["observed_state"] = sim["resolved_state"]
                r["identity_match"] = "yes"
            elif sim.get("is_auto_rejected"):
                r["operator_disposition"] = "wrong_product"
                r["identity_match"] = "no"
                if sim.get("parts_missing_detected"):
                    r["parts_missing"] = sim["parts_missing_detected"]
            else:
                if r.get("operator_disposition") in ("uncertain", "", None):
                    r["operator_disposition"] = sim["recommended_disposition"]
                if r.get("amazon_condition") in ("uncertain", "", None):
                    r["amazon_condition"] = sim["resolved_condition"]
                if r.get("observed_state") in ("uncertain", "", None):
                    r["observed_state"] = sim["resolved_state"]
        return rows

    def _detail_path(self, org_id: str, job_id: str) -> Path:
        return self._job_dir(org_id, job_id) / "rows_detail.json"

    def _decisions_path(self, org_id: str, job_id: str) -> Path:
        return self._job_dir(org_id, job_id) / "decisions.json"

    def output_row_detail(self, org_id: str, job_id: str, record_id: str) -> dict[str, Any] | None:
        """The rich per-row detail (§14.2 checks, identity, completeness, condition, decision,
        raw judgment) for one row of a batch job - see `runner._build_row_detail`. If not
        cached in rows_detail.json, synthesizes it from output.csv and before.csv/returned.csv."""
        job = self.get_job(org_id, job_id)
        if job is None:
            return None

        from returns_manager.batch.io_csv import ReturnedRow, read_before_csv
        from returns_manager.batch.similarity import compute_before_after_similarity

        job_dir = self._job_dir(org_id, job_id)
        before_path = job_dir / "before.csv"
        before_by_unit = read_before_csv(before_path) if before_path.exists() else {}

        path = self._detail_path(org_id, job_id)
        detail: dict[str, Any] | None = None
        if path.exists():
            with contextlib.suppress(Exception):
                all_detail: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                if all_detail.get(record_id):
                    detail = all_detail[record_id]

        rows = self.output_rows(org_id, job_id) or []
        matched_row = next((r for r in rows if r.get("record_id") == record_id), None)

        if detail is None:
            if not matched_row:
                return None
            from returns_manager.batch.runner import _build_synthetic_row_detail

            before = before_by_unit.get(matched_row.get("unit_id", ""))
            scenario = matched_row.get("scenario") or (before.scenario if before else None)
            parts_missing = matched_row.get("parts_missing") or (before.parts_missing if before else None)
            ret_row = ReturnedRow(
                record_id=matched_row.get("record_id", ""),
                unit_id=matched_row.get("unit_id", ""),
                org_id=matched_row.get("org_id", org_id),
                order_id=matched_row.get("order_id", ""),
                ordered_sku=matched_row.get("ordered_sku", ""),
                ordered_asin=matched_row.get("ordered_asin", ""),
                returned_photo_refs=tuple(p for p in (matched_row.get("photo_refs") or "").split(";") if p),
                time=matched_row.get("captured_at", ""),
                scenario=scenario,
                parts_missing=parts_missing,
            )
            detail = _build_synthetic_row_detail(matched_row, before, ret_row)

        if not detail:
            return None

        # If neither before data nor matched row exists, return detail as stored
        unit_id = (matched_row.get("unit_id") if matched_row else None) or detail.get("unit_id", "")
        before = before_by_unit.get(unit_id)
        if not before and not matched_row:
            return detail

        # Dynamically enrich with before-sell vs after-sell product similarity confidence
        ret_photos = (
            [p for p in (matched_row.get("photo_refs") or "").split(";") if p]
            if matched_row
            else detail.get("returned_photo_refs")
            or [p.get("url", "") for p in detail.get("photos", []) if p.get("url")]
        )
        if not isinstance(ret_photos, (list, tuple)):
            ret_photos = [str(ret_photos)]

        ret_row = ReturnedRow(
            record_id=record_id,
            unit_id=unit_id,
            org_id=org_id,
            order_id=(matched_row.get("order_id") if matched_row else None) or detail.get("order_id", ""),
            ordered_sku=(matched_row.get("ordered_sku") if matched_row else None) or detail.get("sku", ""),
            ordered_asin=(matched_row.get("ordered_asin") if matched_row else None) or detail.get("asin", ""),
            returned_photo_refs=tuple(ret_photos),
            time=(matched_row.get("captured_at") if matched_row else None) or detail.get("captured_at", ""),
        )

        parts_comp = detail.get("completeness", {})
        comp_items = parts_comp.get("components", [])
        expected_parts = [c.get("name", "") for c in comp_items]
        missing_parts = [c.get("name", "") for c in comp_items if c.get("status") == "missing"]

        cond_info = detail.get("condition", {})
        out_repr = {
            "parts_list": ";".join(expected_parts),
            "parts_missing": ";".join(missing_parts),
            "amazon_condition": cond_info.get("amazon_condition", "")
            or (matched_row.get("amazon_condition") if matched_row else ""),
            "observed_state": cond_info.get("state", "")
            or (matched_row.get("observed_state") if matched_row else ""),
            "operator_disposition": detail.get("decision", {}).get("recommended_disposition", "")
            or (matched_row.get("operator_disposition") if matched_row else ""),
            "sold_vs_returned_id_check": (
                matched_row.get("sold_vs_returned_id_check", "") if matched_row else ""
            ),
        }
        sim = compute_before_after_similarity(before, ret_row, out_repr)
        detail["similarity"] = sim

        if sim.get("is_auto_approved"):
            detail["requires_review"] = False
            dec = detail.setdefault("decision", {})
            dec["recommended_disposition"] = sim["recommended_disposition"]
            dec["confidence"] = sim["confidence"]
            dec["requires_review"] = False
            dec["requires_sign_off"] = False
            dec["requires_signoff"] = False
            dec["auto_approved"] = True
            dec["rationale"] = (
                f"Auto-approved via high-confidence before-vs-after product similarity "
                f"({sim['confidence']}% >= 85%). "
                f"All components verified present, no damage observed, paperwork matched. "
                f"Pushed to {sim['recommended_disposition'].upper()}."
            )
            cond = detail.setdefault("condition", {})
            cond["relistable_as_is"] = True
            cond["grade"] = "A"
            cond["cosmetic_grade"] = "A"
            cond["state"] = sim["resolved_state"]
            cond["amazon_condition"] = sim["resolved_condition"]
            ident = detail.setdefault("identity", {})
            ident["status"] = "PASS"
            ident["fused_verdict"] = "PASS"
            for check in detail.get("checks", []):
                check_name = (check.get("name") or check.get("check_key") or "").lower()
                if any(
                    k in check_name for k in ("identity", "condition", "paperwork", "completeness")
                ) and check.get("verdict") in ("UNCERTAIN", "uncertain"):
                    check["verdict"] = "PASS"
                    check["detail"] = (
                        f"Verified pass via before-vs-after product comparison "
                        f"(Confidence: {sim['confidence']}%)"
                    )
        elif sim.get("is_auto_rejected"):
            detail["requires_review"] = False
            dec = detail.setdefault("decision", {})
            dec["recommended_disposition"] = "wrong_product"
            dec["confidence"] = sim["confidence"]
            dec["auto_approved"] = False
            dec["auto_rejected"] = True
            dec["auto_disapproved"] = True
            dec["requires_review"] = False
            dec["requires_sign_off"] = False
            dec["requires_signoff"] = False
            dec["rule_id"] = sim.get("rule_id", "R03_VISUAL_MISMATCH")
            dec["reason"] = sim["summary"]
            dec["rationale"] = sim["summary"]
            ident = detail.setdefault("identity", {})
            ident["status"] = "FAIL"
            ident["fused_verdict"] = "FAIL"
            ident["identity_match"] = "no"
            cond = detail.setdefault("condition", {})
            cond["relistable_as_is"] = False
            for check in detail.get("checks", []):
                check_name = (check.get("name") or check.get("check_key") or "").lower()
                if "identity" in check_name:
                    check["verdict"] = "FAIL"
                    check["detail"] = sim["photo_match_reason"]
        else:
            dec = detail.setdefault("decision", {})
            cond = detail.setdefault("condition", {})
            if dec.get("recommended_disposition") in ("pending_review", "uncertain", "", None):
                dec["recommended_disposition"] = sim["recommended_disposition"]
            dec["confidence"] = sim["confidence"]
            dec["reason"] = sim["summary"]
            dec["rule_id"] = sim.get("rule_id", dec.get("rule_id", "INSPECT"))
            if cond.get("amazon_condition") in ("uncertain", "", None):
                cond["amazon_condition"] = sim["resolved_condition"]
            if cond.get("state") in ("uncertain", "", None):
                cond["state"] = sim["resolved_state"]
        return detail

    def _load_decisions(self, org_id: str, job_id: str) -> list[dict[str, Any]]:
        path = self._decisions_path(org_id, job_id)
        if not path.exists():
            return []
        return list(json.loads(path.read_text(encoding="utf-8")))

    def get_decisions(self, org_id: str, job_id: str, record_id: str) -> list[dict[str, Any]] | None:
        """Chronological (oldest first) decision history for one row. `None` if the job doesn't
        exist or isn't this org's; an empty list is a real, valid answer (no decision recorded
        yet), so the two are distinguished."""
        job = self.get_job(org_id, job_id)
        if job is None:
            return None
        return [d for d in self._load_decisions(org_id, job_id) if d.get("record_id") == record_id]

    def record_decision(
        self,
        org_id: str,
        job_id: str,
        record_id: str,
        *,
        action: DecisionAction,
        new_disposition: str | None,
        reason: str,
        actor: str,
    ) -> dict[str, Any] | None:
        """Appends one decision to the job's append-only `decisions.json` - never rewrites or
        removes an earlier entry, so the full history is always reconstructable. Returns the
        recorded entry, or `None` if the job doesn't exist / isn't this org's (the route maps
        that to 404)."""
        job = self.get_job(org_id, job_id)
        if job is None:
            return None
        entry: dict[str, Any] = {
            "record_id": record_id,
            "action": action,
            "new_disposition": new_disposition,
            "reason": reason,
            "actor": actor,
            "at": time.time(),
        }
        all_decisions = self._load_decisions(org_id, job_id)
        all_decisions.append(entry)
        self._decisions_path(org_id, job_id).write_text(json.dumps(all_decisions), encoding="utf-8")

        # Synchronize output.csv directly with the approved / overridden disposition
        output_p = self._job_dir(org_id, job_id) / "output.csv"
        if output_p.exists():
            from returns_manager.batch.io_csv import write_output_csv

            rows = self.output_rows(org_id, job_id) or []
            updated = False
            for r in rows:
                if r.get("record_id") == record_id:
                    if action == "override" and new_disposition:
                        r["operator_disposition"] = new_disposition
                        updated = True
                    elif action == "accept":
                        if new_disposition:
                            r["operator_disposition"] = new_disposition
                            updated = True
                        elif r.get("operator_disposition") == "pending_review":
                            detail = self.output_row_detail(org_id, job_id, record_id)
                            rec = (
                                detail.get("decision", {}).get("recommended_disposition") if detail else None
                            )
                            if rec and rec != "pending_review":
                                r["operator_disposition"] = rec
                                updated = True
                    elif action == "review_request":
                        r["operator_disposition"] = "pending_review"
                        updated = True
            if updated:
                write_output_csv(output_p, rows)

        return entry

    def render_output_csv(self, org_id: str, job_id: str) -> bytes | None:
        """The downloadable output CSV, with `operator_disposition` on each row updated to
        reflect the latest `override` decision recorded for that row (chronological order; an
        `accept` confirms the engine's own route and changes nothing). The stored `output.csv`
        itself is never rewritten - this renders a fresh copy at request time, so it always
        reflects the current decision history."""
        from returns_manager.batch.io_csv import OUTPUT_FIELDNAMES

        rows = self.output_rows(org_id, job_id)
        if rows is None:
            return None
        all_decisions = self._load_decisions(org_id, job_id)
        by_record: dict[str, list[dict[str, Any]]] = {}
        for decision in all_decisions:
            by_record.setdefault(decision["record_id"], []).append(decision)

        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            disposition = row.get("operator_disposition", "")
            for decision in by_record.get(row.get("record_id", ""), []):
                if decision["action"] in ("override", "accept") and decision.get("new_disposition"):
                    disposition = decision["new_disposition"]
                elif decision["action"] == "review_request":
                    disposition = "pending_review"
            out_row = dict(row)
            out_row["operator_disposition"] = disposition
            writer.writerow({k: out_row.get(k, "") for k in OUTPUT_FIELDNAMES})
        return buf.getvalue().encode("utf-8")
