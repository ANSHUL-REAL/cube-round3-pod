"""Reads the two seller-supplied input files and joins them on `unit_id`.

Before-file columns: record_id,unit_id,org_id,order_id,ordered_sku,ordered_asin,
identity_match,parts_list,time,photo_ref[,category]
Returned-file columns: record_id,unit_id,org_id,order_id,ordered_sku,ordered_asin,
returned_photo_ref,time
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REQUIRED_BEFORE_COLUMNS = {
    "record_id",
    "unit_id",
    "org_id",
    "order_id",
    "ordered_sku",
    "ordered_asin",
    "identity_match",
    "parts_list",
    "time",
    "photo_ref",
}
REQUIRED_RETURNED_COLUMNS = {
    "record_id",
    "unit_id",
    "org_id",
    "order_id",
    "ordered_sku",
    "ordered_asin",
    "returned_photo_ref",
    "time",
}


@dataclass(frozen=True)
class BeforeRow:
    record_id: str
    unit_id: str
    org_id: str
    order_id: str
    ordered_sku: str
    ordered_asin: str
    identity_match: str
    parts_list: str
    time: str
    photo_ref: str
    category: str | None = None
    scenario: str | None = None
    parts_missing: str | None = None


@dataclass(frozen=True)
class ReturnedRow:
    record_id: str
    unit_id: str
    org_id: str
    order_id: str
    ordered_sku: str
    ordered_asin: str
    returned_photo_refs: tuple[str, ...]
    time: str
    scenario: str | None = None
    parts_missing: str | None = None


def _check_columns(fieldnames: Sequence[str] | None, required: set[str], path: Path) -> None:
    have = set(fieldnames or [])
    missing = required - have
    if missing:
        raise ValueError(f"{path}: missing required column(s): {', '.join(sorted(missing))}")


def read_before_csv(path: Path) -> dict[str, BeforeRow]:
    """Returns {unit_id: BeforeRow}. Later rows for the same unit_id overwrite earlier ones."""
    by_unit: dict[str, BeforeRow] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_columns(reader.fieldnames, REQUIRED_BEFORE_COLUMNS, path)
        for row in reader:
            unit_id = (row.get("unit_id") or "").strip()
            if not unit_id:
                continue
            category = (row.get("category") or "").strip() or None
            scenario = (row.get("scenario") or "").strip() or None
            parts_missing = (row.get("parts_missing") or "").strip() or None
            by_unit[unit_id] = BeforeRow(
                record_id=(row.get("record_id") or "").strip(),
                unit_id=unit_id,
                org_id=(row.get("org_id") or "").strip(),
                order_id=(row.get("order_id") or "").strip(),
                ordered_sku=(row.get("ordered_sku") or "").strip(),
                ordered_asin=(row.get("ordered_asin") or "").strip(),
                identity_match=(row.get("identity_match") or "uncertain").strip(),
                parts_list=(row.get("parts_list") or "").strip(),
                time=(row.get("time") or "").strip(),
                photo_ref=(row.get("photo_ref") or "").strip(),
                category=category.lower() if category else None,
                scenario=scenario,
                parts_missing=parts_missing,
            )
    return by_unit


def read_returned_csv(path: Path) -> list[ReturnedRow]:
    rows: list[ReturnedRow] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_columns(reader.fieldnames, REQUIRED_RETURNED_COLUMNS, path)
        for row in reader:
            unit_id = (row.get("unit_id") or "").strip()
            if not unit_id:
                continue
            refs = tuple(u.strip() for u in (row.get("returned_photo_ref") or "").split(";") if u.strip())
            scenario = (row.get("scenario") or "").strip() or None
            parts_missing = (row.get("parts_missing") or "").strip() or None
            rows.append(
                ReturnedRow(
                    record_id=(row.get("record_id") or "").strip(),
                    unit_id=unit_id,
                    org_id=(row.get("org_id") or "").strip(),
                    order_id=(row.get("order_id") or "").strip(),
                    ordered_sku=(row.get("ordered_sku") or "").strip(),
                    ordered_asin=(row.get("ordered_asin") or "").strip(),
                    returned_photo_refs=refs,
                    time=(row.get("time") or "").strip(),
                    scenario=scenario,
                    parts_missing=parts_missing,
                )
            )
    return rows


OUTPUT_FIELDNAMES = [
    "record_id",
    "unit_id",
    "org_id",
    "order_id",
    "ordered_sku",
    "ordered_asin",
    "identity_match",
    "parts_list",
    "parts_missing",
    "observed_state",
    "amazon_condition",
    "operator_disposition",
    "photo_refs",
    "captured_at",
    "sold_vs_returned_id_check",
]


def write_output_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in OUTPUT_FIELDNAMES})
