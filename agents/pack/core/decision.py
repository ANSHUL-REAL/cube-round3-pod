"""Deterministic decision engine.

The model reports what it sees. This module compares that with the order and decides.
It never asks the model for a verdict, so every outcome traces back to named checks.

Per check: PASS when the evidence supports the condition, FAIL when the evidence shows it
is not met, UNCERTAIN when the evidence can't support a reliable judgment.
Box outcome: any FAIL -> STOP_AND_FIX; else any UNCERTAIN -> UNCERTAIN; else SEAL.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .config import Settings
from .models import (
    Catalogue,
    Check,
    Decision,
    DetectedObject,
    Order,
    Perception,
    QualityReport,
    Verdict,
)

LOCAL_GATE_VERSION = "local-quality-gate/1"

# Most useful reason first: a substitution explains a missing line better than the reverse.
_ORDER = ["photo_reuse", "wrong_item", "extra_item", "line_quantity", "line_present", "scene_coverage",
          "image_quality"]


def _priority(check: Check) -> int:
    key = check.check_key.split(":")[0]
    return _ORDER.index(key) if key in _ORDER else len(_ORDER)


@dataclass
class DecisionResult:
    checks: list[Check]
    decision: Decision
    reasons: list[str]
    fix_instructions: list[str]
    observations: dict


def _ref(o: DetectedObject) -> str:
    """How an object is shown to people: #1, #2 ... matching the numbered boxes on the photo."""
    return "#" + o.object_id.lstrip("o")


def _name(o: DetectedObject, catalogue: Catalogue) -> str:
    if o.sku:
        return catalogue.title(o.sku)
    return _short(o.description) or "an unidentified product"


def _short(text: str, limit: int = 60) -> str:
    """A model description made to read well mid-sentence: 'A white box.' -> 'a white box'."""
    text = " ".join(text.split()).rstrip(".")
    first, _, rest = text.partition(" ")
    if first in ("A", "An", "The"):
        text = f"{first.lower()} {rest}"
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _pick_short_line(o: DetectedObject, short: dict[str, int], catalogue: Catalogue) -> str | None:
    """Pair an unexpected product with a line that is short: prefer a known look-alike."""
    open_lines = [s for s, k in short.items() if k > 0]
    if not open_lines:
        return None
    for sku in open_lines:
        item = catalogue.get(sku)
        other = catalogue.get(o.sku) if o.sku else None
        if (item and o.sku in item.confusable_with) or (other and sku in other.confusable_with):
            return sku
    return open_lines[0]


_PHOTO_CHECKS = {"image_quality", "scene_coverage", "photo_reuse"}


def reuse_check(order: Order, earlier_uses: list[dict]) -> tuple[Check, str | None]:
    """Was this exact photo already used for a different order? Returns the check and a hand check.

    The same photo for the same order is a re-check (e.g. after the model timed out), not reuse.
    """
    other = [u for u in earlier_uses if u["order_id"] != order.order_id]
    same = sorted({u["record_id"] for u in earlier_uses if u["order_id"] == order.order_id})
    if other:
        refs = sorted({u["record_id"] for u in other})
        orders = sorted({u["order_id"] for u in other if u["order_id"]})
        check = Check(
            check_key="photo_reuse", verdict=Verdict.UNCERTAIN, confidence=None, model_version=LOCAL_GATE_VERSION,
            detail=f"This exact photo was already used for another order ({', '.join(orders or refs)}), so it can't show this box.",
            evidence={"records": refs},
        )
        return check, "Take a new photo of this box. The photo uploaded was already used for another order."
    detail = "The photo hasn't been used for any other order."
    if same:
        detail += f" It was used for an earlier check of this order ({', '.join(same)})."
    return Check(check_key="photo_reuse", verdict=Verdict.PASS, confidence=1.0, model_version=LOCAL_GATE_VERSION,
                 detail=detail, evidence={"records": same}), None


def _uncertainty(checks: list[Check], needed: list[str], hand_checks: list[str], decision: Decision) -> dict | None:
    """Known / unknown / missing evidence / next action, for any result with unclear checks.

    Built from the checks themselves, so it can't say more than the checks do.
    """
    unclear = [c for c in checks if c.verdict == Verdict.UNCERTAIN]
    if not unclear:
        return None
    photo_only = all(c.check_key.split(":")[0] in _PHOTO_CHECKS for c in unclear)
    if decision == Decision.STOP_AND_FIX:
        action = "Fix the problems listed, then check the unclear items by hand before sealing."
    elif photo_only:
        action = "Retake the photo with the whole box in the frame and items spread out, then check again."
    else:
        action = f"Don't seal yet. {hand_checks[0] if hand_checks else 'Check the box by hand.'} Then record Seal or Stop and fix."
    return {
        "known": [c.detail for c in sorted(checks, key=_priority) if c.verdict in (Verdict.PASS, Verdict.FAIL)],
        "unknown": [c.detail for c in sorted(unclear, key=_priority)],
        "missing_evidence": list(dict.fromkeys(needed)),
        "next_action": action,
    }


def decide(
    order: Order,
    catalogue: Catalogue,
    perception: Perception,
    quality: list[QualityReport],
    settings: Settings,
    earlier_uses: list[dict] | None = None,
) -> DecisionResult:
    """`earlier_uses`: records that used the same photo bytes, when the caller looked them up.
    None means reuse wasn't checked (CLI, eval), and no photo_reuse check is added."""
    tau = settings.match_threshold
    expected = order.expected()
    title = catalogue.title
    checks: list[Check] = []
    hand_checks: list[str] = []
    # What evidence would settle each unclear check, for the uncertainty summary.
    needed: list[str] = []

    def add(key, verdict, conf, detail, evidence=None, local=False):
        checks.append(
            Check(
                check_key=key,
                verdict=verdict,
                confidence=None if conf is None else round(float(conf), 3),
                detail=detail,
                model_version=LOCAL_GATE_VERSION if local else perception.model_version,
                latency_ms=None if local else perception.latency_ms,
                evidence=evidence or {},
            )
        )

    # 1. Photo quality (local gate, before the model).
    failed = [(i, q) for i, q in enumerate(quality, 1) if q.gate == "FAIL"]
    if failed:
        detail = "; ".join(f"Photo {i}: {' '.join(q.reasons)}" for i, q in failed)
        add("image_quality", Verdict.UNCERTAIN, None, detail + " The operator chose to continue.", local=True)
        hand_checks.append("Retake the photo. " + failed[0][1].reasons[0])
        needed.append("A sharp, well-lit photo. " + failed[0][1].reasons[0])
    else:
        add("image_quality", Verdict.PASS, 1.0,
            f"{len(quality)} photo(s) passed the blur, exposure, glare and resolution checks.", local=True)

    # 1b. Is this photo new, or was it already used for another order?
    if earlier_uses is not None:
        check, hand = reuse_check(order, earlier_uses)
        checks.append(check)
        if hand:
            hand_checks.append(hand)
            needed.append("A new photo of this box, taken now.")

    # 2. Can we see the whole box?
    sc = perception.scene
    covered = (
        sc.box_interior_fully_visible
        and not sc.items_may_be_hidden
        and sc.visibility_confidence >= settings.visibility_threshold
    )
    if covered:
        add("scene_coverage", Verdict.PASS, sc.visibility_confidence,
            "The whole inside of the box is visible and nothing appears hidden.")
    else:
        why = []
        if not sc.box_interior_fully_visible:
            why.append("part of the box is out of frame")
        if sc.items_may_be_hidden:
            why.append("items may be stacked or hidden")
        if sc.visibility_confidence < settings.visibility_threshold:
            why.append(f"low visibility confidence ({sc.visibility_confidence:.2f})")
        detail = "; ".join(why).capitalize() + "." + (f" {sc.notes}" if sc.notes else "")
        add("scene_coverage", Verdict.UNCERTAIN, sc.visibility_confidence, detail)
        hand_checks.append("Take a photo that shows the whole inside of the box, with stacked items spread out.")
        if not sc.box_interior_fully_visible:
            needed.append("A photo with the whole inside of the box in the frame.")
        if sc.items_may_be_hidden or sc.visibility_confidence < settings.visibility_threshold:
            needed.append("A view of stacked or covered items: spread them out, or add a photo from an angle.")

    # 3. Sort what the model saw. "Packaging" is only ignored when the model is sure of it: a
    # low-confidence insert, or one it thinks could be a candidate, may be a product under wrap.
    def surely_packaging(o: DetectedObject) -> bool:
        return o.classification == "NON_PRODUCT" and o.confidence >= tau and not o.alternative_skus

    products = [o for o in perception.objects if not surely_packaging(o)]
    inserts = [o for o in perception.objects if surely_packaging(o)]
    confident: dict[str, list[DetectedObject]] = defaultdict(list)
    unknown_confident: list[DetectedObject] = []
    tentative: list[DetectedObject] = []
    for o in products:
        if o.confidence < tau or o.classification == "NON_PRODUCT":
            tentative.append(o)
        elif o.sku:
            confident[o.sku].append(o)
        else:
            unknown_confident.append(o)

    def could_be(o: DetectedObject, sku: str) -> bool:
        # An unclear unidentified product could be anything, including the missing item.
        return o.sku == sku or sku in o.alternative_skus or o.sku is None

    model_counts = {c.sku: c for c in perception.counts}

    component_of: dict[str, str] = {}
    for sku in expected:
        item = catalogue.get(sku)
        for look in item.component_lookalikes if item else []:
            component_of.setdefault(look, sku)

    # 4. Every order line: present? right quantity?
    rows, shortfall, overage, add_up_to = [], {}, {}, {}
    uncatalogued = [s for s in expected if catalogue.get(s) is None]
    for sku, qty in expected.items():
        if sku in uncatalogued:
            # The model was never shown this product, so "not found" means nothing.
            add(f"line_present:{sku}", Verdict.UNCERTAIN, None,
                f"{sku} isn't in the product catalogue, so it can't be recognised in the photo.")
            add(f"line_quantity:{sku}", Verdict.UNCERTAIN, None,
                f"{sku}: {qty} ordered; not counted, because it isn't in the product catalogue.")
            hand_checks.append(f"Check {sku} by hand: there should be {qty}. Add it to the catalogue so it can be checked.")
            needed.append(f"{sku} added to the product catalogue, or a hand check of it.")
            rows.append({"sku": sku, "title": sku, "ordered": qty, "found": 0, "unclear": 0,
                         "present": "UNCERTAIN", "quantity": "UNCERTAIN"})
            continue
        objs = confident.get(sku, [])
        n = len(objs)
        possible = [o for o in tentative if could_be(o, sku)]
        mc = model_counts.get(sku)
        count_certain = mc.count_certain if mc else True
        consistent = mc is None or mc.count == n + sum(1 for o in possible if o.sku == sku)
        confs = [o.confidence for o in objs]
        ids = [o.object_id for o in objs]
        pids = [o.object_id for o in possible]
        name = title(sku)

        if n >= 1:
            pv = Verdict.PASS
            add(f"line_present:{sku}", pv, max(confs), f"{name}: found ({', '.join(_ref(o) for o in objs)}).", {"object_ids": ids})
        elif not possible and covered and not (mc and mc.count > 0):
            pv = Verdict.FAIL
            add(f"line_present:{sku}", pv, sc.visibility_confidence,
                f"{name}: not in the box, and the whole box is visible.")
        else:
            pv = Verdict.UNCERTAIN
            if possible:
                why = f"possible match {', '.join(_ref(o) for o in possible)} is unclear"
            elif covered:
                why = f"the model counted {mc.count} but didn't point to one"
            else:
                why = "not seen, but items may be hidden"
            add(f"line_present:{sku}", pv, max((o.confidence for o in possible), default=None),
                f"{name}: {why}.", {"possible_object_ids": pids})
            feature = catalogue.get(sku).distinguishing_features if catalogue.get(sku) else ""
            needed.append(f"Whether {name} is in the box"
                          + (f": a clear view of {', '.join(_ref(o) for o in possible)}" if possible else "")
                          + (f" showing {feature}" if feature else "") + ".")

        exact = not possible and covered and count_certain and consistent
        if exact and n == qty:
            qv = Verdict.PASS
            add(f"line_quantity:{sku}", qv, min(confs), f"{name}: {n} of {qty}.", {"object_ids": ids})
        elif n > qty:
            qv = Verdict.FAIL
            overage[sku] = n - qty
            add(f"line_quantity:{sku}", qv, min(confs),
                f"{name}: {n} in the box, {qty} ordered ({n - qty} too many).", {"object_ids": ids})
        elif exact or (n + len(possible) < qty and covered and count_certain and consistent):
            qv = Verdict.FAIL
            shortfall[sku] = qty - n
            if possible:
                add_up_to[sku] = True
            conf = min(confs) if confs else sc.visibility_confidence
            found = f"{n}" if not possible else f"at most {n + len(possible)}"
            add(f"line_quantity:{sku}", qv, conf,
                f"{name}: {found} in the box, {qty} ordered.", {"object_ids": ids, "possible_object_ids": pids})
        else:
            qv = Verdict.UNCERTAIN
            parts = [f"{n} clearly counted"]
            if possible:
                parts.append(f"{len(possible)} unclear ({', '.join(_ref(o) for o in possible)})")
            if not covered or not count_certain:
                parts.append("some units may be stacked or hidden")
            if not consistent:
                parts.append(f"the model's own count ({mc.count}) disagrees with the objects it listed")
            add(f"line_quantity:{sku}", qv, min(confs) if confs else None,
                f"{name}: {', '.join(parts)}; {qty} ordered.", {"object_ids": ids, "possible_object_ids": pids})
            hand_checks.append(f"Count {name} by hand: there should be {qty}.")
            needed.append(f"A count of {name} with every unit visible.")

        rows.append({
            "sku": sku, "title": name, "ordered": qty, "found": n, "unclear": len(possible),
            "present": pv.value, "quantity": qv.value,
        })

    # 5. Anything wrong or extra?
    short_left = dict(shortfall)
    wrong_pairs: list[tuple[str, DetectedObject]] = []
    extras: list[DetectedObject] = []
    component_ambiguous: list[tuple[DetectedObject, str]] = []
    maybe_uncatalogued: list[DetectedObject] = []
    parts_left = {parent: expected[parent] for parent in set(component_of.values())}
    non_expected = [o for sku, objs in confident.items() if sku not in expected for o in objs] + unknown_confident
    for o in non_expected:
        if o.sku and o.sku in component_of and parts_left[component_of[o.sku]] > 0:
            # At most one loose part per parent item ordered can be "the part inside it".
            parts_left[component_of[o.sku]] -= 1
            component_ambiguous.append((o, component_of[o.sku]))
            continue
        if o.sku is None and uncatalogued:
            maybe_uncatalogued.append(o)
            continue
        target = _pick_short_line(o, short_left, catalogue)
        if target:
            wrong_pairs.append((target, o))
            short_left[target] -= 1
        else:
            extras.append(o)
    short_left = {s: k for s, k in short_left.items() if k > 0}

    # (object, the non-ordered sku it may be): its best guess, or any alternative it named.
    possible_wrong = []
    for o in tentative:
        other = next((s for s in [o.sku, *o.alternative_skus] if s and s not in expected), None)
        if other:
            possible_wrong.append((o, other))
    possible_extra = [o for o in tentative if o.sku is None]

    for o in tentative:
        guess = title(o.sku) if o.sku else "a product"
        look = catalogue.get(o.sku).distinguishing_features if o.sku and catalogue.get(o.sku) else ""
        hand_checks.append(f"Check {_ref(o)} by hand: it looks like {guess}" + (f" (look for: {look})" if look else "") + ".")

    product_confs = [o.confidence for o in products if o.confidence >= tau]
    clean_conf = min([sc.visibility_confidence] + product_confs)

    if wrong_pairs:
        detail = "; ".join(
            f"Expected {title(exp)}, found {_name(o, catalogue)} ({_ref(o)})" for exp, o in wrong_pairs
        ) + "."
        add("wrong_item", Verdict.FAIL, min(o.confidence for _, o in wrong_pairs), detail,
            {"pairs": [{"expected_sku": e, "found_sku": o.sku, "object_id": o.object_id} for e, o in wrong_pairs]})
    elif possible_wrong:
        detail = "; ".join(
            f"{_ref(o)} may be {title(other)} (confidence {o.confidence:.2f})" for o, other in possible_wrong
        ) + "."
        add("wrong_item", Verdict.UNCERTAIN, max(o.confidence for o, _ in possible_wrong), detail,
            {"object_ids": [o.object_id for o, _ in possible_wrong]})
        for o, other in possible_wrong:
            look = catalogue.get(other).distinguishing_features if catalogue.get(other) else ""
            hand_checks.append(f"Check {_ref(o)} by hand: is it {title(other)}?")
            needed.append(f"A close-up of {_ref(o)}" + (f" showing {look}" if look else " showing its label or colour")
                          + f", to tell whether it is {title(other)}.")
    else:
        add("wrong_item", Verdict.PASS, clean_conf, "No product from outside the order was found.")

    if extras:
        detail = "; ".join(f"{_name(o, catalogue)} ({_ref(o)})" for o in extras)
        add("extra_item", Verdict.FAIL, min(o.confidence for o in extras), f"Not in the order: {detail}.",
            {"object_ids": [o.object_id for o in extras]})
    elif component_ambiguous or possible_extra or maybe_uncatalogued:
        bits = [f"{_name(o, catalogue)} ({_ref(o)}) could be the part that ships inside {title(parent)}"
                for o, parent in component_ambiguous]
        bits += [f"unclear product {_ref(o)}: {_short(o.description)}" for o in possible_extra]
        bits += [f"{_name(o, catalogue)} ({_ref(o)}) could be {' or '.join(uncatalogued)}, which isn't in the catalogue"
                 for o in maybe_uncatalogued]
        add("extra_item", Verdict.UNCERTAIN, None, "; ".join(bits) + ".",
            {"object_ids": [o.object_id for o, _ in component_ambiguous] + [o.object_id for o in possible_extra]
             + [o.object_id for o in maybe_uncatalogued]})
        for o in maybe_uncatalogued:
            hand_checks.append(f"Check {_ref(o)} by hand: is it {' or '.join(uncatalogued)}?")
        for o, parent in component_ambiguous:
            hand_checks.append(f"Check {_ref(o)}: is it loose, or part of the {title(parent)} packaging?")
            needed.append(f"Whether {_ref(o)} is loose or packed inside the {title(parent)} box.")
        for o in possible_extra:
            needed.append(f"A clear view of {_ref(o)} to identify it.")
    else:
        add("extra_item", Verdict.PASS, clean_conf, "Nothing outside the order is in the box.")

    # 6. Box outcome.
    fails = [c for c in checks if c.verdict == Verdict.FAIL]
    uncertain = [c for c in checks if c.verdict == Verdict.UNCERTAIN]
    fixes = [f"Replace {_name(o, catalogue)} ({_ref(o)}) with {title(exp)}." for exp, o in wrong_pairs]
    fixes += [f"Add {'up to ' if add_up_to.get(s) else ''}{k} × {title(s)}." for s, k in short_left.items()]
    fixes += [f"Remove {k} × {title(s)}." for s, k in overage.items()]
    fixes += [f"Remove {_name(o, catalogue)} ({_ref(o)}); it is not in the order." for o in extras]
    hand_checks = list(dict.fromkeys(hand_checks))

    if fails:
        decision = Decision.STOP_AND_FIX
        reasons = [c.detail for c in sorted(fails, key=_priority)]
        instructions = fixes + [f"Also check: {h}" for h in hand_checks]
    elif uncertain:
        decision = Decision.UNCERTAIN
        reasons = [c.detail for c in sorted(uncertain, key=_priority)]
        instructions = hand_checks
    else:
        decision = Decision.SEAL
        reasons = ["Every ordered item is in the box in the right quantity, and nothing else is."]
        instructions = []

    observations = {
        "uncertainty": _uncertainty(checks, needed, hand_checks, decision),
        "expected_vs_observed": rows,
        "detected_items": [
            {**o.model_dump(), "title": title(o.sku) if o.sku else None} for o in perception.objects
        ],
        "missing": [{"sku": s, "title": title(s), "units": k} for s, k in short_left.items()],
        "wrong": [
            {"expected_sku": e, "expected_title": title(e), "found_sku": o.sku,
             "found_title": _name(o, catalogue), "object_id": o.object_id}
            for e, o in wrong_pairs
        ],
        "extra": [{"sku": o.sku, "title": _name(o, catalogue), "object_id": o.object_id} for o in extras],
        "over_quantity": [{"sku": s, "title": title(s), "units": k} for s, k in overage.items()],
        "unclear": [
            {"object_id": o.object_id, "guess_sku": o.sku, "confidence": o.confidence, "description": o.description}
            for o in tentative
        ] + [
            {"object_id": o.object_id, "guess_sku": o.sku, "confidence": o.confidence,
             "description": f"may be part of {title(parent)}"}
            for o, parent in component_ambiguous
        ],
        "non_product_items": [{"object_id": o.object_id, "description": o.description} for o in inserts],
        "scene": sc.model_dump(),
        "image_issues": perception.image_issues,
        "candidates_shown": perception.candidates,
    }
    return DecisionResult(checks, decision, reasons, instructions, observations)
