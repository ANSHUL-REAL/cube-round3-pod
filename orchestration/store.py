"""Where workflow state and evidence live. Two implementations of one tiny interface.

MemoryStore: tests and library use. FileStore: the CLI and a laptop demo (JSON files under out/). PgStore: a deployment
(Postgres, chosen when DATABASE_URL is set; see shared/utils/db.py). All three implement the same four methods plus
list_workflows. Evidence is IMMUTABLE: a record_id, once written,
can only be written again with identical content.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from shared.utils.ids import SAFE_ID, is_safe_id  # noqa: F401  (workflow and record ids become file names)


class EvidenceConflict(Exception):
    pass


class MemoryStore:
    def __init__(self) -> None:
        self.workflows: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}

    def load_workflow(self, workflow_id: str) -> dict | None:
        wf = self.workflows.get(workflow_id)
        return json.loads(json.dumps(wf)) if wf else None

    def save_workflow(self, wf: dict) -> None:
        self.workflows[wf["workflow_id"]] = json.loads(json.dumps(wf))

    def get_evidence(self, record_id: str) -> dict | None:
        return self.evidence.get(record_id)

    def put_evidence(self, record: dict) -> None:
        existing = self.evidence.get(record["record_id"])
        if existing and existing["content_hash"] != record["content_hash"]:
            raise EvidenceConflict(f"{record['record_id']} already exists with different content; evidence is immutable")
        self.evidence.setdefault(record["record_id"], record)

    def list_workflows(self, orgs: set[str] | None = None) -> list[dict]:
        """Every workflow, or only those of `orgs` (None = all). Newest first is not promised; callers sort."""
        return [json.loads(json.dumps(w)) for w in self.workflows.values() if orgs is None or w["org_id"] in orgs]


class FileStore(MemoryStore):
    def __init__(self, root: str | Path | None = None) -> None:
        super().__init__()
        self.root = Path(root or os.environ.get("OUT_DIR", "out"))
        (self.root / "workflows").mkdir(parents=True, exist_ok=True)
        (self.root / "evidence").mkdir(parents=True, exist_ok=True)

    def load_workflow(self, workflow_id: str) -> dict | None:
        if not is_safe_id(workflow_id):
            return None
        p = self.root / "workflows" / f"{workflow_id}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def save_workflow(self, wf: dict) -> None:
        if not is_safe_id(wf["workflow_id"]):
            raise ValueError(f"unsafe workflow_id {wf['workflow_id']!r}")
        p = self.root / "workflows" / f"{wf['workflow_id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(wf, indent=2))
        tmp.replace(p)  # atomic: a crash never leaves half a workflow

    def get_evidence(self, record_id: str) -> dict | None:
        if not is_safe_id(record_id):
            return None
        p = self.root / "evidence" / f"{record_id}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def put_evidence(self, record: dict) -> None:
        if not is_safe_id(record["record_id"]):
            raise ValueError(f"unsafe record_id {record['record_id']!r}")
        existing = self.get_evidence(record["record_id"])
        if existing and existing["content_hash"] != record["content_hash"]:
            raise EvidenceConflict(f"{record['record_id']} already exists with different content; evidence is immutable")
        if not existing:
            (self.root / "evidence" / f"{record['record_id']}.json").write_text(json.dumps(record, indent=2))

    def list_workflows(self, orgs: set[str] | None = None) -> list[dict]:
        out = []
        for p in sorted((self.root / "workflows").glob("*.json")):
            try:
                wf = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(wf, dict) and "org_id" in wf and (orgs is None or wf["org_id"] in orgs):
                out.append(wf)
        return out


class PgStore:
    """Workflows and evidence in Postgres. Same contract as FileStore: evidence is write-once (the insert does nothing
    when the id exists, and a different content_hash is refused), and every list query names the orgs it may see."""

    def __init__(self) -> None:
        from shared.utils import db

        self.db = db
        db.migrate()

    def load_workflow(self, workflow_id: str) -> dict | None:
        if not is_safe_id(workflow_id):
            return None
        row = self.db.fetchone(f"select body from {self.db.SCHEMA}.workflows where workflow_id=%s", (workflow_id,))
        return row[0] if row else None

    def save_workflow(self, wf: dict) -> None:
        from psycopg.types.json import Jsonb

        if not is_safe_id(wf["workflow_id"]):
            raise ValueError(f"unsafe workflow_id {wf['workflow_id']!r}")
        outcome = (wf.get("final_outcome") or {}).get("outcome")
        self.db.execute(
            f"insert into {self.db.SCHEMA}.workflows (workflow_id, org_id, subject_id, status, outcome, body) "
            f"values (%s,%s,%s,%s,%s,%s) on conflict (workflow_id) do update set status=excluded.status, "
            f"outcome=excluded.outcome, body=excluded.body, updated_at=now()",
            (wf["workflow_id"], wf["org_id"], wf.get("subject_id", ""), wf.get("status"), outcome, Jsonb(wf)))

    def get_evidence(self, record_id: str) -> dict | None:
        if not is_safe_id(record_id):
            return None
        row = self.db.fetchone(f"select body from {self.db.SCHEMA}.evidence where record_id=%s", (record_id,))
        return row[0] if row else None

    def put_evidence(self, record: dict) -> None:
        from psycopg.types.json import Jsonb

        if not is_safe_id(record["record_id"]):
            raise ValueError(f"unsafe record_id {record['record_id']!r}")
        self.db.execute(
            f"insert into {self.db.SCHEMA}.evidence (record_id, org_id, workflow_id, stage, verdict, content_hash, body) "
            f"values (%s,%s,%s,%s,%s,%s,%s) on conflict (record_id) do nothing",
            (record["record_id"], (record.get("subject") or {}).get("org_id", ""), record.get("workflow_id"), record.get("stage"),
             (record.get("decision") or {}).get("verdict"), record["content_hash"], Jsonb(record)))
        row = self.db.fetchone(f"select content_hash from {self.db.SCHEMA}.evidence where record_id=%s", (record["record_id"],))
        if row and row[0] != record["content_hash"]:
            raise EvidenceConflict(f"{record['record_id']} already exists with different content; evidence is immutable")

    def list_workflows(self, orgs: set[str] | None = None) -> list[dict]:
        if orgs is None:
            rows = self.db.fetchall(f"select body from {self.db.SCHEMA}.workflows order by updated_at desc")
        else:
            rows = self.db.fetchall(f"select body from {self.db.SCHEMA}.workflows where org_id = any(%s) "
                                    f"order by updated_at desc", (sorted(orgs),))
        return [r[0] for r in rows]


def default_store():
    """Postgres when DATABASE_URL is set (a deployment), else JSON files under OUT_DIR."""
    from shared.utils import db

    return PgStore() if db.enabled() else FileStore()
