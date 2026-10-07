"""Reading ALL previous evidence, with the latest human override applied.

Recovery never rewrites what earlier stages said. It reads each record, works out the verdict that is *effective*
(the latest workflow override of that record wins, `shared.utils.stubs.effective_verdict`), and cites the record
ids it relied on. The original verdict is kept next to the effective one so a reviewer can see what was overridden.
"""
from __future__ import annotations

from dataclasses import dataclass

from shared.utils.hashing import verify
from shared.utils.stubs import effective_verdict

STAGES = ("receiving", "prep", "pack", "returns")
JOIN_KEYS = ("sku", "fnsku", "fba_shipment_id", "order_id")


@dataclass
class Upstream:
    record: dict
    verdict: str            # effective: after the latest override
    original: str           # what the record's own decision said
    override: dict | None   # the latest override that applies, or None

    @property
    def record_id(self) -> str:
        return self.record["record_id"]

    @property
    def stage(self) -> str:
        return self.record["stage"]

    @property
    def completed(self) -> bool:
        return self.record.get("status") == "completed"

    @property
    def overridden(self) -> bool:
        return self.override is not None and self.verdict != self.original

    @property
    def checks(self) -> list[dict]:
        return self.record.get("checks", [])

    @property
    def refs(self) -> dict:
        return (self.record.get("subject") or {}).get("refs") or {}

    def check(self, key: str) -> dict | None:
        return next((c for c in self.checks if c["check_key"] == key), None)

    def summary(self) -> dict:
        r = self.record
        return {"record_id": self.record_id, "stage": self.stage, "agent_id": r.get("agent_id"),
                "model": (r.get("model") or {}).get("name"), "status": r.get("status"),
                "unit_scope": (r.get("subject") or {}).get("unit_scope"), "verdict": self.original,
                "effective_verdict": self.verdict, "overridden": self.overridden,
                "override_id": (self.override or {}).get("override_id"),
                "outcome": (r.get("decision") or {}).get("outcome")}


class Evidence:
    """Everything the earlier stages left for this subject."""

    def __init__(self, request: dict):
        subject = request["subject"]
        self.by_stage: dict[str, list[Upstream]] = {}
        self.ignored: list[dict] = []
        self.all_ids: list[str] = []
        overrides = (request.get("context") or {}).get("overrides") or []
        for rec in request.get("previous_evidence") or []:
            who = rec.get("subject") or {}
            if who.get("org_id") != subject["org_id"]:
                # Another tenant's evidence must never reach a decision. The orchestrator never sends it, so this is
                # a bug or an attack: refuse the whole request, do not quietly drop it.
                raise LookupError(f"previous evidence {rec.get('record_id')} belongs to another organisation")
            if who.get("subject_id") != subject["subject_id"]:
                self.ignored.append({"record_id": rec["record_id"], "reason": "about a different subject"})
                continue
            if not verify(rec):
                # A claim must not rest on a record whose body no longer matches its own hash.
                self.ignored.append({"record_id": rec["record_id"], "reason": "content_hash does not match the record body"})
                continue
            self.all_ids.append(rec["record_id"])
            mine = [o for o in overrides if (o.get("supersedes") or {}).get("record_id") == rec["record_id"]]
            up = Upstream(rec, effective_verdict(request, rec), rec["decision"]["verdict"], mine[-1] if mine else None)
            self.by_stage.setdefault(rec["stage"], []).append(up)

    def latest(self, stage: str) -> Upstream | None:
        """The latest record of a stage (list order is stage order). A later failed attempt is not skipped over:
        if the latest record is not a judgment, there is no judgment."""
        found = self.by_stage.get(stage)
        return found[-1] if found else None

    def every(self) -> list[Upstream]:
        return [u for stage in STAGES for u in self.by_stage.get(stage, [])]

    def overrides_applied(self) -> list[dict]:
        return [{"override_id": u.override.get("override_id"), "record_id": u.record_id, "stage": u.stage,
                 "previous_verdict": u.override.get("previous_verdict"), "new_verdict": u.override.get("new_verdict"),
                 "actor": u.override.get("actor"), "at": u.override.get("at"), "reason": u.override.get("reason")}
                for u in self.every() if u.override]


def ref_conflicts(line_refs: dict, up: Upstream) -> list[str]:
    """Join keys that both the fee line and the record carry and that disagree. Empty means "no sign of a different
    item", not "proven the same": a record that carries none of these keys is joined on the unit id alone (F-08)."""
    return [k for k in JOIN_KEYS if line_refs.get(k) and up.refs.get(k) and str(line_refs[k]) != str(up.refs[k])]
