"""The Judgment Agent's exception tools (§11.3): small, typed, strict, budgeted, read-only.

No HTTP, SQL, file, shell or write tools exist. Every tool result goes back as one `function_result`
step whose `result` holds text and, where the tool produces images, image sub-contents (verified against
google-genai 2.25.0: `FunctionResultSubcontent = TextContent | ImageContent`; finding F-010). Errors and
exhausted budgets are returned to the model as `{"error": ...}` so it can finalise with what it has.
"""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageOps

from returns_manager.config import Settings
from returns_manager.llm import context as context_mod
from returns_manager.llm.client import FunctionCall
from returns_manager.llm.context import ContextBundle

CROP_PURPOSES = ["read_label", "read_barcode", "inspect_damage", "check_component", "compare_feature"]
VIEWS = [
    "front",
    "back",
    "left",
    "right",
    "top",
    "bottom",
    "label",
    "packaging",
    "accessories",
    "contents_layout",
]
BUDGET_EXHAUSTED = "budget exhausted — finalize now with the evidence you have"
MAX_SIBLINGS = 2


def tool_definitions() -> list[dict[str, Any]]:
    """Function declarations, sorted by name (stable prefix for caching)."""
    tools = [
        {
            "type": "function",
            "name": "crop_photo_region",
            "description": "Get a full-resolution crop of a region of a return photo, to read a label or "
            "barcode, "
            "inspect damage, check a component or compare a feature. Request all crops you need in one turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "photo": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "box_2d": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "[ymin, xmin, ymax, xmax] normalised to 0-1000",
                    },
                    "purpose": {"type": "string", "enum": CROP_PURPOSES},
                },
                "required": ["photo", "box_2d", "purpose"],
            },
        },
        {
            "type": "function",
            "name": "get_reference_views",
            "description": "Get more reference images of the correct product (views not already provided).",
            "parameters": {
                "type": "object",
                "properties": {"views": {"type": "array", "items": {"type": "string", "enum": VIEWS}}},
                "required": ["views"],
            },
        },
        {
            "type": "function",
            "name": "get_sibling_product",
            "description": "Get the distinguishing features and one reference image of a similar SKU "
            "listed in "
            "the product card's similar_skus.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    ]
    return sorted(tools, key=lambda t: str(t["name"]))


def _jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class ToolRecord:
    seq: int
    round_trip: int
    tool_name: str
    input: dict[str, Any]
    output_summary: dict[str, Any]
    status: str
    latency_ms: int


@dataclass
class ToolExecutor:
    bundle: ContextBundle
    settings: Settings
    crop_budget: int
    crop_resolution: str
    crops_used: int = 0
    reference_calls: int = 0
    siblings_used: set[str] = field(default_factory=set)
    crop_aliases: list[str] = field(default_factory=list)
    reference_aliases: list[str] = field(default_factory=list)
    records: list[ToolRecord] = field(default_factory=list)

    def execute_all(self, calls: list[FunctionCall], round_trip: int) -> list[dict[str, Any]]:
        """Run every call of one model turn; return one function_result step per call (same order)."""
        steps = []
        for call in calls:
            started = time.perf_counter()
            try:
                result, summary, status = self._dispatch(call)
            except Exception as exc:  # a tool bug must not end the session; the model gets an error
                result, summary, status = (
                    [self._text({"error": f"tool failed: {type(exc).__name__}"})],
                    {"error": type(exc).__name__},
                    "error",
                )
            self.records.append(
                ToolRecord(
                    seq=len(self.records) + 1,
                    round_trip=round_trip,
                    tool_name=call.name,
                    input=dict(call.arguments),
                    output_summary=summary,
                    status=status,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            step: dict[str, Any] = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": result,
            }
            if status != "ok":
                step["is_error"] = True
            steps.append(step)
        return steps

    @staticmethod
    def _text(payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "text", "text": json.dumps(payload, sort_keys=True, ensure_ascii=False)}

    def _image(self, b64: str, resolution: str) -> dict[str, Any]:
        return {"type": "image", "data": b64, "mime_type": "image/jpeg", "resolution": resolution}

    def _dispatch(self, call: FunctionCall) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if call.name == "crop_photo_region":
            return self._crop(call.arguments)
        if call.name == "get_reference_views":
            return self._reference_views(call.arguments)
        if call.name == "get_sibling_product":
            return self._sibling(call.arguments)
        return [self._text({"error": f"unknown tool {call.name}"})], {"error": "unknown_tool"}, "error"

    def _crop(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if self.crops_used >= self.crop_budget:
            return [self._text({"error": BUDGET_EXHAUSTED})], {"error": "crop_budget"}, "budget_exhausted"
        photo = next((p for p in self.bundle.photos if p.alias == args.get("photo")), None)
        box = args.get("box_2d")
        if photo is None:
            return [self._text({"error": "unknown photo alias"})], {"error": "unknown_photo"}, "error"
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(isinstance(v, int) and 0 <= v <= 1000 for v in box)
            or box[0] >= box[2]
            or box[1] >= box[3]
        ):
            return (
                [self._text({"error": "box_2d must be [ymin, xmin, ymax, xmax], integers 0-1000"})],
                {"error": "bad_box"},
                "error",
            )
        with Image.open(io.BytesIO(photo.original_bytes)) as raw:
            img = ImageOps.exif_transpose(raw) or raw
            w, h = img.size
            ymin, xmin, ymax, xmax = box
            crop = img.crop(
                (xmin * w // 1000, ymin * h // 1000, max(xmax * w // 1000, 1), max(ymax * h // 1000, 1))
            )
            longest = max(crop.size)
            if longest > self.settings.rm_crop_max_edge:
                scale = self.settings.rm_crop_max_edge / longest
                crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))))
            b64 = _jpeg_b64(crop)
        self.crops_used += 1
        alias = f"C{self.crops_used}"
        self.crop_aliases.append(alias)
        meta = {"crop": alias, "of": photo.alias, "box_2d": box, "purpose": args.get("purpose")}
        return [self._text(meta), self._image(b64, self.crop_resolution)], meta, "ok"

    def _reference_views(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if self.reference_calls >= 1:
            return (
                [self._text({"error": BUDGET_EXHAUSTED})],
                {"error": "reference_budget"},
                "budget_exhausted",
            )
        self.reference_calls += 1
        wanted = [v for v in args.get("views") or [] if v in VIEWS]
        sent = set(self.bundle.initial_reference_aliases) | set(self.reference_aliases)
        found = [r for r in self.bundle.references if r.view in wanted and r.alias not in sent]
        if not found:
            return (
                [self._text({"error": "no further reference views of those kinds exist"})],
                {"found": []},
                "ok",
            )
        items: list[dict[str, Any]] = [
            self._text({"references": [{"alias": r.alias, "view": r.view} for r in found]})
        ]
        for r in found:
            self.reference_aliases.append(r.alias)
            items.append(
                self._image(
                    base64.b64encode(r.data).decode("ascii"), self.settings.rm_reference_photo_resolution
                )
            )
        return items, {"found": [r.alias for r in found]}, "ok"

    def _sibling(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        sku = str(args.get("sku") or "")
        card = self.bundle.ctx.card
        if sku not in {s.sku for s in card.similar_skus}:
            return [self._text({"error": "sku is not in similar_skus"})], {"error": "not_a_sibling"}, "error"
        if sku not in self.siblings_used and len(self.siblings_used) >= MAX_SIBLINGS:
            return [self._text({"error": BUDGET_EXHAUSTED})], {"error": "sibling_budget"}, "budget_exhausted"
        self.siblings_used.add(sku)
        sibling = self.bundle.ctx.other_cards.get(sku)
        differs = next(s.differs_by for s in card.similar_skus if s.sku == sku)
        if sibling is None:
            note = {"sku": sku, "differs_by": differs, "note": "no product card on file for this sibling"}
            return [self._text(note)], {"sku": sku, "card": False}, "ok"
        payload: dict[str, Any] = {
            "sku": sku,
            "title": sibling.title,
            "differs_by": differs,
            "distinguishing_features": [f.model_dump() for f in sibling.distinguishing_features],
        }
        items = [self._text(payload)]
        if sibling.reference_images:
            ref = sibling.reference_images[0]
            path = context_mod.PRODUCTS_DIR / sibling.org_id / ref.path
            if path.is_file():
                alias = f"R{len(self.bundle.references) + len(self.reference_aliases) + 1}"
                self.reference_aliases.append(alias)
                items[0] = self._text({**payload, "reference_image": alias})
                items.append(
                    self._image(
                        base64.b64encode(path.read_bytes()).decode("ascii"),
                        self.settings.rm_reference_photo_resolution,
                    )
                )
        return items, {"sku": sku, "card": True}, "ok"
