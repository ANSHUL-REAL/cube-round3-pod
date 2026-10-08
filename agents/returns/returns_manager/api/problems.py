"""RFC 9457 problem details (`application/problem+json`) for every error response (§15)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from returns_manager.errors import (
    BadRequest,
    Conflict,
    NotFound,
    PayloadTooLarge,
    QualityGateRefusal,
    ReturnsManagerError,
    UnsupportedMediaType,
)
from returns_manager.security.controls import ReasonRequired
from returns_manager.security.roles import Forbidden, Unauthenticated

PROBLEM_BASE = "https://returns-manager.local/problems/"
MEDIA_TYPE = "application/problem+json"


class Problem(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str | None = None) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def problem_response(
    request: Request, status: int, code: str, title: str, detail: str | None
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_BASE + code,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


_MAP: list[tuple[type[Exception], int, str, str]] = [
    (Unauthenticated, 401, "unauthenticated", "Authentication required"),
    (Forbidden, 403, "forbidden", "Not allowed"),
    (NotFound, 404, "not_found", "Not found"),
    (BadRequest, 400, "bad_request", "Bad request"),
    (QualityGateRefusal, 409, "retake_required", "Quality gate check failed; retake required"),
    (Conflict, 409, "conflict", "Conflict"),
    (PayloadTooLarge, 413, "payload_too_large", "Payload too large"),
    (UnsupportedMediaType, 415, "unsupported_media_type", "Unsupported media type"),
    (ReasonRequired, 422, "reason_required", "A reason is required"),
]


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(request, exc.status, exc.code, exc.title, exc.detail)

    @app.exception_handler(ReturnsManagerError)
    async def _domain(request: Request, exc: ReturnsManagerError) -> JSONResponse:
        for cls, status, code, title in _MAP:
            if isinstance(exc, cls):
                # 401/404 details never reveal whether another org's resource exists.
                detail = str(exc) if status not in (401, 404) else None
                return problem_response(request, status, code, title, detail)
        return problem_response(request, 500, "internal_error", "Internal error", None)

    @app.exception_handler(ReasonRequired)
    async def _reason(request: Request, exc: ReasonRequired) -> JSONResponse:
        return problem_response(request, 422, "reason_required", "A reason is required", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return problem_response(request, 422, "validation_error", "Invalid request", detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return problem_response(request, exc.status_code, code, str(exc.detail), None)
