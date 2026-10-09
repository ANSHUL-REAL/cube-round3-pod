"""The product's name and the names people see, in one place. Change BRAND_NAME (and BRAND_TAGLINE) and every page
follows: the front page, the console, the phone stations, the tab titles.

Sellers (orgs) keep their ids everywhere data is stored or checked (`org_demo_alpha`); pages show a display name.
An admin adds or renames sellers on /admin; those names live in the database when there is one (every worker sees
them), else in this process.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

NAME = os.environ.get("BRAND_NAME") or "Snitch"
TAGLINE = os.environ.get("BRAND_TAGLINE") or "It tells on every box, with receipts."
CREDIT = os.environ.get("BRAND_CREDIT") or "Built by Pod 12"

_DEFAULT_ORGS = {"org_demo_alpha": "Alpha Retail", "org_demo_bravo": "Bravo Supply"}
_MEM: dict[str, str] = {}  # sellers added while running without a database
_LOCK = threading.Lock()
_CACHE: list = [0.0, None]  # [read at (monotonic), {org id: name}]
_TTL_S = 15.0  # another worker's change shows within this long; this worker's own change at once


def _stored() -> dict[str, str]:
    """Sellers added or renamed on the admin page: org id -> name. A failed read shows the default names, never an
    error page."""
    from shared.utils import db

    if not db.enabled():
        with _LOCK:
            return dict(_MEM)
    if _CACHE[1] is not None and time.monotonic() - _CACHE[0] < _TTL_S:
        return _CACHE[1]
    try:
        names = dict(db.fetchall(f"select org_id, name from {db.SCHEMA}.sellers"))
    except Exception:
        names = {}
    _CACHE[0], _CACHE[1] = time.monotonic(), names
    return names


def org_names() -> dict[str, str]:
    """org id -> display name. ORG_NAMES='{"org_x": "X Ltd"}' adds or replaces names; the admin page's sellers win."""
    names = dict(_DEFAULT_ORGS)
    try:
        names.update(json.loads(os.environ.get("ORG_NAMES") or "{}"))
    except ValueError:
        pass
    names.update(_stored())
    return names


def org_name(org_id: str | None) -> str:
    if not org_id:
        return ""
    return org_names().get(org_id, org_id)


def clean_name(name: str | None) -> str:
    """A seller's name as typed, tidied: single spaces, 2 to 60 characters. Raises ValueError otherwise."""
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 60:
        raise ValueError("A seller's name is 2 to 60 characters.")
    return name


def new_org_id(name: str, taken: set[str]) -> str:
    """A stable id for a new seller, from its name: 'Gamma Goods' -> 'org_gamma_goods' (then _2, _3 if taken)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "seller"
    org_id, n = f"org_{slug}", 2
    while org_id in taken:
        org_id, n = f"org_{slug}_{n}", n + 1
    return org_id


def save_seller(org_id: str, name: str, actor: str) -> None:
    """Add a seller, or rename one (a built-in seller too). The id never changes: data stays attached to it."""
    from shared.utils import db

    if db.enabled():
        db.execute(f"insert into {db.SCHEMA}.sellers (org_id, name, created_by) values (%s,%s,%s) on conflict (org_id) "
                   f"do update set name=excluded.name, updated_at=now()", (org_id, name, actor))
        _CACHE[1] = None
    else:
        with _LOCK:
            _MEM[org_id] = name


def context() -> dict:
    return {"brand": {"name": NAME, "tagline": TAGLINE, "credit": CREDIT}}
