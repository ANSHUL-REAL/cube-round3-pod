"""Schema validation against the Round 3 contract schemas."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
SCHEMAS = {name: f"{name}.schema.json" for name in
           ("agent-input", "agent-output", "evidence", "workflow-state", "final-outcome", "error")}


@lru_cache(maxsize=1)
def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMA_DIR.glob("*.schema.json"):
        doc = json.loads(path.read_text())
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return registry


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    doc = json.loads((SCHEMA_DIR / SCHEMAS[name]).read_text())
    return Draft202012Validator(doc, registry=_registry(), format_checker=FormatChecker())


def errors(name: str, instance: dict) -> list[str]:
    """Human-readable list of schema violations (empty list means valid)."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(_validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]


def validate(name: str, instance: dict) -> None:
    errs = errors(name, instance)
    if errs:
        raise ValueError(f"{name} failed schema validation:\n  " + "\n  ".join(errs))
