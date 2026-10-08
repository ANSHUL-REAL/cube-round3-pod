"""Exit codes (build prompt §20) and the exception types that map onto them."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    FAILURE = 1
    USAGE = 2
    VERIFICATION_FAILED = 3
    SPEND_GUARD_REFUSED = 4
    CONFIG_INVALID = 5


class ReturnsManagerError(Exception):
    """Base class. `exit_code` tells the CLI how to exit when this escapes a command."""

    exit_code: ExitCode = ExitCode.FAILURE


class ConfigError(ReturnsManagerError):
    """Configuration is missing or invalid. The message never contains secret values."""

    exit_code = ExitCode.CONFIG_INVALID


class VerificationFailed(ReturnsManagerError):
    exit_code = ExitCode.VERIFICATION_FAILED


class SpendGuardRefused(ReturnsManagerError):
    exit_code = ExitCode.SPEND_GUARD_REFUSED


class NotFound(ReturnsManagerError):
    """The resource does not exist for this caller. Other orgs' resources are reported the same way (404)."""


class BadRequest(ReturnsManagerError):
    """Malformed or invalid request data."""

    exit_code = ExitCode.USAGE


class Conflict(ReturnsManagerError):
    """State conflict, such as invalid status transition or constraint violation."""

    exit_code = ExitCode.FAILURE


class QualityGateRefusal(Conflict):
    """Quality gate rejected submission because <2 non-fail photos without acknowledgement."""


class PayloadTooLarge(ReturnsManagerError):
    """Request payload exceeded maximum allowed size (e.g. 20 MB photo cap)."""

    exit_code = ExitCode.USAGE


class UnsupportedMediaType(ReturnsManagerError):
    """Unsupported payload format or media type."""

    exit_code = ExitCode.USAGE


class InvalidTransitionError(Conflict):
    """An illegal state machine transition was attempted."""

    def __init__(self, entity: str, current: str | None, target: str, trigger: str | None = None) -> None:
        trig_str = f" via trigger '{trigger}'" if trigger else ""
        super().__init__(f"Invalid {entity} transition from '{current}' to '{target}'{trig_str}")
        self.entity = entity
        self.current = current
        self.target = target
        self.trigger = trigger


class CircuitOpenError(ReturnsManagerError):
    """Circuit breaker is open for the model; calls temporarily blocked."""

    exit_code = ExitCode.FAILURE


class QuotaExhaustedError(ReturnsManagerError):
    """Model request quota exhausted for today."""

    exit_code = ExitCode.SPEND_GUARD_REFUSED


class HandlerNotConfigured(ReturnsManagerError):
    """The worker has no handler for a job kind (the Judgment Agent is wired in P5). Never retried."""

    exit_code = ExitCode.CONFIG_INVALID


class NotBuiltYet(ReturnsManagerError):
    """A command that exists in the CLI tree but whose phase has not been built yet."""

    exit_code = ExitCode.FAILURE

    def __init__(self, command: str, phase: str) -> None:
        super().__init__(f"`{command}` is not built yet (planned for phase {phase}).")
        self.command = command
        self.phase = phase
