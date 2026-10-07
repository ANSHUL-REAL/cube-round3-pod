"""Data shapes shared by the agent, the web app and the eval tooling.

Evidence-record field names follow the handbook's evidence contract (section 9):
record_id, schema_version, organization_id, client_id, agent, subject, captured_at,
operator_label, images, checks[], outcome, overrides[], status, content_hash.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer

SCHEMA_VERSION = "cube.evidence.v1"
AGENT_NAME = "pack-manager"

DEFAULT_ALLOWED_INSERTS = [
    "packing slip",
    "invoice",
    "return form",
    "thank-you card",
    "promotional flyer or insert",
    "air pillows",
    "bubble wrap",
    "crumpled or shredded paper",
    "tissue paper",
    "foam or cardboard divider",
    "desiccant sachet",
]

OVERRIDE_REASONS = {
    "agent_miscount": "Agent counted wrong",
    "variant_verified_by_label": "Checked the label/variant by hand",
    "item_is_insert_or_packaging": "Flagged item is packaging or an insert",
    "hidden_item_verified": "Checked hidden or stacked items by hand",
    "box_fixed": "Box was fixed and re-checked by hand",
    "order_changed": "Order was changed by customer service",
    "agent_unavailable": "Agent unavailable, checked by hand",
    "other": "Other (see note)",
}


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"
    NOT_CHECKED = "NOT_CHECKED"


class Decision(str, Enum):
    SEAL = "SEAL"
    STOP_AND_FIX = "STOP_AND_FIX"
    UNCERTAIN = "UNCERTAIN"
    PENDING = "PENDING"


class RecordStatus(str, Enum):
    FINAL = "final"  # agent decided SEAL or STOP_AND_FIX
    PENDING_REVIEW = "pending_review"  # agent said UNCERTAIN; a human must decide
    PENDING = "pending"  # vision model failed; photos saved, human must decide
    OVERRIDDEN = "overridden"  # a human recorded a decision


# ---------------------------------------------------------------- catalogue & order


class CatalogueItem(BaseModel):
    sku: str
    title: str
    asin: str | None = None
    attributes: dict[str, str] = {}
    # What ONE sellable unit looks like, e.g. "white gift box holding 3 candles".
    sellable_unit: str = ""
    # The visible detail that separates this SKU from its look-alikes.
    distinguishing_features: str = ""
    confusable_with: list[str] = []
    # Other SKUs that resemble a part shipped inside this SKU (a lamp's USB cable
    # vs the USB-C cable sold on its own). A loose one is ambiguous, not "extra".
    component_lookalikes: list[str] = []
    components: list[str] = []
    reference_images: list[str] = []


class Catalogue(BaseModel):
    organization_id: str | None = None
    items: list[CatalogueItem]
    allowed_inserts: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_INSERTS))

    def get(self, sku: str) -> CatalogueItem | None:
        for item in self.items:
            if item.sku == sku:
                return item
        return None

    def title(self, sku: str | None) -> str:
        item = self.get(sku) if sku else None
        return item.title if item else (sku or "unidentified item")

    @property
    def skus(self) -> list[str]:
        return [i.sku for i in self.items]


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class Order(BaseModel):
    order_id: str
    organization_id: str
    client_id: str | None = None
    unit_id: str | None = None
    channel: str | None = None
    # The outbound shipment this box leaves in, when the warehouse system has one at packing
    # time. Recovery joins charges to evidence on it (Evidence Contract 1.1, subject.shipment_id).
    shipment_id: str | None = None
    lines: list[OrderLine]

    def expected(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for line in self.lines:
            out[line.sku] = out.get(line.sku, 0) + line.qty
        return out


def parse_lines(text: str) -> list[OrderLine]:
    """Parse the sample-data format ``SKU:qty;SKU:qty``. Raises ValueError on anything else,
    e.g. "CAP:1,MUG:2" (commas instead of semicolons) or a line without a sku."""
    lines = []
    for part in filter(None, (p.strip() for p in text.split(";"))):
        sku, sep, qty = part.rpartition(":")
        sku = sku.strip()
        if not sep or not sku or any(c in sku for c in ":,"):
            raise ValueError(f'"{part}" should look like SKU:qty, with ";" between lines')
        lines.append(OrderLine(sku=sku, qty=int(qty)))
    return lines


# ---------------------------------------------------------------- perception (model output)


class DetectedObject(BaseModel):
    object_id: str
    photo: int = 1
    # [ymin, xmin, ymax, xmax] normalised to 0-1000 on the given photo.
    box_2d: list[int] | None = None
    description: str = ""
    classification: Literal["CANDIDATE", "UNKNOWN_PRODUCT", "NON_PRODUCT"]
    sku: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    alternative_skus: list[str] = []
    deciding_feature: str = ""
    partially_hidden: bool = False


class SkuCount(BaseModel):
    sku: str
    count: int
    count_certain: bool
    reason: str = ""


class Scene(BaseModel):
    box_interior_fully_visible: bool
    items_may_be_hidden: bool
    visibility_confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class Perception(BaseModel):
    objects: list[DetectedObject]
    counts: list[SkuCount] = []
    scene: Scene
    image_issues: list[str] = []
    candidates: list[str] = []
    model_version: str
    prompt_version: str
    latency_ms: int
    usage: dict[str, int] = {}
    cached: bool = False


# ---------------------------------------------------------------- evidence record


class QualityReport(BaseModel):
    gate: Literal["PASS", "FAIL"]
    reasons: list[str] = []
    width: int
    height: int
    blur_var: float
    mean_luma: float
    clipped_pct: float
    dark_pct: float


class ImageRef(BaseModel):
    image_id: str
    role: str
    # Hash of the exact bytes stored and shown to the model (upright, resized JPEG).
    sha256: str
    original_sha256: str
    mime: str
    width: int
    height: int
    quality: QualityReport


class Check(BaseModel):
    check_key: str
    verdict: Verdict
    confidence: float | None = None
    detail: str
    model_version: str | None = None
    latency_ms: int | None = None
    evidence: dict[str, Any] = {}


class Outcome(BaseModel):
    decision: Decision
    decided_by: str | None
    decided_at: datetime | None
    reasons: list[str] = []
    fix_instructions: list[str] = []


class Override(BaseModel):
    original_decision: Decision
    new_decision: Decision
    reason_code: str
    note: str = ""
    operator_label: str
    at: datetime
    prior_content_hash: str
    # What the override replaced, so the record as it was before can be rebuilt and checked
    # against prior_content_hash (evidence.verify_history). None on overrides made before this.
    prior_outcome: Outcome | None = None
    prior_status: RecordStatus | None = None
    # Set when a person corrects one check rather than deciding the box (Evidence Contract 1.1,
    # section 4: an override on every check). The box decision is then unchanged, so
    # original_decision == new_decision. None on a box decision, which is an override of the
    # contract's order_matches_manifest.
    check_key: str | None = None
    from_verdict: Literal["pass", "fail", "uncertain"] | None = None
    to_verdict: Literal["pass", "fail", "uncertain"] | None = None

    @model_serializer(mode="wrap")
    def _leave_out_unset_check(self, handler):
        # Box decisions are written exactly as before these fields existed, so older records keep
        # their content hash and the database's append-only rule still sees the same overrides.
        data = handler(self)
        if isinstance(data, dict) and self.check_key is None:
            for key in ("check_key", "from_verdict", "to_verdict"):
                data.pop(key, None)
        return data


class EvidenceRecord(BaseModel):
    record_id: str
    schema_version: str = SCHEMA_VERSION
    organization_id: str
    client_id: str | None = None
    agent: dict[str, str]
    subject: dict[str, Any]
    captured_at: datetime
    operator_label: str
    images: list[ImageRef]
    observations: dict[str, Any] = {}
    checks: list[Check]
    outcome: Outcome
    overrides: list[Override] = []
    status: RecordStatus
    content_hash: str = ""

    @property
    def box_overrides(self) -> list[Override]:
        """A person's decisions on the whole box, leaving out corrections of single checks."""
        return [o for o in self.overrides if o.check_key is None]
