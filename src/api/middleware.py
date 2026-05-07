"""
Request-ID middleware — binds a UUID to each request via a contextvar so
every structlog record emitted during the request carries the same ID.
The ID is also returned as `X-Request-ID` so users can quote it in bug reports.

If the client sends `X-Request-ID`, we trust short reasonable values
(<= 64 chars, ASCII printable) and pass them through; otherwise generate one.
"""

from __future__ import annotations

import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.logging_config import request_id_var

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)
