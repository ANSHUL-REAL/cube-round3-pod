"""
Domain Models for CUBE Receiving Manager (Pod 01)
Phase 1: Foundational contracts & strong Pydantic validation.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class CheckVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


class FinalVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"
    PENDING_REVIEW = "PENDING_REVIEW"


class DamageType(str, Enum):
    NONE = "none"
    CRUSHING = "crushing"
    WATER = "water"
    TEARS = "tears"
    UNCERTAIN = "uncertain"


class ViewType(str, Enum):
    PALLET = "pallet"
    CARTON = "carton"
    UNIT = "unit"
    BARCODE = "barcode"
    LABEL = "label"
    OVERVIEW = "overview"


class POHeader(BaseModel):
    po_number: str = Field(..., min_length=1, description="Purchase order identifier")
    po_line: int = Field(..., ge=1, description="Line number on the purchase order")
    supplier: str = Field(..., min_length=1, description="Supplier / Vendor name")


class POExpected(BaseModel):
    sku: str = Field(..., min_length=1, description="Stock Keeping Unit expected")
    asin: str = Field(..., min_length=1, description="Amazon Standard Identification Number")
    product_title: str = Field(..., min_length=1, description="Product name/title")
    expected_colour: str = Field(..., description="Agreed color spec (or 'n/a')")
    expected_variant: str = Field(..., description="Agreed variant/size spec (or 'standard')")
    expected_components: List[str] = Field(default_factory=list, description="List of required components")
    cartons_ordered: int = Field(..., ge=0, description="Master cartons expected")
    units_per_carton_ordered: int = Field(..., ge=0, description="Units per master carton expected")
    quantity_ordered: int = Field(..., ge=0, description="Total sellable units expected")

    @model_validator(mode="after")
    def validate_total_quantity(self):
        computed = self.cartons_ordered * self.units_per_carton_ordered
        # Only validate if nonzero cartons and units_per_carton
        if self.cartons_ordered > 0 and self.units_per_carton_ordered > 0:
            if self.quantity_ordered != computed:
                raise ValueError(
                    f"quantity_ordered ({self.quantity_ordered}) must equal cartons_ordered ({self.cartons_ordered}) * units_per_carton_ordered ({self.units_per_carton_ordered}) = {computed}"
                )
        return self


class BoundingBox(BaseModel):
    label: str = Field(..., min_length=1)
    box_2d: List[float] = Field(..., min_length=4, max_length=4, description="[ymin, xmin, ymax, xmax] normalized (0.0 to 1.0)")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


class EvidenceReference(BaseModel):
    view_type: ViewType
    photo_reference: str = Field(..., min_length=1)
    sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$", description="SHA-256 hash of the evidence image file")
    bounding_boxes: Optional[List[BoundingBox]] = Field(default_factory=list)


class VisualObservation(BaseModel):
    ocr_text: Optional[str] = Field(None, description="Raw text extracted from carton/unit labels")
    identified_sku: Optional[str] = Field(None, description="SKU observed on physical goods")
    barcode: Optional[str] = Field(None, description="Observed barcode/FNSKU/UPC value")
    cartons_counted: Optional[int] = Field(None, ge=0, description="Number of master cartons detected")
    units_per_carton_counted: Optional[int] = Field(None, ge=0, description="Units counted inside an inspected carton")
    quantity_counted: Optional[int] = Field(None, ge=0, description="Total units counted/calculated")
    carton_damage: DamageType = Field(DamageType.UNCERTAIN, description="Observed condition of carton")
    unit_damage: DamageType = Field(DamageType.UNCERTAIN, description="Observed condition of unit")
    observed_colour: Optional[str] = Field(None, description="Color classified from unit image")
    observed_variant: Optional[str] = Field(None, description="Variant classified from unit image")
    observed_components: Optional[List[str]] = Field(default=None, description="Observed item components")
    image_clarity: Optional[float] = Field(None, ge=0.0, le=1.0, description="Quality/clarity score of the image capture")


class CheckResult(BaseModel):
    verdict: CheckVerdict
    reason: str = Field(..., min_length=1)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    discrepancy: Optional[int] = Field(None, description="Numerical variance for count checks")
    damage_type: Optional[str] = Field(None, description="Damage type string if applicable")
    flagged_issues: Optional[List[str]] = Field(default_factory=list, description="Specific issue flags")


class IndividualChecks(BaseModel):
    identity_check: CheckResult
    quantity_check: CheckResult
    carton_condition_check: CheckResult
    unit_condition_check: CheckResult
    specification_check: CheckResult


class OperatorOverride(BaseModel):
    original_verdict: FinalVerdict
    override_verdict: FinalVerdict
    operator_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceRecord(BaseModel):
    record_id: str = Field(..., pattern=r"^RCV-[A-Za-z0-9_-]{4,20}$", description="Stage record ID")
    unit_id: str = Field(..., pattern=r"^(UNIT|DEMO)-[A-Za-z0-9_-]{4,20}$", description="Global cross-pod join key")
    org_id: str = Field(..., min_length=1, description="Tenant organization ID for RLS isolation")
    operator_id: str = Field(..., min_length=1, description="Dock operator who initiated inspection")
    captured_at: datetime = Field(..., description="UTC capture timestamp")
    po_header: POHeader
    expected_values: POExpected
    observed_values: VisualObservation
    individual_checks: IndividualChecks
    final_verdict: FinalVerdict
    verdict_reason: str = Field(..., min_length=1)
    evidence_references: List[EvidenceReference] = Field(..., min_length=1)
    operator_override: Optional[OperatorOverride] = None


class Outcome(str, Enum):
    """
    Deterministic receiving outcome.
    - accept: all required checks PASS.
    - accept_with_exceptions: one or more actionable exceptions/FAILs where shipment can continue.
    - reject: critical receiving failure (wrong SKU/ASIN or unit damage).
    - pending_review: model/evidence failure prevents reliable decision.
    """
    ACCEPT = "accept"
    ACCEPT_WITH_EXCEPTIONS = "accept_with_exceptions"
    REJECT = "reject"
    PENDING_REVIEW = "pending_review"


class SharedCheckKey(str, Enum):
    IDENTITY_MATCH = "identity_match"
    CARTON_COUNT = "carton_count"
    QUANTITY = "quantity"
    CARTON_DAMAGE = "carton_damage"
    UNIT_DAMAGE = "unit_damage"
    QUALITY_FLAGS = "quality_flags"


class SharedCheck(BaseModel):
    """
    Shared receiving check item contract for Round 3 CUBE interoperability.
    Allowed verdicts: PASS, FAIL, UNCERTAIN.
    """
    check_key: str = Field(..., description="One of the 6 shared receiving check keys")
    verdict: CheckVerdict = Field(..., description="PASS, FAIL, or UNCERTAIN")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    expected: Any = Field(..., description="Expected PO specification value")
    observed: Any = Field(..., description="Observed evidence value")
    detail: str = Field(..., min_length=1, description="Explanatory text for verdict")
    evidence_refs: List[str] = Field(default_factory=list, description="SHA-256 content hashes of supporting evidence photos")
    model_version: str = Field("gemini-2.5-flash", description="Model version used for observation")
    latency_ms: float = Field(0.0, ge=0.0, description="Inference latency in milliseconds")


class AgentSubject(BaseModel):
    """
    Subject metadata for the receiving record.
    IMPORTANT ARCHITECTURAL ASSUMPTION:
    A Receiving Manager record represents a receiving/PO line.
    It does NOT claim that every Receiving record represents exactly one physical unit.
    unit_scope is explicitly set to 'po_line'.
    """
    unit_id: str = Field(..., pattern=r"^(UNIT|DEMO)-[A-Za-z0-9_-]{4,20}$", description="Global cross-pod join key")
    unit_scope: str = Field("po_line", description="Scope of receiving inspection: po_line")
    po_number: Optional[str] = None
    po_line: Optional[int] = None
    sku: Optional[str] = None
    asin: Optional[str] = None


class AgentInfo(BaseModel):
    name: str = Field("receiving_manager", description="Agent identity")
    pod: str = Field("01", description="Pod position in stream")
    version: str = Field("3.0.0", description="Implementation version")
    model_version: str = Field("gemini-2.5-flash", description="Underlying vision model")


class AgentOutput(BaseModel):
    """
    Round 3 Machine-readable Agent Output schema for cross-pod integration.
    Fully backwards-compatible with Round 2 EvidenceRecord.
    """
    record_id: str = Field(..., pattern=r"^RCV-[A-Za-z0-9_-]{4,20}$", description="Stage record ID")
    schema_version: str = Field("3.0.0", description="Contract schema version")
    organization_id: str = Field(..., min_length=1, description="Tenant organization ID for multi-tenant isolation")
    client_id: str = Field(..., min_length=1, description="Client / requesting entity identifier")
    agent: AgentInfo = Field(default_factory=AgentInfo)
    subject: AgentSubject
    captured_at: datetime = Field(..., description="UTC capture timestamp")
    operator_label: str = Field(..., description="Operator label who handled inspection")
    images: List[Dict[str, Any]] = Field(default_factory=list, description="Preserved photo captures with content hashes")
    checks: List[SharedCheck] = Field(..., min_length=6, max_length=6, description="Exactly the six shared receiving checks")
    outcome: Outcome = Field(..., description="accept, accept_with_exceptions, reject, pending_review")
    overrides: Optional[List[Dict[str, Any]]] = None
    status: str = Field("completed", description="Lifecycle status (completed, pending_review, failed)")
    content_hash: str = Field(..., description="SHA-256 hash of canonical output payload")

    # Backwards-compatibility fields with Round 2
    po_header: Optional[POHeader] = None
    expected_values: Optional[POExpected] = None
    observed_values: Optional[VisualObservation] = None
    individual_checks: Optional[IndividualChecks] = None
    final_verdict: Optional[FinalVerdict] = None
    verdict_reason: Optional[str] = None
    evidence_references: Optional[List[EvidenceReference]] = None
    operator_override: Optional[OperatorOverride] = None

