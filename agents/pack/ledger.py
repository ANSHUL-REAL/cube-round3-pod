"""Which orders has each photo already been used for? (the photo_reuse check)

Keyed by (org_id, sha256 of the prepared photo), so one organisation can never see another's photos.
Persisted to a small JSON file (``PACK_LEDGER_PATH``, default ``out/pack-ledger.json``) so a restart does not
forget earlier uses. Writes are atomic (temp file, then rename). If the file cannot be read or written the
ledger carries on in memory and says so in the log: a lost ledger weakens the reuse check, it must not stop packing.

The same photo for the same order is a re-check, not reuse (see core.decision.reuse_check).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from shared.utils.log import get_logger

log = get_logger("pack.ledger")
ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()
_STATE: dict[str, dict[str, list[dict]]] = {}  # path -> {"org|sha": [uses]}


def _path() -> Path:
    return Path(os.environ.get("PACK_LEDGER_PATH", ROOT / "out" / "pack-ledger.json"))


def _key(org_id: str, sha: str) -> str:
    return f"{org_id}|{sha}"


def _load(path: Path) -> dict[str, list[dict]]:
    cached = _STATE.get(str(path))
    if cached is not None:
        return cached
    data: dict[str, list[dict]] = {}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("reuse ledger unreadable, starting empty", extra={"ctx": {"path": str(path), "error": str(exc)}})
    _STATE[str(path)] = data
    return data


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, path)
            from shared.utils import db

            if db.enabled():  # survives a deployment's restart; copied back to disk on start
                db.save_file("ledger", path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:  # disk or database: the ledger weakens the reuse check, it must never stop packing
        log.warning("reuse ledger not saved, kept in memory only", extra={"ctx": {"path": str(path), "error": str(exc)}})


def earlier_uses(org_id: str, hashes: list[str]) -> list[dict]:
    with _LOCK:
        data = _load(_path())
        return [u for h in hashes for u in data.get(_key(org_id, h), [])]


def remember(org_id: str, hashes: list[str], order_id: str, record_id: str) -> None:
    with _LOCK:
        path = _path()
        data = _load(path)
        changed = False
        for h in hashes:
            uses = data.setdefault(_key(org_id, h), [])
            if not any(u["order_id"] == order_id and u["record_id"] == record_id for u in uses):
                uses.append({"order_id": order_id, "record_id": record_id})
                changed = True
        if changed:
            _save(path, data)


def clear() -> None:
    """Forget everything, in memory and on disk (tests)."""
    with _LOCK:
        path = _path()
        _STATE.pop(str(path), None)
        path.unlink(missing_ok=True)
