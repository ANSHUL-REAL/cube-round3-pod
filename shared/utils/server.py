"""Tiny FastAPI factory so any Python agent can expose the contract in a few lines.

    from shared.utils.server import make_app
    app = make_app("receiving", handle)        # uvicorn agents.receiving.app:app

`handle(agent_input) -> agent_output`. Non-Python agents: implement the same two endpoints
(see shared/contracts/agent-api.md).
"""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from .records import pending_output
from .schema import errors

CONTRACT_VERSION = "1.0"


def make_app(stage: str, handle: Callable[[dict], dict], version: str = "0.0.0") -> FastAPI:
    app = FastAPI(title=f"CUBE {stage} agent", version=version)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "stage": stage, "version": version, "contract_version": CONTRACT_VERSION}

    @app.post("/run")
    async def run(request: Request) -> dict:
        body = await request.json()
        problems = errors("agent-input", body)
        if problems or body.get("stage") != stage:
            raise HTTPException(status_code=422, detail=problems or [f"stage must be '{stage}'"])
        try:
            return handle(body)
        except LookupError as exc:  # unknown subject / wrong tenant
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # fail open: always return an output
            return pending_output(body, code="agent_exception", message=str(exc))

    return app
