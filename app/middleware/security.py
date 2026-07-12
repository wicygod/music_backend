import re
import threading
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import APP_AUTH_TOKEN, token_matches
from app.database import SessionLocal
from app.services.admin_monitor import record_event, record_session
from app.services.auth_service import decode_access_token, decode_stream_ticket, is_user_banned


RATE_LIMIT_WINDOW_SECONDS = 2.0
RATE_LIMIT_MAX_REQUESTS = 3
RATE_LIMIT_BLOCK_SECONDS = 120.0
RATE_LIMIT_PATH_RE = re.compile(r"^/api/(?:search|stream)(?:/|$)")
AUTH_EXEMPT_PATHS = {"/api/health", "/api/auth/register", "/api/auth/login"}
STREAM_TRACK_PATH_RE = re.compile(r"^/api/stream/track/(\d+)$")


class LightweightSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or not request.url.path.startswith("/api"):
            return await call_next(request)

        if request.url.path == "/api/health":
            return await call_next(request)

        client_ip = self._client_ip(request)
        ticket_auth = self._stream_ticket_auth(request, client_ip)
        if isinstance(ticket_auth, JSONResponse):
            return ticket_auth
        if not ticket_auth:
            if not self._has_app_token(request):
                return JSONResponse({"detail": "Unauthorized app token"}, status_code=401)
            auth_response = self._auth_response(request)
            if auth_response:
                return auth_response

        if RATE_LIMIT_PATH_RE.match(request.url.path):
            if not ticket_auth:
                limited_response = self._rate_limit_response(client_ip, request.url.path)
                if limited_response:
                    return limited_response
            if not request.headers.get("range"):
                self._record_request_event(request, client_ip)

        return await call_next(request)

    def _stream_ticket_auth(self, request: Request, client_ip: str) -> bool | JSONResponse:
        match = STREAM_TRACK_PATH_RE.match(request.url.path)
        ticket = request.query_params.get("stream_ticket")
        if not match or not ticket or request.method not in {"GET", "HEAD"}:
            return False
        try:
            payload = decode_stream_ticket(ticket, int(match.group(1)))
            user_id = int(payload["sub"])
        except Exception:
            return JSONResponse({"detail": "Invalid stream ticket"}, status_code=401)
        with SessionLocal() as db:
            if is_user_banned(db, user_id):
                return JSONResponse({"detail": "Account is banned"}, status_code=403)
        request.state.user_id = user_id
        record_session(user_id, ip=client_ip)
        return True

    def _has_app_token(self, request: Request) -> bool:
        header_token = request.headers.get("X-App-Token")
        query_token = parse_qs(request.url.query).get("app_token", [None])[0]
        return token_matches(APP_AUTH_TOKEN, header_token or query_token)

    def _auth_response(self, request: Request):
        path = request.url.path
        if path in AUTH_EXEMPT_PATHS or path.startswith("/api/admin"):
            return None
        token = self._auth_token(request)
        if not token:
            return JSONResponse({"detail": "Missing auth token"}, status_code=401)
        try:
            payload = decode_access_token(token)
            user_id = int(payload["sub"])
        except Exception:
            return JSONResponse({"detail": "Invalid auth token"}, status_code=401)
        with SessionLocal() as db:
            if is_user_banned(db, user_id):
                return JSONResponse({"detail": "Account is banned"}, status_code=403)
        record_session(user_id, ip=self._client_ip(request))
        request.state.user_id = user_id
        return None

    def _auth_token(self, request: Request) -> str | None:
        authorization = request.headers.get("Authorization") or ""
        if authorization.lower().startswith("bearer "):
            return authorization.split(" ", 1)[1].strip()
        return parse_qs(request.url.query).get("auth_token", [None])[0]

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _rate_limit_response(self, client_ip: str, path: str):
        now = time.monotonic()
        with self._lock:
            blocked_until = self._blocked_until.get(client_ip, 0)
            if blocked_until > now:
                retry_after = max(1, int(blocked_until - now))
                return JSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )

            bucket = self._requests[client_ip]
            while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
                bucket.popleft()
            bucket.append(now)
            if len(bucket) > RATE_LIMIT_MAX_REQUESTS:
                self._blocked_until[client_ip] = now + RATE_LIMIT_BLOCK_SECONDS
                bucket.clear()
                record_event("rate-limit", f"Rate limit blocked {client_ip} for {path}", ip=client_ip, path=path)
                return JSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": str(int(RATE_LIMIT_BLOCK_SECONDS))},
                )
        return None

    def _record_request_event(self, request: Request, client_ip: str) -> None:
        path = request.url.path
        if path == "/api/search":
            q = request.query_params.get("q", "")
            record_event("search", f"Search request: {q}", ip=client_ip, path=path)
            return
        if path.startswith("/api/stream/track/"):
            track_id = path.rsplit("/", 1)[-1]
            record_event("stream", f"Track stream started: {track_id}", ip=client_ip, path=path)
            return
        if path.startswith("/api/stream"):
            record_event("stream", "Stream endpoint hit", ip=client_ip, path=path)
