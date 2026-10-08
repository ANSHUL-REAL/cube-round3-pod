"""Read-only MCP server for cross-pod agent access (§16).

Served as its own process: returns-manager mcp serve
Authenticated by API key (Authorization: Bearer rmk_<env>_...), scope evidence:read.
The key's org scopes every call: org_id is never accepted as a tool argument.

Tools (annotated read-only):
  - get_return_evidence(unit_id, version?, include_pending?)
  - list_return_evidence(since?, limit?)
  - verify_return_chain(unit_id)
  - explain_return_decision(unit_id, question)

Tool output is built from the same service functions as REST (byte-identical after JCS;
see T-CON-13). No write tools. Least privilege for agent-to-agent traffic.

Note on the SDK: this targets the installed `mcp` package's v2 API
(`mcp.server.mcpserver.MCPServer`, formerly `FastMCP` in v1). The v2 OAuth-oriented
`TokenVerifier`/`AuthSettings` machinery assumes a real authorization server (issuer,
protected-resource metadata, RFC 8707 resource indicators) which does not describe our
static API keys, so authentication is done ourselves with a thin Starlette middleware
that resolves the bearer token through `security.api_keys.authenticate` — the same
function the REST API uses — and stores the resulting `Principal` in a contextvar for
the duration of the request.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from returns_manager.security.roles import Permission, Principal, require

logger = logging.getLogger(__name__)

# Set by the auth middleware for the lifetime of one HTTP request/connection; read by
# each tool. There is no default: a tool invoked with nothing in context is a bug in
# the auth wiring, not an unauthenticated caller, and must fail loudly rather than
# silently falling back to some "no org" scope.
_principal_var: contextvars.ContextVar[Principal] = contextvars.ContextVar("rm_mcp_principal")


def _current_principal() -> Principal:
    try:
        return _principal_var.get()
    except LookupError as exc:  # pragma: no cover - indicates a wiring bug, not user error
        raise RuntimeError(
            "no authenticated principal in context: the API-key auth middleware did not run"
        ) from exc


async def authenticate_bearer_token(db: Any, authorization_header: str | None, *, env: str) -> Principal:
    """Resolve an `Authorization: Bearer <api key>` header to a Principal with evidence:read.

    Raises `Unauthenticated` (missing/malformed header, unknown/revoked/expired key) or
    `Forbidden` (valid key, wrong scope). Factored out of the ASGI middleware so it can be
    unit-tested without standing up Starlette/uvicorn.
    """
    from returns_manager.security import api_keys
    from returns_manager.security.roles import Unauthenticated

    scheme, _, token = (authorization_header or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("expected 'Authorization: Bearer <api key>'")
    principal = await api_keys.authenticate(db, token.strip(), env=env)
    if principal is None:
        raise Unauthenticated("invalid, revoked or expired API key")
    require(principal, Permission.EVIDENCE_READ)
    return principal


def _build_mcp_server(settings_override: Any = None) -> Any:
    """Build the MCPServer instance and register the four read-only tools.

    Import is deferred so that the MCP package is only required when this
    entry point is actually invoked (not at import time of the whole package).
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required for the MCP server. Install it with: uv add mcp"
        ) from exc

    from returns_manager.config import get_settings
    from returns_manager.db.pool import Database

    settings = settings_override or get_settings()
    settings.require("database_url")
    assert settings.database_url is not None

    mcp: Any = MCPServer(
        name="returns-manager",
        description=(
            "Read-only MCP server for Returns Manager evidence records. "
            "Every call is scoped to the org of the API key used to authenticate; "
            "org_id is never accepted as a tool argument."
        ),
    )

    db = Database(settings.database_url.get_secret_value())
    mcp.rm_db = db
    mcp.rm_env = settings.rm_env

    # ── Tool: get_return_evidence ─────────────────────────────────────────────

    @mcp.tool(description="Get the evidence record for a unit_id, in the caller's org.")  # type: ignore[untyped-decorator]
    async def get_return_evidence(
        unit_id: str,
        version: int | None = None,
        include_pending: bool = False,
    ) -> dict[str, Any]:
        """Return the evidence record document for unit_id in the authenticated caller's org.

        Returns the latest non-superseded version unless version is specified.
        Returns an empty dict {} when no record exists (the caller should treat
        this as 'silent' per §14.4).
        """
        from returns_manager.contract.service import get_evidence_document

        principal = _current_principal()
        doc = await get_evidence_document(
            db.pool,
            org_id=principal.org_id,
            unit_id=unit_id,
            version=version,
            include_pending=include_pending,
        )
        return doc if doc is not None else {}

    # ── Tool: list_return_evidence ────────────────────────────────────────────

    @mcp.tool(description="List evidence records for the caller's org, optionally filtered by timestamp.")  # type: ignore[untyped-decorator]
    async def list_return_evidence(
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return finalized evidence records for the authenticated caller's org.

        `since` is an ISO 8601 UTC timestamp (inclusive).
        `limit` caps the number of results (max 100).
        """
        from datetime import datetime

        from returns_manager.contract.service import export_evidence_stream

        principal = _current_principal()

        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                since_dt = None

        records = await export_evidence_stream(
            db.pool,
            org_id=principal.org_id,
            since=since_dt,
        )
        capped = min(max(1, limit), 100)
        return records[:capped]

    # ── Tool: verify_return_chain ─────────────────────────────────────────────

    @mcp.tool(description="Verify the event chain integrity for a unit in the caller's org.")  # type: ignore[untyped-decorator]
    async def verify_return_chain(unit_id: str) -> dict[str, Any]:
        """Recompute and verify the event chain for unit_id in the authenticated caller's org.

        Returns a verification summary dict with 'valid: bool' and 'summary' string.
        """
        from returns_manager.chain.service import run_verify_unit

        principal = _current_principal()
        result = await run_verify_unit(
            db.pool,
            principal.org_id,
            unit_id,
            "anchors/ledger-anchors.jsonl",
        )
        return {
            "org_id": principal.org_id,
            "unit_id": unit_id,
            "valid": result.valid,
            "units_checked": result.units_checked,
            "events_checked": result.events_checked,
            "ledger_entries_checked": result.ledger_entries_checked,
            "records_checked": result.records_checked,
            "anchors_checked": result.anchors_checked,
            "first_hash": result.first_hash,
            "last_hash": result.last_hash,
            "failures": result.failures,
            "summary": result.summary_line(),
        }

    # ── Tool: explain_return_decision ─────────────────────────────────────────

    @mcp.tool(  # type: ignore[untyped-decorator]
        description=(
            "Answer a natural-language question about why a return decision was made, "
            "for a unit in the caller's org. Read-only; never proposes new verdicts."
        )
    )
    async def explain_return_decision(unit_id: str, question: str) -> dict[str, Any]:
        """Answer `question` about the decision for unit_id using the evidence record.

        Returns {answer, citations, not_recorded}. Delegates to `ExplainerService`
        (§11.14, built in P14): every fact and citation is grounded in this unit's own
        evidence document and event history, never a general-purpose description table.
        """
        from returns_manager.explainer.service import ExplainerService

        principal = _current_principal()
        service = ExplainerService(db.pool)
        resp = await service.explain(
            org_id=principal.org_id,
            unit_id=unit_id,
            question=question,
        )
        return resp.model_dump()

    return mcp


def _build_asgi_app(mcp: Any) -> Any:
    """Wrap the MCP streamable-HTTP app with API-key authentication middleware.

    Every request must carry `Authorization: Bearer <api key>` resolving (via the same
    `security.api_keys.authenticate` the REST API uses) to a Principal with the
    evidence:read scope. The Principal is stashed in `_principal_var` for the duration
    of the request/connection so tools never see a client-supplied org_id.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from returns_manager.security.roles import Forbidden, Unauthenticated

    db = mcp.rm_db
    env = mcp.rm_env

    class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            try:
                principal = await authenticate_bearer_token(db, request.headers.get("authorization"), env=env)
            except Unauthenticated as exc:
                return JSONResponse({"error": "unauthenticated", "detail": str(exc)}, status_code=401)
            except Forbidden as exc:
                return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)

            token = _principal_var.set(principal)
            try:
                return await call_next(request)
            finally:
                _principal_var.reset(token)

    app = mcp.streamable_http_app()
    app.add_middleware(ApiKeyAuthMiddleware)
    return app


async def run_mcp_server(host: str = "127.0.0.1", port: int = 8001) -> None:
    """Entry point for `returns-manager mcp serve`."""
    import uvicorn

    from returns_manager.config import get_settings
    from returns_manager.observability.logging import configure_logging

    configure_logging(get_settings().log_level)

    mcp = _build_mcp_server()
    await mcp.rm_db.open()
    try:
        app = _build_asgi_app(mcp)
        # log_config=None: keep the structured JSON logging configured just above instead of
        # letting uvicorn's default dictConfig overwrite the root logger's handlers (§18.1).
        config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None)
        server = uvicorn.Server(config)
        logger.info("Starting MCP server on %s:%d", host, port)
        await server.serve()
    finally:
        await mcp.rm_db.close()
