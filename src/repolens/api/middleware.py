"""Request correlation and access logging."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from repolens.api.errors import error_body
from repolens.observability.logging import bind_context, clear_context, get_logger

log = get_logger("repolens.api.access")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
    request.state.request_id = request_id
    clear_context()
    bind_context(request_id=request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # last line of defence; details stay in logs
        log.exception("unhandled_error", error_type=type(exc).__name__)
        response = JSONResponse(
            error_body(request, "internal_error", "Internal server error"), status_code=500
        )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    clear_context()
    return response
