"""The deterministic decision core: what the model SAW in, verdicts out. Same observations, same verdicts.

Ported from the Round 2 reference (rules.ts) and fitted to the Round 3 contract:

* verdicts are PASS / FAIL / UNCERTAIN only. A check that does not apply to the unit is **omitted** (the reference
  emitted NOT_APPLICABLE), and one that no photo can judge (bag thickness) is reported in the payload, not as a check
  (the reference emitted NOT_VERIFIABLE).
* the reference's eleven checks roll up (worst verdict wins) into the contract's check_keys. Nothing is lost: the
  eleven are kept whole in ``payload.rule_checks``.
* ALL failed checks are reported (the reference named only the first).
* a PASS or FAIL needs a cited, existing, usable photo and visible evidence, at least medium confidence. Anything else is
  UNCERTAIN with a reason from the contract's list. No evidence is ever invented to fill a check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.utils.records import check as make_check
from shared.utils.records import rollup

from .requirements import CONTRACT_KEYS, DEMO_TAG, EXTRA_KEYS, CheckDef, RequirementPack, norm_mark
from .vision import Observation, VisionResponse

# The model reports an ordinal band, not a probability. These numbers are a fixed mapping of that band, NOT a
# calibrated probability: nobody has measured how often "high" is right (see README, Limits).
BAND_VALUE = {"high": 0.9, "medium": 0.6, "low": 0.3}
_RANK = {"PASS": 0, "UNCERTAIN": 1, "FAIL": 2}
_POOR = {"blurry", "glare", "too_dark", "low_resolution"}
_OCCLUDED = {"cropped", "out_of_frame", "occluded"}

OUTCOME = {"PASS": "compliant", "FAIL": "non_compliant", "UNCERTAIN": "pending_review"}


@dataclass
class RuleCheck:
    """One of the reference's eleven checks, decided."""

    check_id: str
    contract_key: str
    verdict: str
    band: str | None
    expected: str
    observed: str
    detail: str
    photo_index: int | None = None
    location: str = ""
    uncertain_reason: str | None = None
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float | None:
        """Confidence in THIS verdict. An UNCERTAIN verdict carries none: the band describes the observation."""
        return BAND_VALUE.get(self.band) if self.band and self.verdict != "UNCERTAIN" else None

    def as_dict(self) -> dict:
        return {"check_id": self.check_id, "contract_key": self.contract_key, "verdict": self.verdict,
                "confidence": self.confidence, "expected": self.expected, "observed": self.observed,
                "detail": self.detail, "photo_index": self.photo_index, "location": self.location or None,
                "uncertain_reason": self.uncertain_reason, "evidence_refs": self.evidence_refs}


def _normalise_code(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum()).upper()


def _ref(idx: int | None, refs: list[str]) -> str | None:
    return refs[idx - 1] if idx is not None and 1 <= idx <= len(refs) else None


def _unc(c: CheckDef, reason: str, detail: str, *, refs: list[str], idx=None, loc="", band=None, observed=None):
    return RuleCheck(c.check_id, c.contract_key, "UNCERTAIN", band, c.requirement, observed or detail, detail, idx,
                     loc, reason, refs)


def _limit_reason(limit: str | None, photo_unusable: bool) -> str:
    if photo_unusable or limit in _POOR:
        return "poor_image"
    if limit in _OCCLUDED:
        return "occluded"
    return "insufficient_evidence"


def _gate(c: CheckDef, o: Observation | None, usable: dict[int, bool], refs: list[str]):
    """The conditions every observation-driven PASS/FAIL must clear. Returns (UNCERTAIN RuleCheck | None, photo ref)."""
    if o is None:
        return _unc(c, "insufficient_evidence", "The model returned no observation for this check.", refs=refs), None
    ref = _ref(o.photo_index, refs)
    unusable = o.photo_index is not None and usable.get(o.photo_index) is False
    where = ref and [ref] or refs
    common = dict(refs=where, idx=o.photo_index, loc=o.location, band=o.confidence)
    if o.status == "cant_tell":
        return _unc(c, _limit_reason(o.limit, unusable), o.evidence or "Not determinable from the photos provided.",
                    **common), None
    if o.photo_index is None:
        return _unc(c, "insufficient_evidence", "The model gave a verdict but cited no photo.", **common), None
    if ref is None:
        return _unc(c, "insufficient_evidence", f"The model cited photo {o.photo_index}, which does not exist.",
                    refs=refs, band=o.confidence), None
    if unusable:
        return _unc(c, "poor_image", f"Photo {o.photo_index} is not usable for this check"
                    + (f": {o.evidence}" if o.evidence else ". Retake it."), **common), None
    if not o.evidence.strip():
        return _unc(c, "insufficient_evidence", "No visible evidence was cited, so it is treated as uncertain.",
                    **common), None
    if o.confidence == "low":
        return _unc(c, "insufficient_evidence", f"Low-confidence observation: {o.evidence}", **common), None
    return None, ref


def _decide(c: CheckDef, o: Observation, ref: str) -> RuleCheck:
    verdict = "PASS" if o.status == "met" else "FAIL"
    return RuleCheck(c.check_id, c.contract_key, verdict, o.confidence, c.requirement, o.evidence, o.evidence,
                     o.photo_index, o.location, None, [ref])


def _handling(c: CheckDef, o: Observation | None, pack: RequirementPack, usable, refs) -> RuleCheck:
    early, ref = _gate(c, o, usable, refs)
    if early:
        return early
    required = sorted(pack.handling_marks)
    seen = sorted({norm_mark(m) for m in o.marks_visible if str(m).strip()})
    missing = [m for m in required if m not in seen]
    expected = ", ".join(required)
    observed = ", ".join(seen) or "none"
    if missing:
        return RuleCheck(c.check_id, c.contract_key, "FAIL", o.confidence, expected, observed,
                         f"Required handling mark(s) not seen: {', '.join(missing)}. Seen: {observed}. {o.evidence}",
                         o.photo_index, o.location, None, [ref])
    if o.status == "not_met":
        return _unc(c, "conflicting_evidence", "The model said the requirement is not met but listed every required "
                    f"mark ({observed}).", refs=[ref], idx=o.photo_index, loc=o.location, band=o.confidence)
    return RuleCheck(c.check_id, c.contract_key, "PASS", o.confidence, expected, observed,
                     f"All required handling mark(s) seen: {observed}. {o.evidence}", o.photo_index, o.location, None,
                     [ref])


def _text_match(c: CheckDef, pack: RequirementPack, resp: VisionResponse, usable, refs) -> RuleCheck:
    lr = resp.label_text_read
    ref = _ref(lr.photo_index, refs)
    expected = f"label reads {pack.fnsku}"
    where = [ref] if ref else refs
    base = dict(refs=where, idx=lr.photo_index, loc="barcode label", band=lr.confidence)
    if not lr.legible or not (lr.value or "").strip():
        unusable = lr.photo_index is not None and usable.get(lr.photo_index) is False
        return _unc(c, "poor_image" if unusable else "insufficient_evidence",
                    "The label text could not be read. Photograph the label in sharp focus.", **base)
    if ref is None:
        return _unc(c, "insufficient_evidence", "The label was read but no existing photo was cited.", refs=refs,
                    band=lr.confidence)
    if usable.get(lr.photo_index) is False:
        return _unc(c, "poor_image", f"Photo {lr.photo_index} is not usable, so the label text is not trusted.", **base)
    if lr.confidence == "low":
        return _unc(c, "insufficient_evidence", f'Read "{lr.value}" with low confidence. Re-photograph the label.',
                    **base)
    if _normalise_code(lr.value) == _normalise_code(pack.fnsku or ""):
        return RuleCheck(c.check_id, c.contract_key, "PASS", lr.confidence, expected, lr.value,
                         f'The label reads "{lr.value}", which matches the expected code.', lr.photo_index,
                         "barcode label", None, [ref])
    if lr.confidence != "high":
        # A mismatch is a FAIL that a person will act on; a model that is only "medium" sure of its transcription
        # may simply have misread a character, so it goes to a person instead.
        return _unc(c, "insufficient_evidence", f'Read "{lr.value}", which differs from the expected code, but the '
                    "transcription is not high-confidence: a possible misread. Re-photograph the label.", **base)
    return RuleCheck(c.check_id, c.contract_key, "FAIL", lr.confidence, expected, lr.value,
                     f'The label reads "{lr.value}", which does not match the expected code (mislabelled unit).',
                     lr.photo_index, "barcode label", None, [ref])


def evaluate(pack: RequirementPack, resp: VisionResponse, refs: list[str]) -> list[RuleCheck]:
    """Decide every applicable, photo-verifiable check. ``refs[i]`` is the capture the model called photo i+1."""
    usable = {q.photo_index: q.usable for q in resp.photo_quality}
    by_check: dict[str, list[Observation]] = {}
    for o in resp.observations:
        by_check.setdefault(o.check_id, []).append(o)

    out: list[RuleCheck] = []
    for c in pack.active():
        if not c.verifiable:
            continue
        if c.check_id == "fnsku_text_match":
            out.append(_text_match(c, pack, resp, usable, refs))
            continue
        seen = by_check.get(c.check_id, [])
        if len({o.status for o in seen}) > 1:  # the model contradicted itself: that is not evidence
            out.append(_unc(c, "conflicting_evidence", "The model gave conflicting observations for this check.",
                            refs=refs))
            continue
        o = seen[0] if seen else None
        if c.check_id == "handling_marks_present":
            out.append(_handling(c, o, pack, usable, refs))
            continue
        early, ref = _gate(c, o, usable, refs)
        out.append(early or _decide(c, o, ref))
    return out


def _worst(verdicts: list[str]) -> str:
    return max(verdicts, key=_RANK.__getitem__)


def to_contract_checks(rule_checks: list[RuleCheck]) -> list[dict]:
    """Roll the eleven checks up into the contract's check_keys (worst verdict wins). Keys with no member are omitted."""
    out: list[dict] = []
    for key in (*CONTRACT_KEYS, *EXTRA_KEYS):
        group = [r for r in rule_checks if r.contract_key == key]
        if not group:
            continue
        verdict = _worst([r.verdict for r in group])
        unclear = [r for r in group if r.verdict != "PASS"]
        shown = unclear or group
        conf = [r.confidence for r in group if r.confidence is not None]
        refs: list[str] = []
        for r in group:
            refs += [x for x in r.evidence_refs if x not in refs]
        reason = next((r.uncertain_reason for r in group if r.verdict == "UNCERTAIN"), None)
        observed = {r.check_id: r.observed for r in group}
        out.append(make_check(
            key, verdict, round(min(conf), 3) if conf else None,
            expected=" ".join(dict.fromkeys(r.expected for r in group)),
            observed=next(iter(observed.values())) if len(observed) == 1 else observed,
            detail=" ".join(r.detail for r in shown).strip() + f" {DEMO_TAG}",
            evidence_refs=refs, uncertain_reason=reason if verdict == "UNCERTAIN" else None))
    return out


def decide(contract_checks: list[dict]) -> tuple[str, str]:
    """(verdict, outcome) from the contract checks, using the contract's own roll-up."""
    verdict = rollup(contract_checks)
    return verdict, OUTCOME[verdict]


def summarise(contract_checks: list[dict]) -> str:
    """One sentence that names EVERY failed and every uncertain check (the reference named only the first)."""
    failed = [c for c in contract_checks if c["verdict"] == "FAIL"]
    unsure = [c for c in contract_checks if c["verdict"] == "UNCERTAIN"]
    parts: list[str] = []
    if failed:
        parts.append(f"{len(failed)} check(s) failed: " + "; ".join(c["check_key"] for c in failed))
    if unsure:
        parts.append(f"{len(unsure)} check(s) could not be confirmed: " + "; ".join(
            f"{c['check_key']} ({c.get('uncertain_reason')})" for c in unsure))
    if not parts:
        return f"All {len(contract_checks)} required check(s) passed with cited visual evidence."
    return ". ".join(parts) + "."
