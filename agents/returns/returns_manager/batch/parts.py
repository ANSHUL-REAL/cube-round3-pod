"""Parses the flat `parts_list` column (e.g. `lamp;usb cable;manual`) into product-card
components, and classifies them by the confirmed convention (build-log, batch import):

- The first part listed is the root/main part: essential, non-replaceable. Missing -> the
  disposition engine's rule R10 (liquidate/dispose) applies, not R09 - a missing main part
  is never just "refurbish".
- Every other part listed is a side/accessory part: essential, replaceable. Missing -> rule
  R09 applies (refurbish, if the net gain clears the threshold; otherwise R10). This is the
  existing, unmodified disposition engine (finding F-012): a missing part is never routed
  straight to `restock`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_QTY_RE = re.compile(r"^(?P<name>.*\S)\s+x(?P<qty>\d+)$", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ParsedPart:
    component_id: str
    name: str
    quantity: int
    essential: bool
    replaceable: bool


def _slugify(name: str, taken: set[str]) -> str:
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_") or "part"
    candidate = slug
    n = 2
    while candidate in taken:
        candidate = f"{slug}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def parse_parts_list(parts_list: str) -> list[ParsedPart]:
    """`;`-separated tokens, each `name` or `name xN` (quantity). Empty/blank input -> []."""
    tokens = [t.strip() for t in parts_list.split(";") if t.strip()]
    taken: set[str] = set()
    parts: list[ParsedPart] = []
    for i, token in enumerate(tokens):
        m = _QTY_RE.match(token)
        name = m.group("name").strip() if m else token
        quantity = int(m.group("qty")) if m else 1
        parts.append(
            ParsedPart(
                component_id=_slugify(name, taken),
                name=name,
                quantity=max(quantity, 1),
                essential=True,
                replaceable=i != 0,  # first listed = main/root part, non-replaceable
            )
        )
    return parts
