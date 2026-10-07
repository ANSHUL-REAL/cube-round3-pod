"""Errors raised by the ported Round 2 engine when a reference or prompt cannot be trusted."""
from __future__ import annotations


class VerificationFailed(Exception):
    """A hash-locked file (prompt, reference document) does not match what was recorded for it."""


class MissingReference(Exception):
    """The judgment cannot run (Round 2 §11.2a). `reasons` are stable codes, `missing` says what is absent."""

    def __init__(self, reasons: list[str], missing: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons
        self.missing = missing
