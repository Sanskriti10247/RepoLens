"""Service-level errors, translated to HTTP responses by the API layer."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class InvalidInput(ServiceError):
    status_code = 422
    code = "invalid_input"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"
