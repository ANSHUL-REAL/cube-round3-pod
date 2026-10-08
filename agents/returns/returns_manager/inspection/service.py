"""The judgment job handler (§3.3, §11).

references → context → model session → deterministic pipeline → persist.

- Missing reference data (§11.2a): no model call; a `skipped` inspection run and an all-UNCERTAIN result are
  stored and the return goes to `awaiting_review` with the reason codes; photos stay intact.
- Model/provider failure: the failed run (requests, usage, paid-equivalent cost, error class) is stored in its
  own transaction and the error is re-raised so the worker fails open (§10.6).
- Success: the run, its tool calls and the result are written by `persist`, which the worker executes in the
  same transaction as the state transition (§11.7 step 12). Thinking content is never stored.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

from returns_manager.canonical.hashing import sha256_hex, sha256_jcs
from returns_manager.chain import event_types as ET
from returns_manager.chain.append import append_event
from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.disposition.engine import DispositionInputs, decide
from returns_manager.disposition.params import load_params, rules_version
from returns_manager.errors import NotFound
from returns_manager.ids import new_id
from returns_manager.intake.quality import load_thresholds
from returns_manager.jobs.queue import JobQueue, JobRecord, compute_job_priority
from returns_manager.jobs.statemachine import ReturnStatus, transition_return_status
from returns_manager.jobs.worker import HandlerResult
from returns_manager.judgment.completeness import compute_completeness
from returns_manager.judgment.consistency import apply_consistency
from returns_manager.judgment.pipeline import Check, PhotoGate, PipelineResult, ReturnTarget, run_pipeline
from returns_manager.judgment.referential import validate_references
from returns_manager.llm.client import ModelClient
from returns_manager.llm.context import (
    ContextBundle,
    MissingReference,
    PhotoSource,
    assemble,
    load_photos,
    load_references,
    reference_bytes,
)
from returns_manager.llm.loop import SessionFailed, SessionTrace, run_session
from returns_manager.llm.prompts import get_prompt
from returns_manager.llm.quota import QuotaGuard
from returns_manager.llm.schemas import SCHEMA_VERSION, JudgmentV1
from returns_manager.review.merge import audit_disagreements, audit_sampled, merge_area
from returns_manager.security import controls


def _json(value: Any) -> str:
    def default(o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        return str(o)

    return json.dumps(value, default=default)


"""Hash method constants for evidence metadata.

RFC 8785 (JCS) is the canonical hashing method for integer-only payloads (reference
data, disposition inputs, event chains).  Model output contains decimal confidence
floats, which JCS rejects, so it uses a deterministic sorted-key JSON serialisation
instead.  These constants are stored alongside every hash so a future verifier knows
exactly how to reproduce it without reading the source code.
"""

OUTPUT_HASH_METHOD = "sha256-sorted-json-v1"
OUTPUT_HASH_DESCRIPTION = (
    "SHA-256 of json.dumps(output, allow_nan=False, ensure_ascii=False, "
    "separators=(',',':'), sort_keys=True).encode('utf-8').  "
    "Deterministic for valid JSON; refuses NaN/Infinity.  "
    "NOT RFC 8785: model output carries decimal floats which JCS rejects."
)
EVIDENCE_HASH_METHOD = "sha256-rfc8785-v1"
EVIDENCE_HASH_DESCRIPTION = (
    "SHA-256 of RFC 8785 JCS canonical bytes.  No floats allowed in the payload "
    "(basis points, minor units, strings only)."
)


def _output_sha256(output: dict[str, Any]) -> str:
    """Hash raw model JSON deterministically, including decimal confidences.

    Method: ``OUTPUT_HASH_METHOD`` (sorted-key JSON, not RFC 8785).
    See the module-level constants for the reproducible specification.
    """
    canonical = json.dumps(
        output,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_hex(canonical)


@dataclasses.dataclass(frozen=True)
class GateFacts:
    gate: PhotoGate
    operator_state: str | None


async def _gate_facts(conn: Any, return_id: str) -> GateFacts:
    cur = await conn.execute(
        "SELECT quality_status, quality FROM rm.return_photos WHERE return_id = %s AND superseded = false",
        (return_id,),
    )
    rows = await cur.fetchall()
    non_fail = sum(1 for r in rows if r["quality_status"] != "fail")
    issues = {i for r in rows for i in ((r["quality"] or {}).get("issues") or [])}
    cur = await conn.execute(
        "SELECT observed_state FROM rm.operator_observations WHERE return_id = %s "
        "ORDER BY recorded_at DESC LIMIT 1",
        (return_id,),
    )
    obs = await cur.fetchone()
    # Submit refuses fewer than 2 non-failing photos unless the operator acknowledged the warnings (§9.4).
    return GateFacts(
        gate=PhotoGate(
            non_fail_photos=non_fail,
            acknowledged_warnings=non_fail < 2,
            integrity_flags=("possible_reused_photo",) if "possible_reused_photo" in issues else (),
        ),
        operator_state=obs["observed_state"] if obs else None,
    )


def inspection_config_version(
    bundle: ContextBundle, settings: Settings, rv: str, *, model_id: str | None = None
) -> str:
    """§10.5: hash of everything that, when changed, makes a new inspection of the same photos meaningful.

    `model_id` is the model that actually served the session (it can differ from
    `settings.rm_judgment_model` when the quota-exhaustion fallback of §10.4a fired); it
    defaults to the configured judgment model for the escalation/audit call sites, which
    do not have a fallback.
    """
    m = bundle.manifest
    return sha256_jcs(
        {
            "prompts": [m["prompt"], m["task_prompt"]],
            "judgment_schema_version": SCHEMA_VERSION,
            "response_schema_sha256": m["response_schema_sha256"],
            "model": model_id or settings.rm_judgment_model,
            "thinking": settings.rm_judgment_thinking,
            "output_mode": settings.rm_output_mode,
            "rules_version": rv,
            "quality_gate_version": load_thresholds().version,
            "policy": m["policy"],
            "rubric": m["rubric"],
            "product_card": m["product_card"],
        }
    )


class JudgmentHandler:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: ModelClient,
        photos: PhotoSource,
        quota: QuotaGuard | None = None,
        eval_run_id: str | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.client = client
        self.photos = photos
        self.quota = quota or QuotaGuard(db, settings)
        self.eval_run_id = eval_run_id

    async def __call__(self, job: JobRecord) -> HandlerResult:
        if job.kind == "escalation":
            return await self.handle_escalation(job)
        if job.kind == "audit":
            return await self.handle_audit(job, eval_run_id=self.eval_run_id)

        eff = await controls.effective(self.db, job.org_id)
        auto = eff[controls.Control.AUTO_DISPOSITION].enabled
        async with self.db.transaction(job.org_id) as conn:
            facts = await _gate_facts(conn, job.return_id)
            # Fetch unit_id for the chain (needed in both success and skipped paths)
            cur = await conn.execute(
                "SELECT unit_id FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (job.org_id, job.return_id),
            )
            ret_row = await cur.fetchone()
            unit_id: str = ret_row["unit_id"] if ret_row else job.return_id
            try:
                ret, card, others, rubric, policy = await load_references(conn, job.org_id, job.return_id)
                refs = reference_bytes(card, job.org_id)
                photos = await load_photos(conn, self.photos, job.return_id)
            except MissingReference as missing:
                return self._skipped(job, facts, missing, auto, unit_id)
        try:
            bundle = assemble(
                settings=self.settings,
                return_row=ret,
                card=card,
                other_cards=others,
                rubric=rubric,
                policy=policy,
                photos=photos,
                refs=refs,
            )
        except MissingReference as missing:
            return self._skipped(job, facts, missing, auto, unit_id)

        inspection_id = new_id()
        started = time.time()
        max_tokens = self.settings.rm_max_output_tokens
        if job.last_error_class == "truncated":  # §10.4: retry once with twice the output budget
            max_tokens = min(max_tokens * 2, 32000)
        try:
            session = await run_session(
                self.client, self.quota, bundle, self.settings, max_output_tokens=max_tokens
            )
        except SessionFailed as failed:
            fallback_model = self.settings.rm_judgment_fallback_model
            can_fall_back = (
                getattr(failed.cause, "error_class", None) == "quota_exhausted"
                and fallback_model
                and fallback_model != self.settings.rm_judgment_model
            )
            if not can_fall_back:
                await self._persist_failed_run(job, inspection_id, bundle, failed.trace, failed.cause)
                raise failed.cause from None
            # §10.4a fallback: the primary model's real daily quota is exhausted before any turn of
            # this session succeeded. Start an entirely fresh session on the fallback model — never a
            # continuation of the failed one, so "never switch models mid-session" still holds. If the
            # fallback also fails, fail open on that failure instead.
            try:
                session = await run_session(
                    self.client,
                    self.quota,
                    bundle,
                    self.settings,
                    model=fallback_model,
                    max_output_tokens=max_tokens,
                )
            except SessionFailed as fallback_failed:
                await self._persist_failed_run(
                    job, inspection_id, bundle, fallback_failed.trace, fallback_failed.cause
                )
                raise fallback_failed.cause from None

        ctx = dataclasses.replace(
            bundle.ctx,
            crop_aliases=session.crop_aliases,
            reference_aliases=bundle.ctx.reference_aliases + session.reference_aliases,
        )
        rv = rules_version(load_params())
        t0 = time.perf_counter()
        result = run_pipeline(
            session.judgment,
            ctx,
            photo_gate=facts.gate,
            rules_version=rv,
            auto_disposition_enabled=auto,
            operator_state=facts.operator_state,
        )
        det_ms = int((time.perf_counter() - t0) * 1000)
        config_version = inspection_config_version(bundle, self.settings, rv, model_id=session.trace.model)
        target = ReturnStatus(result.target_status)
        output_sha = _output_sha256(session.raw_output) if session.raw_output else None

        async def persist(conn: Any) -> None:
            await self._insert_run(
                conn,
                job,
                inspection_id,
                bundle,
                session.trace,
                status="completed",
                started=started,
                output=session.raw_output,
                validation={
                    "actions": [dataclasses.asdict(a) for a in result.report.actions],
                    "invented_reference_count": result.report.invented_reference_count,
                    "invented_quote_count": result.report.invented_quote_count,
                },
                config_version=config_version,
            )
            for rec in session.trace.tools:
                await conn.execute(
                    """INSERT INTO rm.model_tool_calls (tool_call_id, org_id, inspection_id, seq, round_trip,
                         tool_name, input, output_summary, status, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
                    (
                        new_id(),
                        job.org_id,
                        inspection_id,
                        rec.seq,
                        rec.round_trip,
                        rec.tool_name,
                        json.dumps(rec.input),
                        json.dumps(rec.output_summary),
                        rec.status,
                        rec.latency_ms,
                    ),
                )
            await self._insert_result(conn, job, inspection_id, result, session.trace, det_ms, rv)
            # ── P7: chain events (in the same transaction as the DB inserts) ──
            _actor = "system"
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_STARTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={"inspection_id": inspection_id, "job_id": job.job_id, "kind": job.kind},
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_COMPLETED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": inspection_id,
                    "output_sha256": output_sha or "",
                    "api_requests": session.trace.requests_sent,
                    "tool_calls": len(session.trace.tools),
                },
            )
            d = result.decision
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.DISPOSITION_COMPUTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": inspection_id,
                    "inputs_sha256": sha256_jcs(result.inputs.canonical()),
                    "rule_id": d.rule_id if hasattr(d, "rule_id") else "unknown",
                    "rules_version": rv,
                    "recommended_disposition": d.recommended_disposition,
                    "requires_review": result.requires_review,
                },
            )
            # Auto-enqueue escalation if triggers fired and escalation is enabled
            if result.escalation_triggers:
                eff_esc = eff[controls.Control.ESCALATION].enabled
                if eff_esc:
                    await JobQueue.enqueue_job(
                        conn,
                        job.org_id,
                        job.return_id,
                        kind="escalation",
                        priority=compute_job_priority(kind="escalation"),
                    )
            elif self.settings.rm_audit_sample_rate > 0.0:
                if audit_sampled(job.return_id, self.settings.rm_audit_sample_rate):
                    eff_aud = eff[controls.Control.AUDIT].enabled
                    if eff_aud:
                        await JobQueue.enqueue_job(
                            conn,
                            job.org_id,
                            job.return_id,
                            kind="audit",
                            priority=-10,
                        )

        return HandlerResult(
            target_return_status=target, persist=persist, details={"inspection_id": inspection_id}
        )

    async def handle_escalation(self, job: JobRecord) -> HandlerResult:
        eff = await controls.effective(self.db, job.org_id)
        if not eff[controls.Control.ESCALATION].enabled:
            raise Exception("escalation_disabled")
        auto = eff[controls.Control.AUTO_DISPOSITION].enabled

        async with self.db.transaction(job.org_id) as conn:
            facts = await _gate_facts(conn, job.return_id)
            cur = await conn.execute(
                "SELECT unit_id, status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
                (job.org_id, job.return_id),
            )
            ret_row = await cur.fetchone()
            if not ret_row:
                raise NotFound(f"Return '{job.return_id}' not found")
            unit_id: str = ret_row["unit_id"]

            cur = await conn.execute(
                """SELECT inspection_id, model_id FROM rm.inspection_runs
                   WHERE org_id = %s AND return_id = %s AND kind = 'judgment' AND status = 'completed'
                   ORDER BY started_at DESC LIMIT 1""",
                (job.org_id, job.return_id),
            )
            prim_run = await cur.fetchone()
            if not prim_run:
                raise NotFound(f"No completed primary inspection run found for return '{job.return_id}'")
            primary_inspection_id = prim_run["inspection_id"]

            cur = await conn.execute(
                """SELECT * FROM rm.inspection_results
                   WHERE org_id = %s AND return_id = %s AND inspection_id = %s""",
                (job.org_id, job.return_id, primary_inspection_id),
            )
            prim_res = await cur.fetchone()
            if not prim_res:
                raise NotFound(f"No primary inspection result found for inspection '{primary_inspection_id}'")

            # Extract unresolved triggers/reasons without primary verdicts (genuinely blind)
            reasons = list(prim_res.get("review_reasons") or [])
            for u in prim_res.get("uncertainties") or []:
                if isinstance(u, dict) and u.get("area"):
                    reasons.append(str(u["area"]))
            focus = list(dict.fromkeys(reasons)) or ["unresolved_uncertainty"]

            ret, card, others, rubric, policy = await load_references(conn, job.org_id, job.return_id)
            refs = reference_bytes(card, job.org_id)
            photos = await load_photos(conn, self.photos, job.return_id)

        bundle = assemble(
            settings=self.settings,
            return_row=ret,
            card=card,
            other_cards=others,
            rubric=rubric,
            policy=policy,
            photos=photos,
            refs=refs,
            escalation_focus=focus,
        )

        escalation_inspection_id = new_id()
        started = time.time()
        max_tokens = self.settings.rm_max_output_tokens
        if job.last_error_class == "truncated":
            max_tokens = min(max_tokens * 2, 32000)

        try:
            session = await run_session(
                self.client,
                self.quota,
                bundle,
                self.settings,
                role="escalation",
                model=self.settings.rm_escalation_model,
                thinking=self.settings.rm_escalation_thinking,
                crop_budget=6,
                max_output_tokens=max_tokens,
            )
        except SessionFailed as failed:
            await self._persist_failed_run(
                job,
                escalation_inspection_id,
                bundle,
                failed.trace,
                failed.cause,
                model_id=self.settings.rm_escalation_model,
                effort=self.settings.rm_escalation_thinking,
                kind="escalation",
            )
            raise failed.cause from None

        # Build area dicts for strict §11.12 merge
        primary_areas: dict[str, Any] = {
            "unit_presence": prim_res["unit_presence"],
            "identity_match": prim_res["identity_match"],
            "cosmetic_grade": prim_res.get("cosmetic_grade") or "uncertain",
            "model_observed_state": prim_res.get("model_observed_state") or "uncertain",
        }
        prim_comps = {
            c["component_id"]: c["status"]
            for c in (prim_res.get("components") or [])
            if isinstance(c, dict) and "component_id" in c
        }
        for comp in card.components:
            primary_areas[f"component:{comp.id}"] = prim_comps.get(comp.id, "uncertain")

        esc_j = session.judgment
        esc_comps = {c.component_id: c.status for c in esc_j.completeness.components}
        esc_areas: dict[str, Any] = {
            "unit_presence": esc_j.unit_presence.status,
            "identity_match": esc_j.identity.identity_match,
            "cosmetic_grade": esc_j.condition.proposed_grade.grade_code or "uncertain",
            "model_observed_state": esc_j.model_observed_state or "uncertain",
        }
        for comp in card.components:
            esc_areas[f"component:{comp.id}"] = esc_comps.get(comp.id, "uncertain")

        merge_outcomes: dict[str, Any] = {}
        for area, p_val in primary_areas.items():
            e_val = esc_areas[area]
            outcome = merge_area(p_val, e_val)
            merge_outcomes[area] = {
                "primary": p_val,
                "escalation": e_val,
                "value": outcome.value,
                "outcome": outcome.outcome,
                "requires_review": outcome.requires_review,
            }

        # Build merged JudgmentV1
        merged_dict = esc_j.model_dump()
        merged_dict["unit_presence"]["status"] = merge_outcomes["unit_presence"]["value"]
        if merge_outcomes["unit_presence"]["value"] == "uncertain":
            merged_dict["unit_presence"]["confidence"] = 0.0

        merged_dict["identity"]["identity_match"] = merge_outcomes["identity_match"]["value"]
        if merge_outcomes["identity_match"]["value"] == "uncertain":
            merged_dict["identity"]["confidence"] = 0.0
            if merge_outcomes["identity_match"]["outcome"] == "model_disagreement":
                merged_dict["identity"]["reasons"] = ["contradictory_evidence"]

        comp_dict_map = {c["component_id"]: c for c in merged_dict["completeness"]["components"]}
        for comp in card.components:
            area_key = f"component:{comp.id}"
            if comp.id in comp_dict_map and area_key in merge_outcomes:
                c_data = comp_dict_map[comp.id]
                c_out = merge_outcomes[area_key]
                c_data["status"] = c_out["value"]
                if c_out["value"] == "uncertain":
                    c_data["confidence"] = 0.0
                    c_data["reason"] = (
                        "contradictory_evidence"
                        if c_out["outcome"] == "model_disagreement"
                        else "component_area_not_visible"
                    )

        grade_out = merge_outcomes["cosmetic_grade"]
        if grade_out["value"] == "uncertain":
            merged_dict["condition"]["proposed_grade"]["grade_code"] = None
            merged_dict["condition"]["proposed_grade"]["confidence"] = 0.0
        else:
            merged_dict["condition"]["proposed_grade"]["grade_code"] = grade_out["value"]

        merged_dict["model_observed_state"] = merge_outcomes["model_observed_state"]["value"]
        merged_judgment = JudgmentV1.model_validate(merged_dict)

        ctx = dataclasses.replace(
            bundle.ctx,
            crop_aliases=session.crop_aliases,
            reference_aliases=bundle.ctx.reference_aliases + session.reference_aliases,
        )
        rv = rules_version(load_params())
        t0 = time.perf_counter()
        result = run_pipeline(
            merged_judgment,
            ctx,
            photo_gate=facts.gate,
            rules_version=rv,
            auto_disposition_enabled=auto,
            operator_state=facts.operator_state,
        )
        det_ms = int((time.perf_counter() - t0) * 1000)
        config_version = inspection_config_version(bundle, self.settings, rv)
        output_sha = _output_sha256(session.raw_output) if session.raw_output else None

        has_disagreement = any(m["requires_review"] for m in merge_outcomes.values())
        final_review_reasons = list(result.review_reasons)
        if has_disagreement and "model_disagreement" not in final_review_reasons:
            final_review_reasons.append("model_disagreement")
        final_requires_review = result.requires_review or has_disagreement
        target_return_status: ReturnStatus
        target_target: ReturnTarget
        if final_requires_review:
            target_return_status = ReturnStatus.AWAITING_REVIEW
            target_target = "awaiting_review"
        elif result.decision.requires_signoff:
            target_return_status = ReturnStatus.AWAITING_SIGNOFF
            target_target = "awaiting_signoff"
        else:
            target_return_status = ReturnStatus.AWAITING_OPERATOR
            target_target = "awaiting_operator"

        async def persist(conn: Any) -> None:
            await self._insert_run(
                conn,
                job,
                escalation_inspection_id,
                bundle,
                session.trace,
                status="completed",
                started=started,
                output=session.raw_output,
                validation={
                    "actions": [dataclasses.asdict(a) for a in result.report.actions],
                    "invented_reference_count": result.report.invented_reference_count,
                    "invented_quote_count": result.report.invented_quote_count,
                },
                config_version=config_version,
                model_id=self.settings.rm_escalation_model,
                effort=self.settings.rm_escalation_thinking,
                kind="escalation",
            )
            for rec in session.trace.tools:
                await conn.execute(
                    """INSERT INTO rm.model_tool_calls (tool_call_id, org_id, inspection_id, seq, round_trip,
                         tool_name, input, output_summary, status, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
                    (
                        new_id(),
                        job.org_id,
                        escalation_inspection_id,
                        rec.seq,
                        rec.round_trip,
                        rec.tool_name,
                        json.dumps(rec.input),
                        json.dumps(rec.output_summary),
                        rec.status,
                        rec.latency_ms,
                    ),
                )
            for area, info in merge_outcomes.items():
                await conn.execute(
                    """INSERT INTO rm.escalation_merges (
                         merge_id, org_id, return_id, primary_inspection_id, escalation_inspection_id,
                         area, primary_value, escalation_value, merged_value, outcome
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)""",
                    (
                        new_id(),
                        job.org_id,
                        job.return_id,
                        primary_inspection_id,
                        escalation_inspection_id,
                        area,
                        json.dumps(info["primary"]),
                        json.dumps(info["escalation"]),
                        json.dumps(info["value"]),
                        info["outcome"],
                    ),
                )
            updated_r = dataclasses.replace(
                result,
                requires_review=final_requires_review,
                review_reasons=tuple(dict.fromkeys(final_review_reasons)),
                target_status=target_target,
            )
            await self._insert_result(
                conn,
                job,
                escalation_inspection_id,
                updated_r,
                session.trace,
                det_ms,
                rv,
                escalation_state="completed",
                model_id=self.settings.rm_escalation_model,
            )
            _actor = "system"
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_STARTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": escalation_inspection_id,
                    "job_id": job.job_id,
                    "kind": "escalation",
                },
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_COMPLETED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": escalation_inspection_id,
                    "output_sha256": output_sha or "",
                    "api_requests": session.trace.requests_sent,
                    "tool_calls": len(session.trace.tools),
                },
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.ESCALATION_COMPLETED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "primary_inspection_id": primary_inspection_id,
                    "escalation_inspection_id": escalation_inspection_id,
                    "areas": {
                        a: {"value": m["value"], "outcome": m["outcome"]} for a, m in merge_outcomes.items()
                    },
                },
            )
            d = updated_r.decision
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.DISPOSITION_COMPUTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": escalation_inspection_id,
                    "inputs_sha256": sha256_jcs(updated_r.inputs.canonical()),
                    "rule_id": d.rule_id if hasattr(d, "rule_id") else "unknown",
                    "rules_version": rv,
                    "recommended_disposition": d.recommended_disposition,
                    "requires_review": final_requires_review,
                },
            )

        return HandlerResult(
            target_return_status=target_return_status,
            persist=persist,
            details={
                "inspection_id": escalation_inspection_id,
                "primary_inspection_id": primary_inspection_id,
                "outcomes": {a: m["outcome"] for a, m in merge_outcomes.items()},
            },
        )

    async def handle_audit(self, job: JobRecord, eval_run_id: str | None = None) -> HandlerResult:
        eff = await controls.effective(self.db, job.org_id)
        if not eff[controls.Control.AUDIT].enabled:
            raise Exception("audit_disabled")

        async with self.db.transaction(job.org_id) as conn:
            cur = await conn.execute(
                "SELECT unit_id, status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
                (job.org_id, job.return_id),
            )
            ret_row = await cur.fetchone()
            if not ret_row:
                raise NotFound(f"Return '{job.return_id}' not found")
            unit_id: str = ret_row["unit_id"]
            current_status = ret_row["status"]

            cur = await conn.execute(
                """SELECT inspection_id, model_id FROM rm.inspection_runs
                   WHERE org_id = %s AND return_id = %s AND kind = 'judgment' AND status = 'completed'
                   ORDER BY started_at DESC LIMIT 1""",
                (job.org_id, job.return_id),
            )
            prim_run = await cur.fetchone()
            if not prim_run:
                raise NotFound(f"No completed primary inspection run found for return '{job.return_id}'")
            primary_inspection_id = prim_run["inspection_id"]

            cur = await conn.execute(
                """SELECT * FROM rm.inspection_results
                   WHERE org_id = %s AND return_id = %s AND inspection_id = %s""",
                (job.org_id, job.return_id, primary_inspection_id),
            )
            prim_res = await cur.fetchone()
            if not prim_res:
                raise NotFound(f"No primary inspection result found for inspection '{primary_inspection_id}'")

            ret, card, others, rubric, policy = await load_references(conn, job.org_id, job.return_id)
            refs = reference_bytes(card, job.org_id)
            photos = await load_photos(conn, self.photos, job.return_id)

        # Assemble blind bundle with NO primary verdicts!
        bundle = assemble(
            settings=self.settings,
            return_row=ret,
            card=card,
            other_cards=others,
            rubric=rubric,
            policy=policy,
            photos=photos,
            refs=refs,
            escalation_focus=None,
        )

        audit_inspection_id = new_id()
        started = time.time()
        max_tokens = self.settings.rm_max_output_tokens
        if job.last_error_class == "truncated":
            max_tokens = min(max_tokens * 2, 32000)

        try:
            session = await run_session(
                self.client,
                self.quota,
                bundle,
                self.settings,
                role="audit",
                model=self.settings.rm_audit_model,
                thinking=self.settings.rm_audit_thinking,
                crop_budget=self.settings.rm_max_crops_per_session,
                max_output_tokens=max_tokens,
            )
        except SessionFailed as failed:
            await self._persist_failed_run(
                job,
                audit_inspection_id,
                bundle,
                failed.trace,
                failed.cause,
                model_id=self.settings.rm_audit_model,
                effort=self.settings.rm_audit_thinking,
                kind="audit",
            )
            raise failed.cause from None

        prim_dict = {
            "identity_match": prim_res["identity_match"],
            "completeness_status": prim_res["completeness_status"],
            "unit_presence": prim_res["unit_presence"],
            "cosmetic_grade": prim_res.get("cosmetic_grade"),
        }
        audit_j = session.judgment
        aud_valid_j, aud_rep = validate_references(audit_j, bundle.ctx)
        aud_valid_j = apply_consistency(aud_valid_j, bundle.ctx, aud_rep)
        aud_comp = compute_completeness(aud_valid_j, bundle.ctx, aud_rep)
        audit_dict = {
            "identity_match": audit_j.identity.identity_match,
            "completeness_status": aud_comp.status,
            "unit_presence": audit_j.unit_presence.status,
            "cosmetic_grade": audit_j.condition.proposed_grade.grade_code,
        }

        disagreements = audit_disagreements(prim_dict, audit_dict)
        output_sha = _output_sha256(session.raw_output) if session.raw_output else None

        target_status = ReturnStatus(current_status)
        if disagreements and current_status != str(ReturnStatus.FINALIZED):
            target_status = ReturnStatus.AWAITING_REVIEW

        async def persist(conn: Any) -> None:
            await self._insert_run(
                conn,
                job,
                audit_inspection_id,
                bundle,
                session.trace,
                status="completed",
                started=started,
                output=session.raw_output,
                model_id=self.settings.rm_audit_model,
                effort=self.settings.rm_audit_thinking,
                kind="audit",
            )
            for rec in session.trace.tools:
                await conn.execute(
                    """INSERT INTO rm.model_tool_calls (tool_call_id, org_id, inspection_id, seq, round_trip,
                         tool_name, input, output_summary, status, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
                    (
                        new_id(),
                        job.org_id,
                        audit_inspection_id,
                        rec.seq,
                        rec.round_trip,
                        rec.tool_name,
                        json.dumps(rec.input),
                        json.dumps(rec.output_summary),
                        rec.status,
                        rec.latency_ms,
                    ),
                )
            await conn.execute(
                """INSERT INTO rm.audit_findings (
                     finding_id, org_id, return_id, primary_inspection_id, audit_inspection_id,
                     disagreements, routes_to_review, eval_run_id
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    new_id(),
                    job.org_id,
                    job.return_id,
                    primary_inspection_id,
                    audit_inspection_id,
                    list(disagreements),
                    bool(disagreements),
                    eval_run_id,
                ),
            )
            _actor = "system"
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_STARTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={"inspection_id": audit_inspection_id, "job_id": job.job_id, "kind": "audit"},
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_COMPLETED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": audit_inspection_id,
                    "output_sha256": output_sha or "",
                    "api_requests": session.trace.requests_sent,
                    "tool_calls": len(session.trace.tools),
                },
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=unit_id,
                return_id=job.return_id,
                event_type=ET.AUDIT_COMPLETED,
                actor_type=_actor,
                actor_id="audit-reviewer",
                payload={
                    "primary_inspection_id": primary_inspection_id,
                    "audit_inspection_id": audit_inspection_id,
                    "disagreements": list(disagreements),
                    "eval_run_id": eval_run_id,
                },
            )
            terminal = (str(ReturnStatus.FINALIZED), str(ReturnStatus.AWAITING_REVIEW))
            if disagreements and current_status not in terminal:
                transition_return_status(current_status, ReturnStatus.AWAITING_REVIEW, "audit_disagreement")
                await conn.execute(
                    "UPDATE rm.returns SET status = %s WHERE org_id = %s AND return_id = %s",
                    (str(ReturnStatus.AWAITING_REVIEW), job.org_id, job.return_id),
                )

        return HandlerResult(
            target_return_status=target_status,
            persist=persist,
            details={
                "inspection_id": audit_inspection_id,
                "primary_inspection_id": primary_inspection_id,
                "disagreements": list(disagreements),
            },
        )

    # ── persistence ────────────────────────────────────────────────────────
    def _model_version(self, extra: str = "", model_id: str | None = None) -> str:
        p = get_prompt("judgment")
        m = model_id or self.settings.rm_judgment_model
        return f"{m}@{p.ref}{extra}"

    def _checks_json(
        self,
        checks: tuple[Check, ...],
        model_latency_ms: int,
        det_ms: int,
        rv: str,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        qg = f"deterministic@quality-gate-{load_thresholds().version}"
        out = []
        for c in checks:
            if c.source == "model":
                version, latency = self._model_version(model_id=model_id), model_latency_ms
            else:
                version = qg if c.check_key == "photo_quality" else f"deterministic@{rv}"
                latency = det_ms
            out.append(
                {
                    "check_key": c.check_key,
                    "verdict": c.verdict,
                    "confidence_bp": c.confidence_bp,
                    "detail": c.detail,
                    "model_version": version,
                    "latency_ms": latency,
                }
            )
        return out

    async def _insert_run(
        self,
        conn: Any,
        job: JobRecord,
        inspection_id: str,
        bundle: ContextBundle | None,
        trace: SessionTrace | None,
        *,
        status: str,
        started: float,
        output: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        config_version: str | None = None,
        skip_reasons: list[str] | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        model_id: str | None = None,
        effort: str | None = None,
        kind: str | None = None,
    ) -> None:
        prompt = get_prompt("judgment")
        manifest = dict(bundle.manifest) if bundle else {}
        if config_version:
            manifest["inspection_config_version"] = config_version
        # §13.1 / evidence hash clarity: record hash methods so verifiers can reproduce.
        manifest["hash_methods"] = {
            "output_sha256": OUTPUT_HASH_METHOD,
            "output_sha256_spec": OUTPUT_HASH_DESCRIPTION,
            "evidence_sha256": EVIDENCE_HASH_METHOD,
            "evidence_sha256_spec": EVIDENCE_HASH_DESCRIPTION,
        }
        actual_model_id = model_id or (trace.model if trace else self.settings.rm_judgment_model)
        actual_effort = effort or (self.settings.rm_judgment_thinking if trace else None)
        await conn.execute(
            """INSERT INTO rm.inspection_runs (inspection_id, org_id, return_id, job_id, kind, model_id,
                 effort,
                 output_mode, prompt_id, prompt_version, prompt_sha256, judgment_schema_version,
                 context_manifest,
                 started_at, completed_at, status, skip_reasons, api_requests, tool_calls, usage,
                 cost_usd_micros,
                 latency_ms, provider_interaction_ids, finish_reasons, error_class, error_detail, output,
                 output_sha256, validation_report)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, to_timestamp(%s),
               now(), %s, %s,
                 %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)""",
            (
                inspection_id,
                job.org_id,
                job.return_id,
                job.job_id,
                kind or job.kind,
                actual_model_id,
                actual_effort,
                self.settings.rm_output_mode if trace else None,
                prompt.prompt_id,
                prompt.version,
                prompt.sha256,
                SCHEMA_VERSION,
                _json(manifest),
                started,
                status,
                skip_reasons,
                trace.requests_sent if trace else 0,
                len(trace.tools) if trace else 0,
                _json(trace.usage if trace else {}),
                trace.cost_usd_micros if trace else None,
                trace.latency_ms if trace else None,
                trace.interaction_ids if trace else [],
                trace.statuses if trace else [],
                error_class,
                error_detail,
                _json(output) if output is not None else None,
                _output_sha256(output) if output is not None else None,
                _json(validation) if validation is not None else None,
            ),
        )

    async def _persist_failed_run(
        self,
        job: JobRecord,
        inspection_id: str,
        bundle: ContextBundle,
        trace: SessionTrace,
        cause: Exception,
        model_id: str | None = None,
        effort: str | None = None,
        kind: str | None = None,
    ) -> None:
        from returns_manager.jobs.retry import classify_error

        c = classify_error(cause)
        async with self.db.transaction(job.org_id) as conn:
            await self._insert_run(
                conn,
                job,
                inspection_id,
                bundle,
                trace,
                status="failed",
                started=time.time(),
                error_class=c.error_class,
                error_detail=c.detail[:500],
                model_id=model_id,
                effort=effort,
                kind=kind,
            )

    async def _insert_result(
        self,
        conn: Any,
        job: JobRecord,
        inspection_id: str,
        r: PipelineResult,
        trace: SessionTrace,
        det_ms: int,
        rv: str,
        escalation_state: str | None = None,
        model_id: str | None = None,
    ) -> None:
        d = r.decision
        await conn.execute(
            """INSERT INTO rm.inspection_results (result_id, org_id, return_id, inspection_id, identity_match,
                 fused_identity, unit_presence, completeness_status, components, cosmetic_grade,
                 amazon_condition,
                 listing_blockers, condition, model_observed_state, claim_signals, uncertainties,
                 retake_requests,
                 validator_actions, checks, escalation_state, recommended_disposition,
                 no_recommendation_reason,
                 provisional, relistable_as_is, disposition, disposition_inputs, requires_review,
                 review_reasons,
                 requires_signoff)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                 %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                 %s, %s, %s)""",
            (
                new_id(),
                job.org_id,
                job.return_id,
                inspection_id,
                r.identity.identity_match,
                _json(r.identity),
                r.presence.status,
                r.completeness.status,
                _json(r.completeness),
                r.condition.cosmetic_grade,
                r.condition.amazon_condition,
                list(r.condition.listing_blockers),
                _json(r.condition),
                r.judgment.model_observed_state,
                _json(r.claims),
                _json([u.model_dump() for u in r.judgment.uncertainties]),
                _json([x.model_dump() for x in r.judgment.retake_requests]),
                _json([dataclasses.asdict(a) for a in r.report.actions]),
                _json(self._checks_json(r.checks, trace.latency_ms, det_ms, rv, model_id=model_id)),
                escalation_state or ("triggered_not_run" if r.escalation_triggers else "not_triggered"),
                d.recommended_disposition,
                d.no_recommendation_reason,
                d.provisional,
                r.condition.relistable_as_is,
                _json({**dataclasses.asdict(d), "escalation_triggers": list(r.escalation_triggers)}),
                _json(r.inputs.canonical()),
                r.requires_review,
                list(r.review_reasons),
                d.requires_signoff,
            ),
        )

    def _skipped(
        self,
        job: JobRecord,
        facts: GateFacts,
        missing: MissingReference,
        auto: bool,
        unit_id: str | None = None,
    ) -> HandlerResult:
        """§11.2a: no model call; awaiting_review with the reason codes; every model check UNCERTAIN."""
        inspection_id = new_id()
        started = time.time()
        rv = rules_version(load_params())
        reason = missing.reasons[0]
        inputs = DispositionInputs(
            inspection_state="skipped",
            skip_reason=reason,
            usable_photo_count=facts.gate.non_fail_photos,
            unit_presence="uncertain",
            identity="uncertain",
            actual_sku=None,
            completeness_status="uncertain",
            essential_missing=(),
            nonessential_missing=(),
            essential_uncertain=(),
            nonessential_uncertain=(),
            cosmetic_grade=None,
            listing_blockers=(),
            blockers_undetermined=(),
            max_severity="none",
            new_only=False,
            opened_item_route="liquidate",
            damaged_item_route="dispose",
            list_price_minor=0,
            currency="INR",
            recovery_rate_bp=(),
            refurbish_cost_minor=0,
            restock_used_grades=(),
            refurbish_min_net_gain_minor=0,
            dispose_max_salvage_minor=0,
            high_value_threshold_minor=0,
            auto_disposition_enabled=auto,
        )
        decision = decide(inputs, rv)
        why = f"Inspection skipped ({', '.join(missing.reasons)}): missing {'; '.join(missing.missing)}."[
            :400
        ]
        gate = facts.gate
        checks = [
            {
                "check_key": "photo_quality",
                "verdict": "PASS" if gate.non_fail_photos >= 2 else "UNCERTAIN",
                "confidence_bp": 10000,
                "detail": f"{gate.non_fail_photos} photos passed the quality gate.",
                "model_version": f"deterministic@quality-gate-{load_thresholds().version}",
                "latency_ms": 0,
            },
            *[
                {
                    "check_key": k,
                    "verdict": "UNCERTAIN",
                    "confidence_bp": 0,
                    "detail": why,
                    "model_version": "not_run",
                    "latency_ms": 0,
                }
                for k in (
                    "unit_presence",
                    "identity",
                    "completeness",
                    "condition_grade",
                    "relistable_as_is",
                    "category_policy",
                )
            ],
        ]

        _unit_id: str = unit_id if unit_id is not None else job.return_id

        async def persist(conn: Any) -> None:
            await self._insert_run(
                conn,
                job,
                inspection_id,
                None,
                None,
                status="skipped",
                started=started,
                skip_reasons=missing.reasons,
            )
            await conn.execute(
                """INSERT INTO rm.inspection_results (result_id, org_id, return_id, inspection_id,
                     identity_match,
                     fused_identity, unit_presence, completeness_status, components, amazon_condition,
                     condition,
                     claim_signals, uncertainties, retake_requests, validator_actions, checks,
                     escalation_state,
                     no_recommendation_reason, provisional, disposition, disposition_inputs, requires_review,
                     review_reasons, requires_signoff)
                   VALUES (%s, %s, %s, %s, 'uncertain', %s::jsonb, 'uncertain', 'uncertain', '[]'::jsonb,
                     'uncertain', %s::jsonb, %s::jsonb, %s::jsonb, '[]'::jsonb, '[]'::jsonb, %s::jsonb,
                     'not_triggered', %s, false, %s::jsonb, %s::jsonb, true, %s, false)""",
                (
                    new_id(),
                    job.org_id,
                    job.return_id,
                    inspection_id,
                    _json({"skipped": missing.reasons}),
                    _json({"skipped": missing.reasons}),
                    _json(
                        {
                            "item_not_returned": "uncertain",
                            "wrong_item_returned": "uncertain",
                            "returned_damaged": "uncertain",
                        }
                    ),
                    _json([{"area": "identity", "reason": "reference_insufficient", "detail": why[:200]}]),
                    _json(checks),
                    decision.no_recommendation_reason,
                    _json(dataclasses.asdict(decision)),
                    _json(inputs.canonical()),
                    list(dict.fromkeys([*missing.reasons, *decision.review_reasons])),
                ),
            )
            # ── P7: chain events (in the same transaction) ──────────────────
            _actor = "system"
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=_unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_STARTED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={"inspection_id": inspection_id, "job_id": job.job_id, "kind": job.kind},
            )
            await append_event(
                conn,
                org_id=job.org_id,
                unit_id=_unit_id,
                return_id=job.return_id,
                event_type=ET.INSPECTION_SKIPPED,
                actor_type=_actor,
                actor_id=f"worker/{job.job_id}",
                payload={
                    "inspection_id": inspection_id,
                    "skip_reasons": list(missing.reasons),
                    "missing": list(missing.missing),
                },
            )

        return HandlerResult(
            target_return_status=ReturnStatus.AWAITING_REVIEW,
            persist=persist,
            details={"inspection_id": inspection_id, "skipped": missing.reasons, "missing": missing.missing},
        )
