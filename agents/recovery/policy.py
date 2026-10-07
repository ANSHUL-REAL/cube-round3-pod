"""Where Recovery takes a side on the open Round 2 findings (docs/decisions.md, F-07 to F-12).

(F-09 / D-005 is not a switch: a 0.00 line is never claimed, because a claim for $0 is meaningless whichever way
"0.00" is read.) Every switch is here, in one place, so that when the organisers rule on a finding the change is one line and one
test, not a hunt through the rules. Defaults are the conservative side: a wrongly filed claim costs a seller
standing, a missed one costs only money.

The snapshot is copied into every record (`payload.policy`) so a reviewer can see which side a decision took.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Policy:
    # F-11: do Returns records (seller-side inspections) speak to a refund-without-return charge on an FBA unit?
    # Unresolved, so False: Returns evidence may contradict only on merchant-fulfilled units.
    fba_returns_can_contradict: bool = False
    # Prep says compliant but Receiving recorded damage or a quality flag on the unit: the defect may be real.
    # True means: do not claim, and ask a person (conflicting evidence).
    receiving_defect_blocks_claim: bool = True
    # A duplicate is claimed only when the order id is the same too. Without one, two identical lines may be two
    # real units (F-08: the fee report's unit_id is meant to be a single unit, but a line can have quantity 2).
    duplicate_needs_order_id: bool = True
    # Prep evidence taken after the charge was posted cannot show the unit's state when it was inspected.
    evidence_must_precede_charge: bool = True
    # A Prep record covers one unit. A fee line for several units cannot be contradicted by it alone (F-08).
    single_unit_lines_only: bool = True

    def snapshot(self) -> dict:
        return asdict(self)


POLICY = Policy()

# Which open finding a reason code belongs to, so the record can say "this charge was left alone because of F-07".
CODE_FINDING = {
    "no_measurements": "F-07",
    "measurements_not_comparable": "F-07",
    "near_tier_boundary": "F-07",
    "quantity_scope": "F-08",
    "possible_duplicate": "F-08",
    "evidence_identity_mismatch": "F-08",
    "amount_zero": "F-09",
    "amount_missing": "F-09",
    "channel_loss_unevidenced": "F-10",
    "returns_route_unresolved": "F-11",
    "route_unknown": "F-12",
}
