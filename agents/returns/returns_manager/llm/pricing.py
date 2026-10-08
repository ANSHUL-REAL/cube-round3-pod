"""Paid-equivalent cost (§8.8, §18.3). Actual spend on the free tier is $0; this is what the same tokens would
cost on the paid Standard tier, from `reference/pricing/gemini.yaml`, so unit economics stay honest.

Method: cost_usd_micros = (input - cached)*p_in + cached*p_cached + (output + thought)*p_out, where p is
USD per million tokens (so tokens * p is micro-USD). Thinking tokens are billed as output.
`total_input_tokens` is taken to include cached tokens (the provider reports them inside the prompt count);
if it does not, the cached share is under-priced, never over-priced. Integer micro-dollars; rounding only
at display time.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from returns_manager.config import REPO_ROOT
from returns_manager.reference.models import ModelPricingV1

PRICING_FILE = REPO_ROOT / "reference" / "pricing" / "gemini.yaml"
LABEL = "paid-equivalent (free tier used)"


@lru_cache(maxsize=2)
def load_pricing(path: Path = PRICING_FILE) -> ModelPricingV1:
    return ModelPricingV1.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def cost_usd_micros(
    model_id: str, usage: dict[str, int], pricing: ModelPricingV1 | None = None
) -> int | None:
    """Paid-equivalent cost of one request in integer micro-USD, or None if the model has no listed price."""
    table = (pricing or load_pricing()).per_million_tokens.get(model_id)
    if table is None:
        return None
    p_in = Decimal(str(table["input"]))
    p_out = Decimal(str(table["output"]))
    p_cached = Decimal(str(table.get("cached_input", table["input"])))
    total_in = usage.get("total_input_tokens", 0)
    cached = min(usage.get("total_cached_tokens", 0), total_in)
    out = usage.get("total_output_tokens", 0) + usage.get("total_thought_tokens", 0)
    micros = (total_in - cached) * p_in + cached * p_cached + out * p_out
    return int(micros.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
