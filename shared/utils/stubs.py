"""Shared helpers for the organiser stub agents.

The stubs replay the synthetic Round 2 CSV rows as contract-compliant Evidence Records so the whole system
runs end to end before any pod has plugged in a real agent.

THEY ARE NOT AGENTS. They do no vision, no model call, no rule lookup. The stub logic is deliberately simple and
mapped from the CSV columns. Replace it. Anything you ship must say what it actually is in `model.name`.
"""
from __future__ import annotations

STUB_MODEL = {"name": "csv-replay-stub", "version": "0", "provider": None, "calls": 0, "cost_usd": 0}


def verdict_from(value: str, ok: set[str], bad: set[str]) -> str:
    if value in ok:
        return "PASS"
    if value in bad:
        return "FAIL"
    return "UNCERTAIN"


def photos(row: dict) -> list[dict]:
    return [{"ref": p, "sha256": None, "kind": "image"} for p in row.get("photo_refs", "").split(";") if p]


def require_same_subject(request: dict) -> None:
    """Refuse a request that carries evidence about another organisation or subject (LookupError -> 404).

    Call it first in `handle()`, before any "no photo" or other early return, so the refusal never depends on the order
    in which an agent happens to look at its input.
    """
    me = request["subject"]
    for r in request.get("previous_evidence", []):
        s = r.get("subject") or {}
        if (s.get("org_id"), s.get("subject_id")) != (me["org_id"], me["subject_id"]):
            raise LookupError(f"previous evidence {r.get('record_id')} belongs to another organisation or subject")


def previous(request: dict, stage: str) -> dict | None:
    """The latest earlier evidence record for a stage, or None.

    Tenancy: a record about another organisation or another subject is never used. It raises LookupError, which the
    server turns into a 404 and the in-process client into a refusal, exactly as for an unknown unit.
    """
    require_same_subject(request)
    found = [r for r in request.get("previous_evidence", []) if r["stage"] == stage]
    return found[-1] if found else None


def effective_verdict(request: dict, record: dict) -> str:
    """A record's verdict after workflow-level overrides (latest wins). Downstream agents should use this."""
    latest = [o for o in request.get("context", {}).get("overrides", [])
              if o["supersedes"]["record_id"] == record["record_id"]]
    return latest[-1]["new_verdict"] if latest else record["decision"]["verdict"]
