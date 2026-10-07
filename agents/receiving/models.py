"""Data shapes for the Receiving Manager.

``PurchaseOrderLine`` is what should arrive (from the PO). ``Observation`` is what a vision model reports it SAW.
The two never meet inside the model: the model is not shown the purchase order. Deterministic rules (rules.py)
compare them.

Ported from the Round 2 Receiving Manager (@cherryy-x23): ``POExpected`` -> ``PurchaseOrderLine``,
``VisualObservation`` -> ``Observation``. See PROVENANCE.md for what changed.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

Damage = Literal["none", "crushing", "water", "tears", "uncertain"]
Defect = Literal["yes", "no", "unsure"]


class OrderError(ValueError):
    """The purchase-order data cannot be judged against (missing fields, impossible quantities)."""


class PurchaseOrderLine(BaseModel):
    po_number: str
    po_line: int
    supplier: str
    sku: str = Field(min_length=1)
    asin: str = ""
    product_title: str = ""
    spec_colour: str = "n/a"
    spec_variant: str = "standard"
    spec_components: list[str] = Field(default_factory=list)
    cartons_ordered: int = Field(gt=0)
    units_per_carton_ordered: int = Field(gt=0)
    qty_ordered: int = Field(gt=0)

    @field_validator("spec_components", mode="before")
    @classmethod
    def _split(cls, v: Any):
        if isinstance(v, str):
            return [p.strip() for p in v.split(";") if p.strip()]
        return v or []

    @model_validator(mode="after")
    def _qty_adds_up(self):
        if self.qty_ordered != self.cartons_ordered * self.units_per_carton_ordered:
            raise ValueError(f"qty_ordered {self.qty_ordered} != cartons_ordered {self.cartons_ordered} x "
                             f"units_per_carton_ordered {self.units_per_carton_ordered}")
        return self

    @classmethod
    def parse(cls, raw: dict) -> "PurchaseOrderLine":
        """From a dict (the organisers' CSV row, or ``context.order``). Reads ONLY the ordered/spec side: never the
        received, damage or identity columns, which are the answers the agent is supposed to find out by looking."""
        try:
            return cls(**{k: raw[k] for k in cls.model_fields if k in raw})
        except ValidationError as exc:
            raise OrderError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
        except ValueError as exc:
            raise OrderError(str(exc)) from exc


class Observation(BaseModel):
    """What the model reports from the photos. Every field is required (a model that skips one fails validation,
    which fails open); null / "uncertain" / "unsure" are the honest answers when something cannot be read."""

    ocr_text: str | None
    identified_sku: str | None
    barcode: str | None
    cartons_counted: int | None = Field(ge=0)
    units_per_carton_counted: int | None = Field(ge=0)
    quantity_counted: int | None = Field(ge=0)
    carton_damage: Damage
    unit_damage: Damage
    observed_colour: str | None
    observed_variant: str | None
    observed_components: list[str] | None
    obvious_defect: Defect
    image_clarity: float = Field(ge=0, le=1)
    # Self-reported, uncalibrated. Used only as a gate that can turn a verdict into UNCERTAIN, never reported as the
    # check's confidence.
    identity_confidence: float = Field(ge=0, le=1)
    count_confidence: float = Field(ge=0, le=1)
    damage_confidence: float = Field(ge=0, le=1)
    spec_confidence: float = Field(ge=0, le=1)


class Perception(BaseModel):
    """One batched model answer plus what is needed to describe the model honestly in the record."""

    observation: Observation
    model_name: str
    model_version: str
    provider: str | None
    prompt_version: str
    calls: int = 1
    latency_ms: int | None = None
    usage: dict[str, int] = Field(default_factory=dict)


def norm_id(text: str | None) -> str:
    """Case, spaces and punctuation do not change an identifier: 'sku-towel-blu ' == 'SKU TOWEL BLU'."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def tokens(text: str | None) -> frozenset[str]:
    """Order-insensitive words, with a trailing quantity ('x2') and the filler word 'of' dropped."""
    t = re.sub(r"\bx\s*\d+\b", " ", (text or "").lower())
    return frozenset(w for w in re.findall(r"[a-z0-9]+", t) if w != "of")
