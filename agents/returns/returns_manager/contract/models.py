"""Pydantic models for the fixed evidence contract (§14.2) and extensions.returns (§14.2b).

Every evidence record this system emits — REST, MCP, export, webhook — is an
EvidenceRecord instance.  The models are used to:
  - validate records at build time (contract.build);
  - generate evidence-record.v1.schema.json (contract.schema);
  - type the REST and MCP response bodies.

IMPORTANT: field names and the top-level structure are dictated by the Official
Participant Handbook §9 / the build prompt §14.2.  Never add a field at the
top level that is defined inside extensions.returns, and vice versa.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Fixed check verdicts (§14.2) ─────────────────────────────────────────────

Verdict = Literal["PASS", "FAIL", "UNCERTAIN"]


class CheckEntry(BaseModel):
    """One entry in the fixed `checks` list of an evidence record.

    `confidence_bp` is 0-10000 (basis points) stored in the document.
    Divide by 10000 for the 0.00-1.00 display value.
    The document stores integers only; no floats in hashed payloads (ss13.1).
    """

    check_key: str = Field(..., description="Canonical check identifier, e.g. 'identity'.")
    verdict: Verdict
    confidence_bp: int = Field(
        ...,
        ge=0,
        le=10000,
        description="Confidence in basis points (0-10000). Divide by 10000 for display.",
    )
    detail: str = Field(
        ...,
        description="Plain-English sentence a customer can read; never contains secrets or URLs.",
    )
    model_version: str = Field(
        ...,
        description=(
            "<model_id>@<prompt_id>-<prompt_version> for model-derived checks; "
            "deterministic@<component>-<version> for deterministic checks."
        ),
    )
    latency_ms: int = Field(..., ge=0, description="Wall-clock ms for this check.")

    @property
    def confidence(self) -> float:
        """0.00-1.00 display value (bp / 10000, rounded to 2 decimals)."""
        return round(self.confidence_bp / 10000, 2)


# ── Images ────────────────────────────────────────────────────────────────────


class ImageEntry(BaseModel):
    image_id: str
    slot: int = Field(..., ge=1)
    role: str = Field(..., description="front | back | label | contents | other")
    sha256: str
    quality: Literal["pass", "fail", "warning", "acknowledged"]
    url: str | None = Field(None, description="Short-TTL signed URL; null unless requested.")


# ── Outcome ───────────────────────────────────────────────────────────────────


class Outcome(BaseModel):
    decision: Literal["RESTOCK", "REFURBISH", "LIQUIDATE", "DISPOSE", "PENDING_REVIEW"]
    decided_by: str = Field(
        ...,
        description=(
            "rules_engine@<version> for the engine; operator:<label> or reviewer:<label> "
            "for a human.  Never the model name."
        ),
    )
    decided_at: str = Field(..., description="RFC 3339 UTC, second precision.")


# ── Overrides ─────────────────────────────────────────────────────────────────


class OverrideEntry(BaseModel):
    check_key: str
    original: Any
    new: Any
    reason_code: str
    reason: str
    by: str = Field(..., description="operator:<label> or reviewer:<label>")
    at: str = Field(..., description="RFC 3339 UTC")


# ── Extensions: returns block (§14.2b) ───────────────────────────────────────


class IntegrityBlock(BaseModel):
    document_sha256: str
    head_event_hash: str
    event_count: int = Field(..., ge=0)
    ledger_seq: int = Field(..., ge=0)
    ledger_hash: str
    claim: str = Field(
        ...,
        description=("Verbatim ADR-007 honesty sentence: tamper-evident within database, not immutable."),
    )


class IdentityBlock(BaseModel):
    identity_match: Literal["yes", "no", "uncertain"]
    evidence_strength: Literal["strong", "moderate", "weak", "conflict"]
    risk_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    observed_identifiers: list[str] = Field(default_factory=list)
    feature_checks: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ComponentEntry(BaseModel):
    component_id: str
    name: str
    expected: int = Field(..., ge=0)
    observed: int | None = None
    status: Literal["present", "missing", "uncertain"]
    essential: bool
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class CompletenessBlock(BaseModel):
    status: Literal["complete", "incomplete", "uncertain"]
    parts_list: str = Field(..., description="';'-separated, e.g. 'usb cable;manual'")
    parts_missing: str = Field(default="")
    parts_uncertain: str = Field(default="")
    components: list[ComponentEntry] = Field(default_factory=list)


class RubricRef(BaseModel):
    snapshot_id: str
    source_marketplace: str
    applies_to_marketplace: str
    verification_status: str
    content_sha256: str
    phrases_matched: list[str] = Field(default_factory=list)


class ConditionBlock(BaseModel):
    amazon_condition: str = Field(
        ...,
        description=(
            "Amazon label ('New', 'Used - Like New', …) or 'uncertain'. Never changed by missing parts."
        ),
    )
    cosmetic_grade: str | None = None
    listing_blockers: list[str] = Field(default_factory=list)
    functional_check: Literal["not_performed"] = "not_performed"
    packaging_state: str | None = None
    signs_of_use: str | None = None
    observations: list[dict[str, Any]] = Field(default_factory=list)
    rubric: RubricRef | None = None


class DispositionBlock(BaseModel):
    recommended_disposition: Literal["restock", "refurbish", "liquidate", "dispose"] | None
    no_recommendation_reason: str | None = None
    provisional: bool = False
    assumptions: list[str] = Field(default_factory=list)
    requires_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    final_disposition: str | None = Field(None, description="Human-confirmed; null until finalized.")
    listing_condition: str | None = None
    relistable_as_is: bool
    rule_id: str
    rules_version: str
    decided_by: str = "deterministic_engine"
    requires_signoff: bool = False
    signoff: dict[str, Any] | None = None
    expected_recovery: dict[str, Any] | None = None


class ClaimSignalsBlock(BaseModel):
    item_not_returned: bool
    wrong_item_returned: bool
    returned_damaged: dict[str, Any] = Field(default_factory=dict)
    parts_missing: list[str] = Field(default_factory=list)
    parts_uncertain: list[str] = Field(default_factory=list)


class LinksBlock(BaseModel):
    self: str
    chain: str
    verification: str
    explain: str


class ReturnsExtension(BaseModel):
    """extensions.returns block (§14.2b).

    Fields already in the fixed contract top level are NOT repeated here.
    """

    contract_version: str = "1.0.0"
    record_id: str
    record_version: int = Field(..., ge=1)
    org_id: str
    unit_id: str
    return_id: str
    order_id: str
    ordered_sku: str
    ordered_asin: str
    observed_fnsku: str | None = None
    actual_sku: str | None = None
    captured_at: str
    finalized_at: str | None = None
    comparison_mode: Literal["catalogue_return", "before_after_unit"] = "catalogue_return"
    upstream_evidence: list[dict[str, Any]] = Field(default_factory=list)
    photos: list[dict[str, Any]] = Field(default_factory=list)
    identity: IdentityBlock | None = None
    unit_presence: dict[str, Any] | None = None
    completeness: CompletenessBlock | None = None
    condition: ConditionBlock | None = None
    observed_state: dict[str, Any] | None = None
    disposition: DispositionBlock | None = None
    claim_signals: ClaimSignalsBlock | None = None
    uncertainty: list[dict[str, Any]] = Field(default_factory=list)
    review: dict[str, Any] | None = None
    overrides: list[OverrideEntry] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None
    integrity: IntegrityBlock | None = None
    links: LinksBlock | None = None


# ── Top-level fixed contract (§14.2) ─────────────────────────────────────────

EvidenceStatus = Literal[
    "pending",
    "awaiting_operator",
    "awaiting_review",
    "awaiting_signoff",
    "finalized",
    "superseded",
    "needs_attention",
]

SCHEMA_VERSION = "1.0.0"
AGENT_NAME = "returns_manager"
INTEGRITY_CLAIM = (
    "Tamper-evident within this database: modification, reordering, insertion or deletion "
    "of events or records is detected unless an attacker rewrites the entire chain "
    "consistently. Not immutable. Weakly anchored: ledger heads are published in public "
    "git commits; history before an anchor cannot be silently rewritten without breaking "
    "the anchor."
)


class EvidenceRecord(BaseModel):
    """The fixed evidence contract record (§14.2).

    Schema version 1.0.0.  All top-level field names are dictated by the
    Official Participant Handbook §9.  Never rename them.
    """

    model_config = {"populate_by_name": True}

    record_id: str = Field(..., description="Stage prefix RTN-; opaque to consumers.")
    schema_version: str = Field(SCHEMA_VERSION, description="Semver of this contract.")
    organization_id: str = Field(..., description="Tenant identifier (= internal org_id).")
    client_id: str = Field(
        ...,
        description=(
            "Seller/client; equals organization_id when an org processes its own returns "
            "(see OQ-1 / §14.2 note on client_id)."
        ),
    )
    agent: str = Field(AGENT_NAME, description="Fixed string for this track.")
    subject: dict[str, Any] = Field(..., description="{unit_id, return_id, order_id, sku, asin}")
    captured_at: str = Field(..., description="RFC 3339 UTC, second precision.")
    operator_label: str = Field(..., description="Human-readable, pseudonymous operator label.")
    images: list[ImageEntry] = Field(default_factory=list)
    checks: list[CheckEntry] = Field(
        default_factory=list,
        description="All fixed check keys in the canonical order (§14.2).",
    )
    outcome: Outcome
    overrides: list[OverrideEntry] = Field(default_factory=list)
    status: EvidenceStatus
    content_hash: str = Field(
        ...,
        description="sha256:<hex> of SHA-256(JCS(record without this field)).",
    )
    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description='{"returns": ReturnsExtension}; no top-level key is repeated here.',
    )
