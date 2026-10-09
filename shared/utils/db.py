"""Postgres: the durable home of a deployed pod. Turned on by DATABASE_URL; without it everything stays in files.

A cloud host's disk is wiped on every restart and deploy, so in a deployment the database is the only thing that lasts:

  pod12.workflows      one row per workflow (the full Workflow State as JSONB, plus org/status columns to query)
  pod12.evidence       one row per evidence record, write-once (a second write must carry the same content_hash)
  pod12.files          every file the agents read from disk: capture photos, Returns reference photos and cards, the
                       Pack reuse ledger. Written here on every change and copied back to disk when the server starts,
                       so the agents keep reading plain files and none of their code changes.
  pod12.audit          who did what: every sign-in, every change, every admin action
  pod12.access_codes   access codes the admin issues (only their SHA-256 is stored), each scoped to one org or to all
  pod12.faults         agents an admin has switched off (or made misbehave) to show failure handling live

The tables live in their own schema, not `public`: Supabase publishes `public` through its REST API, and nothing here
should be reachable except through this app. Every query on tenant data names the org it is for.
"""
from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from shared.utils.log import get_logger

log = get_logger("db")
SCHEMA = os.environ.get("DB_SCHEMA", "pod12")  # tests use their own schema
_POOL = None
_LOCK = threading.Lock()

def ddl() -> str:
    return f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.workflows (
    workflow_id text primary key,
    org_id      text not null,
    subject_id  text not null,
    status      text,
    outcome     text,
    body        jsonb not null,
    updated_at  timestamptz not null default now()
);
create index if not exists workflows_org_status on {SCHEMA}.workflows (org_id, status);
create table if not exists {SCHEMA}.evidence (
    record_id    text primary key,
    org_id       text not null,
    workflow_id  text,
    stage        text,
    verdict      text,
    content_hash text not null,
    body         jsonb not null,
    created_at   timestamptz not null default now()
);
create index if not exists evidence_org_workflow on {SCHEMA}.evidence (org_id, workflow_id);
create table if not exists {SCHEMA}.files (
    area       text not null,
    path       text not null,
    org_id     text,
    sha256     text not null,
    size       integer not null,
    mtime      double precision not null,
    data       bytea not null,
    updated_at timestamptz not null default now(),
    primary key (area, path)
);
create table if not exists {SCHEMA}.audit (
    id      bigserial primary key,
    at      timestamptz not null default now(),
    actor   text not null,
    role    text not null,
    org_id  text,
    action  text not null,
    target  text,
    status  integer,
    detail  jsonb
);
create index if not exists audit_at on {SCHEMA}.audit (at desc);
create table if not exists {SCHEMA}.access_codes (
    id          bigserial primary key,
    label       text not null,
    org_id      text,
    role        text not null,
    code_sha256 text not null unique,
    created_by  text not null,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz
);
alter table {SCHEMA}.access_codes add column if not exists stage text;
create table if not exists {SCHEMA}.faults (
    stage  text primary key,
    mode   text not null,
    set_by text not null,
    set_at timestamptz not null default now()
);
create table if not exists {SCHEMA}.sellers (
    org_id     text primary key,
    name       text not null,
    created_by text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists {SCHEMA}.labels (
    record_id  text not null,
    check_key  text not null,
    labeller   text not null,
    org_id     text not null,
    label      text not null,
    notes      text,
    at         timestamptz not null default now(),
    primary key (record_id, check_key, labeller)
);
create table if not exists {SCHEMA}.units (
    unit_id    text primary key,
    org_id     text not null,
    data       jsonb not null,
    created_by text not null,
    created_at timestamptz not null default now()
);
"""


def url() -> str | None:
    """DATABASE_URL, checked before anything uses it: a value pasted into the wrong box (an API key, say) must stop the
    server with a plain message, never reach the driver, whose errors would print a piece of it into the logs."""
    value = (os.environ.get("DATABASE_URL") or "").strip()
    if not value:
        return None
    if not value.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL is set but is not a postgresql:// connection string (was another value pasted "
                           "into it?). Its value is not printed. Use Supabase > Connect > Session pooler.")
    if "[YOUR-PASSWORD]" in value.upper():
        raise RuntimeError("DATABASE_URL still contains [YOUR-PASSWORD]: put the database password in its place.")
    return value


def enabled() -> bool:
    return bool((os.environ.get("DATABASE_URL") or "").strip())


def pool():
    """One small pool per process. `prepare_threshold=None`: Supabase's pooler (and PgBouncer) cannot keep prepared
    statements between transactions."""
    global _POOL
    with _LOCK:
        if _POOL is None:
            from psycopg_pool import ConnectionPool

            _POOL = ConnectionPool(url(), min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "5")), open=True,
                                   kwargs={"autocommit": True, "prepare_threshold": None}, timeout=15)
        return _POOL


def close() -> None:
    global _POOL
    with _LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


def execute(sql: str, params: tuple | dict = ()) -> None:
    with pool().connection() as conn:
        conn.execute(sql, params)


def fetchall(sql: str, params: tuple | dict = ()) -> list[tuple]:
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def fetchone(sql: str, params: tuple | dict = ()) -> tuple | None:
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def migrate() -> None:
    with pool().connection() as conn:
        conn.execute(ddl())
    log.info("database ready", extra={"ctx": {"schema": SCHEMA}})


def ping() -> dict:
    """For /health and the admin page: is the database reachable, and how much is in it. Never the address."""
    if not enabled():
        return {"status": "off", "detail": "no DATABASE_URL: state is kept in files on this machine"}
    try:
        rows = fetchall(f"select 'workflows', count(*) from {SCHEMA}.workflows union all "
                        f"select 'evidence', count(*) from {SCHEMA}.evidence union all "
                        f"select 'files', count(*) from {SCHEMA}.files union all "
                        f"select 'audit', count(*) from {SCHEMA}.audit union all "
                        f"select 'units', count(*) from {SCHEMA}.units")
        return {"status": "ok", "rows": {k: n for k, n in rows}}
    except Exception as exc:  # the page must say the database is down, not crash
        return {"status": "down", "detail": type(exc).__name__}


# ---------------------------------------------------------------- files the agents read from disk
# area -> the folder it is copied back into. Paths in the table are relative to that folder, with "/" separators.
def area_root(area: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    if area == "input":
        return Path(os.environ.get("INPUT_DIR", root / "data" / "input")).resolve()
    if area == "reference":
        return (root / "agents" / "returns" / "reference").resolve()
    if area == "catalogue":
        return (root / "agents" / "pack" / "catalogue").resolve()
    if area == "ledger":
        return Path(os.environ.get("PACK_LEDGER_PATH", root / "out" / "pack-ledger.json")).resolve().parent
    raise ValueError(f"unknown file area {area!r}")


def _rel(area: str, path: Path) -> str:
    return path.resolve().relative_to(area_root(area)).as_posix()


def save_file(area: str, path: Path, org_id: str | None = None) -> None:
    """Copy one file that was just written on disk into the database (insert or replace)."""
    if not enabled():
        return
    data = path.read_bytes()
    execute(f"insert into {SCHEMA}.files (area, path, org_id, sha256, size, mtime, data) values (%s,%s,%s,%s,%s,%s,%s) "
            f"on conflict (area, path) do update set org_id=excluded.org_id, sha256=excluded.sha256, size=excluded.size, "
            f"mtime=excluded.mtime, data=excluded.data, updated_at=now()",
            (area, _rel(area, path), org_id, hashlib.sha256(data).hexdigest(), len(data), path.stat().st_mtime, data))


def sync_folder(area: str, folder: Path, org_id: str | None = None) -> None:
    """Make the database hold exactly the files now in `folder` (and below): new and changed ones are saved, deleted
    ones removed. Used after uploads, retakes, deletions and reference onboarding."""
    if not enabled():
        return
    prefix = _rel(area, folder) if folder.exists() else folder.resolve().relative_to(area_root(area)).as_posix()
    here = {_rel(area, p): p for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".")} if folder.exists() else {}
    known = {r[0]: r[1] for r in fetchall(f"select path, sha256 from {SCHEMA}.files where area=%s and (path=%s or path like %s)",
                                          (area, prefix, prefix.rstrip("/") + "/%"))}
    for rel in set(known) - set(here):
        execute(f"delete from {SCHEMA}.files where area=%s and path=%s", (area, rel))
    for rel, p in here.items():
        if known.get(rel) != hashlib.sha256(p.read_bytes()).hexdigest():
            save_file(area, p, org_id)


def restore_files() -> dict[str, int]:
    """On start: write every stored file back to disk where it is missing or different, keeping its original
    modification time (Prep uses it as the capture time when an order gives none)."""
    if not enabled():
        return {}
    counts: dict[str, int] = {}
    for area, rel, sha, mtime in fetchall(f"select area, path, sha256, mtime from {SCHEMA}.files order by area, path"):
        root = area_root(area)
        target = (root / rel).resolve()
        if root not in target.parents:  # a stored path must never write outside its folder
            log.warning("stored file path refused", extra={"ctx": {"area": area, "path": rel}})
            continue
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            continue
        data = fetchone(f"select data from {SCHEMA}.files where area=%s and path=%s", (area, rel))[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
        os.utime(target, (mtime, mtime))
        counts[area] = counts.get(area, 0) + 1
    log.info("files restored from database", extra={"ctx": counts})
    return counts


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
