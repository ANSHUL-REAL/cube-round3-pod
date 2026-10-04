"""Fake agents for failure/UNCERTAIN/override tests: they return crafted, contract-valid outputs."""
import copy

from orchestration.clients import AgentRejected, AgentTimeout, AgentUnavailable, client_for
from shared.utils.records import PREFIX, build_output, build_record, check


class Fake:
    """Returns a valid output with the given verdict. `calls` counts invocations."""

    def __init__(self, verdict="PASS", needs_human=None, outcome="ok", payload=None, status="completed"):
        self.verdict, self.needs_human, self.outcome, self.payload, self.status, self.calls = verdict, needs_human, outcome, payload, status, 0

    def run(self, request, timeout_s):
        self.calls += 1
        stage = request["stage"]
        checks = [check("c1", self.verdict, None, uncertain_reason="poor_image")]
        rec = build_record(request, agent_id=f"{stage}-fake@1", record_id=f"{PREFIX[stage]}-{request['request_id'].split(':', 1)[1].replace(':', '-')}",
                           captured_at="2026-06-01T00:00:00Z", checks=checks, outcome=self.outcome, reason="fake",
                           model={"name": "fake", "version": "1"}, verdict=self.verdict, needs_human=self.needs_human,
                           payload=self.payload, status=self.status)
        return build_output(rec)


class Boom:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def run(self, request, timeout_s):
        self.calls += 1
        raise self.exc


class Flaky:
    """Fails with `exc` for the first `n` calls, then behaves like Fake(PASS)."""

    def __init__(self, exc, n=2):
        self.exc, self.n, self.calls, self.ok = exc, n, 0, Fake("PASS")

    def run(self, request, timeout_s):
        self.calls += 1
        if self.calls <= self.n:
            raise self.exc
        return self.ok.run(request, timeout_s)


class Mangle:
    """Wraps the real stub and corrupts its output in one specific way."""

    def __init__(self, stage, how):
        self.stage, self.how = stage, how

    def run(self, request, timeout_s):
        out = copy.deepcopy(client_for(self.stage).run(request, timeout_s))
        if self.how == "other_tenant":
            out["evidence"]["subject"]["org_id"] = "org_someone_else"
        elif self.how == "tampered":
            out["evidence"]["checks"] = []          # body changed, hash not
        elif self.how == "wrong_stage":
            out["stage"] = "recovery" if self.stage != "recovery" else "receiving"
        elif self.how == "garbage":
            out = {"hello": "world"}
        elif self.how == "disagree":
            out["verdict"] = "PASS" if out["verdict"] != "PASS" else "FAIL"
        return out


__all__ = ["Fake", "Boom", "Flaky", "Mangle", "AgentRejected", "AgentTimeout", "AgentUnavailable"]
