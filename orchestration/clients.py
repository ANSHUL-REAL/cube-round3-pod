"""How the orchestrator talks to an agent: in-process (Python) or HTTP (any language)."""
from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class AgentUnavailable(Exception):
    """Connection / 5xx: worth retrying."""


class AgentTimeout(AgentUnavailable):
    """No answer in time: worth retrying."""


class AgentRejected(Exception):
    """4xx: the request or tenancy was refused. Retrying will not help."""


def load_manifest(stage: str) -> dict:
    return json.loads((ROOT / "agents" / stage / "agent.json").read_text())


class InProcClient:
    def __init__(self, manifest: dict):
        self.handle = importlib.import_module(manifest["module"]).handle

    def run(self, request: dict, timeout_s: float) -> dict:
        """Run the agent in a worker thread and give up after timeout_s.

        A Python thread cannot be killed, so a timed-out agent keeps running in the background until it returns; its
        answer is discarded. That is why the thread is a daemon: a hung agent must not stop the process from exiting.
        """
        box: dict = {}

        def target() -> None:
            try:
                box["out"] = self.handle(request)
            except BaseException as exc:  # handed back to the caller's thread
                box["exc"] = exc

        worker = threading.Thread(target=target, name=f"agent-{request.get('stage')}", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            raise AgentTimeout(f"no answer within {timeout_s:g} s (in-process agent, still running)")
        exc = box.get("exc")
        if exc is None:
            return box["out"]
        # KeyError and IndexError are LookupErrors, but they mean a bug in the agent, not "unknown subject / wrong tenant".
        if isinstance(exc, LookupError) and not isinstance(exc, (KeyError, IndexError)):
            raise AgentRejected(str(exc)) from exc
        raise exc


class HttpClient:
    def __init__(self, manifest: dict):
        env = f"{manifest['stage'].upper()}_URL"
        self.url = os.environ.get(env, manifest["url"]).rstrip("/")

    def run(self, request: dict, timeout_s: float) -> dict:
        try:
            resp = httpx.post(f"{self.url}/run", json=request, timeout=timeout_s)
        except httpx.ConnectTimeout as exc:
            raise AgentUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise AgentTimeout(f"{type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AgentUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if 400 <= resp.status_code < 500:
            raise AgentRejected(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 500:
            raise AgentUnavailable(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def health(self) -> dict:
        return httpx.get(f"{self.url}/health", timeout=5).json()


def client_for(stage: str):
    manifest = load_manifest(stage)
    mode = os.environ.get("ORCH_MODE") or manifest["mode"]
    return InProcClient(manifest) if mode == "inproc" else HttpClient(manifest)
