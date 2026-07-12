from __future__ import annotations

import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.services.performance_metrics import performance_metrics


STREAM_PATH_RE = re.compile(r"^/api/stream/track/\d+$")
STREAM_TICKET_PATH_RE = re.compile(r"^/api/stream/track/\d+/ticket$")


def operation_for_request(method: str, path: str) -> str | None:
    if method == "POST" and path == "/api/auth/register":
        return "registration"
    if method == "GET" and path == "/api/search":
        return "search"
    if method == "POST" and STREAM_TICKET_PATH_RE.match(path):
        return "stream_ticket"
    if method in {"GET", "HEAD"} and STREAM_PATH_RE.match(path):
        return "stream_setup"
    if method == "POST" and path == "/api/history/progress":
        return "listening_progress"
    return None


class PerformanceMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        operation = operation_for_request(request.method, request.url.path)
        if not operation:
            return await call_next(request)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            performance_metrics.record(operation, duration_ms, status_code)
            if "response" in locals():
                response.headers["Server-Timing"] = f'app;dur={duration_ms:.2f};desc="{operation}"'
