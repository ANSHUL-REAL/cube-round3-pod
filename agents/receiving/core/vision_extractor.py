"""
Multimodal Vision Extractor for CUBE Receiving Manager (Pod 01)
Phase 2: Single-batch vision observation abstraction with fail-open resilience.

Core Architectural Tenet:
- VISION AI IS AN EVIDENCE EXTRACTOR ONLY.
- It does NOT make the final PASS/FAIL decision.
- Exactly ONE batched call is made per receiving unit, carrying all checks.
- If the model errors, times out, or is unconfigured, it fails open to PENDING_REVIEW.
"""

import abc
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from .models import (
    DamageType,
    EvidenceReference,
    POExpected,
    ViewType,
    VisualObservation,
)


def calculate_file_sha256(file_path: Union[str, Path]) -> str:
    """Computes SHA-256 hash of a file if it exists, or hashes the path string if placeholder."""
    path = Path(file_path)
    if path.exists() and path.is_file():
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    # If the file path is a virtual/placeholder reference, hash the reference string deterministically
    return hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()


def build_evidence_references(images: Dict[Union[ViewType, str], str]) -> List[EvidenceReference]:
    """Builds valid EvidenceReference objects with real SHA-256 hashes."""
    references = []
    for view, ref_path in images.items():
        if isinstance(view, ViewType):
            v_type = view
        else:
            try:
                v_type = ViewType(str(view).lower())
            except ValueError:
                v_type = ViewType.OVERVIEW

        sha = calculate_file_sha256(ref_path)
        references.append(
            EvidenceReference(
                view_type=v_type,
                photo_reference=str(ref_path),
                sha256=sha,
                bounding_boxes=[],
            )
        )
    return references


@dataclass
class ExtractionResult:
    """Container for the output of a vision extraction attempt."""
    status: str  # "success", "timeout", "error", "invalid_json", "unconfigured"
    observation: Optional[VisualObservation] = None
    error_message: Optional[str] = None
    raw_response: Optional[str] = None
    evidence_references: List[EvidenceReference] = field(default_factory=list)


# Single structured prompt for multimodal extraction
STRUCTURED_VISION_PROMPT = """You are an objective computer vision evidence extractor stationed at an inbound warehouse receiving dock.
Your role is to OBSERVE and EXTRACT physical evidence from the provided photographs.

IMPORTANT OPERATIONAL RULES:
1. Extract observable evidence only. DO NOT make the final commercial receiving decision (do NOT emit PASS or FAIL).
2. Do not assume or extrapolate missing information. If something is not clearly visible, return null or "uncertain".
3. Do not convert uncertainty or missing evidence into a clean confirmation.
4. Distinguish master carton condition from sellable unit condition.
5. Extract all readable OCR text and barcode numbers from carton labels and product packaging.
6. Count only cartons and units that are directly verifiable from the photographs.
7. Note any visual clarity problems (blur, severe glare, plastic wrap reflections, occlusion, poor lighting).

EXPECTED PURCHASE ORDER ATTRIBUTES (FOR CONTEXT ONLY):
- Expected SKU: {expected_sku}
- Expected ASIN: {expected_asin}
- Expected Title: {expected_title}
- Expected Colour: {expected_colour}
- Expected Variant: {expected_variant}
- Expected Components: {expected_components}
- Expected Master Cartons: {cartons_ordered}
- Expected Units per Carton: {units_per_carton}
- Expected Total Units: {quantity_ordered}

You must return valid JSON matching this schema exactly:
{
  "ocr_text": string or null,
  "identified_sku": string or null,
  "barcode": string or null,
  "cartons_counted": integer or null,
  "units_per_carton_counted": integer or null,
  "quantity_counted": integer or null,
  "carton_damage": "none" | "crushing" | "water" | "tears" | "uncertain",
  "unit_damage": "none" | "crushing" | "water" | "tears" | "uncertain",
  "observed_colour": string or null,
  "observed_variant": string or null,
  "observed_components": list of strings or null,
  "image_clarity": float (0.0 to 1.0)
}
"""


class VisionExtractor(abc.ABC):
    """Abstract Base Class for Vision Observation Extractors."""

    def __init__(self):
        self._call_count = 0

    @property
    def call_count(self) -> int:
        """Tracks the number of model invocations (must be 1 per unit)."""
        return self._call_count

    @abc.abstractmethod
    def extract(
        self,
        po: POExpected,
        unit_id: str,
        images: Dict[Union[ViewType, str], str],
    ) -> ExtractionResult:
        """
        Executes a single batched multimodal observation on all images for a receiving unit.
        """
        pass


class MockVisionExtractor(VisionExtractor):
    """
    Deterministic Mock Vision Extractor for automated unit and integration tests.
    EXPLICITLY A TEST FIXTURE ONLY - NEVER USED AS FAKE PRODUCTION INSPECTION.
    """

    def __init__(
        self,
        preset_observation: Optional[VisualObservation] = None,
        simulate_timeout: bool = False,
        simulate_error: bool = False,
        simulate_invalid_json: bool = False,
        error_message: Optional[str] = None,
    ):
        super().__init__()
        self.preset_observation = preset_observation
        self.simulate_timeout = simulate_timeout
        self.simulate_error = simulate_error
        self.simulate_invalid_json = simulate_invalid_json
        self.error_message = error_message

    def extract(
        self,
        po: POExpected,
        unit_id: str,
        images: Dict[Union[ViewType, str], str],
    ) -> ExtractionResult:
        # Increment call counter to prove single-batch invocation
        self._call_count += 1
        refs = build_evidence_references(images)

        if self.simulate_timeout:
            return ExtractionResult(
                status="timeout",
                observation=None,
                error_message=self.error_message or "Vision provider timed out after 30000ms",
                evidence_references=refs,
            )

        if self.simulate_error:
            return ExtractionResult(
                status="error",
                observation=None,
                error_message=self.error_message or "Internal vision model error (HTTP 500)",
                evidence_references=refs,
            )

        if self.simulate_invalid_json:
            return ExtractionResult(
                status="invalid_json",
                observation=None,
                error_message="Failed to parse model response into JSON schema",
                raw_response="INVALID_NON_JSON_CONTENT",
                evidence_references=refs,
            )

        # Check for explicit demo scenario triggers when no preset observation is provided
        if not self.preset_observation and unit_id:
            uid_upper = unit_id.upper()
            if "FAIL" in uid_upper or "SHORTAGE" in uid_upper or "CRUSH" in uid_upper:
                # Scenario 2: Short shipment (-2 units) and carton crushing
                short_qty = max(1, po.quantity_ordered - 2) if po.quantity_ordered else 22
                obs = VisualObservation(
                    identified_sku=po.sku,
                    barcode=po.asin,
                    cartons_counted=po.cartons_ordered,
                    units_per_carton_counted=po.units_per_carton_ordered,
                    quantity_counted=short_qty,
                    carton_damage=DamageType.CRUSHING,
                    unit_damage=DamageType.NONE,
                    observed_colour=po.expected_colour,
                    observed_variant=po.expected_variant,
                    observed_components=po.expected_components,
                    image_clarity=0.95,
                )
                return ExtractionResult(status="success", observation=obs, evidence_references=refs)

            elif "UNCERTAIN" in uid_upper:
                # Scenario 3: Ambiguous visual evidence (blur/glare, unreadable SKU)
                obs = VisualObservation(
                    identified_sku=None,
                    barcode=None,
                    cartons_counted=po.cartons_ordered,
                    units_per_carton_counted=None,
                    quantity_counted=None,
                    carton_damage=DamageType.UNCERTAIN,
                    unit_damage=DamageType.UNCERTAIN,
                    observed_colour=None,
                    observed_variant=None,
                    observed_components=None,
                    image_clarity=0.35,
                )
                return ExtractionResult(status="success", observation=obs, evidence_references=refs)

            elif "PENDING" in uid_upper or "TIMEOUT" in uid_upper or "FAIL_OPEN" in uid_upper:
                # Scenario 4: Simulated provider timeout/failure for fail-open verification
                return ExtractionResult(
                    status="timeout",
                    observation=None,
                    error_message="Vision provider timed out after 30000ms (Simulated Provider Outage)",
                    evidence_references=refs,
                )

        # Return predefined observation or a clean default match (Scenario 1: Clean Pass)
        obs = self.preset_observation or VisualObservation(
            identified_sku=po.sku,
            barcode=po.asin,
            cartons_counted=po.cartons_ordered,
            units_per_carton_counted=po.units_per_carton_ordered,
            quantity_counted=po.quantity_ordered,
            carton_damage=DamageType.NONE,
            unit_damage=DamageType.NONE,
            observed_colour=po.expected_colour,
            observed_variant=po.expected_variant,
            observed_components=po.expected_components,
            image_clarity=0.95,
        )

        return ExtractionResult(
            status="success",
            observation=obs,
            evidence_references=refs,
        )


class GeminiVisionExtractor(VisionExtractor):
    """
    Live multimodal vision extractor powered by Google Gemini API.
    Executes exactly one batched call carrying all inspection images.
    """

    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-2.5-flash"):
        super().__init__()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name

    def extract(
        self,
        po: POExpected,
        unit_id: str,
        images: Dict[Union[ViewType, str], str],
    ) -> ExtractionResult:
        self._call_count += 1
        refs = build_evidence_references(images)

        if not self.api_key:
            return ExtractionResult(
                status="unconfigured",
                observation=None,
                error_message="VISION PROVIDER NOT CONFIGURED: GEMINI_API_KEY environment variable is not set.",
                evidence_references=refs,
            )

        prompt = STRUCTURED_VISION_PROMPT.format(
            expected_sku=po.sku,
            expected_asin=po.asin,
            expected_title=po.product_title,
            expected_colour=po.expected_colour,
            expected_variant=po.expected_variant,
            expected_components=", ".join(po.expected_components),
            cartons_ordered=po.cartons_ordered,
            units_per_carton=po.units_per_carton_ordered,
            quantity_ordered=po.quantity_ordered,
        )

        try:
            # We support the official google-genai or google.generativeai SDK
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)

            contents = [prompt]
            for view_type, img_path in images.items():
                p = Path(img_path)
                if p.exists() and p.is_file():
                    with open(p, "rb") as f:
                        contents.append(
                            types.Part.from_bytes(
                                data=f.read(),
                                mime_type="image/jpeg",
                            )
                        )

            response = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=VisualObservation,
                ),
            )

            raw_text = response.text
            data = json.loads(raw_text)
            obs = VisualObservation.model_validate(data)

            return ExtractionResult(
                status="success",
                observation=obs,
                raw_response=raw_text,
                evidence_references=refs,
            )

        except Exception as e:
            err_str = str(e)
            is_timeout = "timeout" in err_str.lower() or "deadline" in err_str.lower()
            return ExtractionResult(
                status="timeout" if is_timeout else "error",
                observation=None,
                error_message=f"Vision extraction failed: {err_str}",
                evidence_references=refs,
            )
