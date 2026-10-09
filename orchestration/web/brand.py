"""The product's name and the names people see, in one place. Change BRAND_NAME (and BRAND_TAGLINE) and every page
follows: the front page, the console, the phone stations, the tab titles.

Customers (orgs) keep their ids everywhere data is stored or checked (`org_demo_alpha`); pages show a display name.
"""
from __future__ import annotations

import json
import os

NAME = os.environ.get("BRAND_NAME") or "Snitch"
TAGLINE = os.environ.get("BRAND_TAGLINE") or "It tells on every box, with receipts."
CREDIT = os.environ.get("BRAND_CREDIT") or "Built by Pod 12"

_DEFAULT_ORGS = {"org_demo_alpha": "Alpha Retail", "org_demo_bravo": "Bravo Supply"}


def org_names() -> dict[str, str]:
    """org id -> display name. ORG_NAMES='{"org_x": "X Ltd"}' adds or replaces names."""
    names = dict(_DEFAULT_ORGS)
    try:
        names.update(json.loads(os.environ.get("ORG_NAMES") or "{}"))
    except ValueError:
        pass
    return names


def org_name(org_id: str | None) -> str:
    if not org_id:
        return ""
    return org_names().get(org_id, org_id)


def context() -> dict:
    return {"brand": {"name": NAME, "tagline": TAGLINE, "credit": CREDIT}}
