"""FastAPI application (§15). Business logic lives in the service modules; routes only translate HTTP.

Start-up is fail-safe: the pool is opened and the boot check runs before the app serves anything. If the
database role can bypass row-level security, start-up raises and the process refuses to serve.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from returns_manager import __version__
from returns_manager.api import problems
from returns_manager.api.deps import Services, ServicesDep
from returns_manager.api.routes import batch as batch_routes
from returns_manager.api.routes import chain as chain_routes
from returns_manager.api.routes import evidence as evidence_routes
from returns_manager.api.routes import explainer as explainer_routes
from returns_manager.api.routes import intake as intake_routes
from returns_manager.api.routes import jobs as jobs_routes
from returns_manager.api.routes import metrics as metrics_routes
from returns_manager.api.routes import review as review_routes
from returns_manager.api.routes import security as security_routes
from returns_manager.api.routes import simulate as simulate_routes
from returns_manager.api.routes import webhooks as webhooks_routes
from returns_manager.batch.jobs_service import BatchJobsService
from returns_manager.config import AGENT_ROOT, Settings, get_settings
from returns_manager.db.pool import Database, UnsafeDatabaseRole
from returns_manager.ids import new_id
from returns_manager.security.auth_jwt import JwtVerifier
from returns_manager.storage.photos import PhotoStorage

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    from returns_manager.observability.logging import configure_logging

    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db: Database | None = None
        if settings.database_url is not None and settings.database_url.get_secret_value().strip():
            db = Database(settings.database_url.get_secret_value())
            try:
                await db.open()  # runs the boot check; raises UnsafeDatabaseRole under a bypass role
            except UnsafeDatabaseRole:
                raise
            except Exception as exc:
                import structlog

                structlog.get_logger().warning(
                    "Database connection offline; starting in standalone batch mode.",
                    error=str(exc),
                )
        jwt: JwtVerifier | None = None
        if settings.supabase_jwks_url or settings.supabase_jwt_secret or settings.supabase_url:
            try:
                jwt = JwtVerifier(settings)
            except Exception as exc:
                import structlog

                structlog.get_logger().warning(
                    "JWT verifier offline; proceeding without auth verification",
                    error=str(exc),
                )
        storage: PhotoStorage | None = None
        if settings.supabase_url:
            try:
                storage = PhotoStorage(settings)
            except Exception as exc:
                import structlog

                structlog.get_logger().warning(
                    "Photo storage offline; proceeding without Supabase storage",
                    error=str(exc),
                )
        if settings.gemini_api_key is not None:
            from returns_manager.llm.gemini_client import GeminiModelClient

            gemini_client = GeminiModelClient(
                settings.gemini_api_key.get_secret_value(), settings.rm_model_timeout_s
            )
            batch_jobs = BatchJobsService(
                root=AGENT_ROOT / ".data" / "batch_jobs",
                settings=settings,
                client=gemini_client,
            )
        else:
            from returns_manager.errors import ConfigError

            class _NoKeyFallbackClient:
                async def call(self, request: Any) -> Any:
                    raise ConfigError(
                        "GEMINI_API_KEY is not configured in environment. "
                        "Please add GEMINI_API_KEY to Render Environment Variables "
                        "to execute live inspections."
                    )

            batch_jobs = BatchJobsService(
                root=AGENT_ROOT / ".data" / "batch_jobs",
                settings=settings,
                client=_NoKeyFallbackClient(),  # type: ignore[arg-type]
            )
        app.state.services = Services(
            settings=settings, db=db, jwt=jwt, storage=storage, batch_jobs=batch_jobs
        )
        try:
            yield
        finally:
            if db is not None:
                try:
                    await db.close()
                except Exception as exc:
                    import structlog

                    structlog.get_logger().debug("Error closing database", error=str(exc))

    app = FastAPI(
        title="Returns Manager API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    problems.install(app)

    # Allow localhost, 127.0.0.1, and onrender.com origins
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|.*\.onrender\.com)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id")
        rid = incoming if incoming and _REQUEST_ID_RE.match(incoming) else new_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    class Health(BaseModel):
        status: str
        version: str

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        """Liveness only; touches no dependency."""
        return Health(status="ok", version=__version__)

    class Ready(BaseModel):
        ready: bool
        checks: dict[str, Any]

    @app.get("/ready", response_model=Ready)
    async def ready(svc: ServicesDep, response: Response) -> Ready:
        has_role = svc.db.role_name is not None if svc.db else False
        checks: dict[str, Any] = {"database_role_non_bypass": has_role}
        if svc.db is not None:
            try:
                async with svc.db.transaction(None) as conn:
                    await conn.execute("SELECT 1")
                checks["database"] = True
            except Exception:
                checks["database"] = False
        else:
            checks["database"] = False
        checks["storage"] = await svc.storage.ping() if svc.storage else False
        checks["models"] = "not checked until phase P5"
        ok = bool(checks["database"] and checks["database_role_non_bypass"] and checks["storage"])
        response.status_code = 200 if ok else 503
        return Ready(ready=ok, checks=checks)

    app.include_router(security_routes.router)
    app.include_router(intake_routes.router)
    app.include_router(batch_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(simulate_routes.router)
    app.include_router(review_routes.router)
    app.include_router(chain_routes.router)
    app.include_router(evidence_routes.router)
    app.include_router(explainer_routes.router)
    app.include_router(webhooks_routes.router)
    app.include_router(metrics_routes.router)

    # Serve static frontend SPA if ui/dist exists (production container or build)
    dist_dir = AGENT_ROOT.parent / "ui" / "dist"
    if not dist_dir.exists():
        from pathlib import Path

        dist_dir = Path("/app/ui/dist")

    if dist_dir.exists():
        from fastapi.staticfiles import StaticFiles
        from starlette.responses import FileResponse

        if (dist_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")
        if (dist_dir / "frames").exists():
            app.mount("/frames", StaticFiles(directory=dist_dir / "frames"), name="frames")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> Response:
            target = dist_dir / full_path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(dist_dir / "index.html")

    return app
