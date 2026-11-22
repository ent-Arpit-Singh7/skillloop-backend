"""In-process authentication rate limits.

IP limits protect the API surface; failed-login limits keyed by a one-way hash
of the normalized account identifier slow password guessing across IPs. For
multi-worker deployments use a shared Redis store instead of process memory.
"""
from collections import defaultdict, deque
from hashlib import sha256
from threading import Lock
from time import monotonic

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    PROTECTED = {"/api/auth/login", "/api/auth/register", "/api/auth/admin/login", "/api/auth/refresh"}

    def __init__(self, app, max_requests: int = 10, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = Lock()
        self.max_tracked_keys = 20_000

    async def dispatch(self, request, call_next):
        if request.method == "POST" and request.url.path in self.PROTECTED:
            client_ip = request.client.host if request.client else "unknown"
            key = (client_ip, request.url.path)
            now = monotonic()
            with self._lock:
                # Prune stale keys as well as this key so spoofed/rotating client
                # addresses cannot grow the in-memory limiter without bound.
                for old_key, old_hits in list(self._hits.items()):
                    while old_hits and now - old_hits[0] >= self.window_seconds:
                        old_hits.popleft()
                    if not old_hits:
                        self._hits.pop(old_key, None)
                if len(self._hits) >= self.max_tracked_keys and key not in self._hits:
                    self._hits.pop(next(iter(self._hits)), None)
                hits = self._hits[key]
                if len(hits) >= self.max_requests:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Too many authentication attempts. Please try again later."},
                        headers={"Retry-After": str(self.window_seconds)},
                    )
                hits.append(now)
        return await call_next(request)


class AccountLoginRateLimiter:
    """Counts failed logins per account without retaining raw email addresses."""
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures = defaultdict(deque)
        self._lock = Lock()
        self.max_tracked_accounts = 20_000

    @staticmethod
    def _key(role: str, email: str) -> str:
        normalized = f"{role}:{email.strip().casefold()}".encode("utf-8")
        return sha256(normalized).hexdigest()

    def is_limited(self, role: str, email: str) -> tuple[bool, int]:
        key = self._key(role, email)
        now = monotonic()
        with self._lock:
            self._prune(now)
            hits = self._failures[key]
            if len(hits) >= self.max_attempts:
                return True, max(1, int(self.window_seconds - (now - hits[0])))
            return False, 0

    def record_failure(self, role: str, email: str) -> None:
        key = self._key(role, email)
        now = monotonic()
        with self._lock:
            self._prune(now)
            if key not in self._failures and len(self._failures) >= self.max_tracked_accounts:
                self._failures.pop(next(iter(self._failures)), None)
            self._failures[key].append(now)

    def _prune(self, now: float) -> None:
        for old_key, old_hits in list(self._failures.items()):
            while old_hits and now - old_hits[0] >= self.window_seconds:
                old_hits.popleft()
            if not old_hits:
                self._failures.pop(old_key, None)

    def clear(self, role: str, email: str) -> None:
        with self._lock:
            self._failures.pop(self._key(role, email), None)


class ReportTargetRateLimiter:
    """Limit one reporter to one report per target during a rolling window."""

    def __init__(self, window_seconds: int = 24 * 60 * 60, max_tracked_targets: int = 50_000):
        self.window_seconds = window_seconds
        self.max_tracked_targets = max_tracked_targets
        self._submissions: dict[tuple[int, str, int], float] = {}
        self._lock = Lock()

    def claim(self, reporter_id: int, target_type: str, target_id: int) -> int:
        now = monotonic()
        key = (reporter_id, target_type, target_id)
        with self._lock:
            for old_key, expires_at in list(self._submissions.items()):
                if expires_at <= now:
                    self._submissions.pop(old_key, None)
            expires_at = self._submissions.get(key)
            if expires_at is not None:
                return max(1, int(expires_at - now))
            if len(self._submissions) >= self.max_tracked_targets:
                oldest_key = min(self._submissions, key=self._submissions.get)
                self._submissions.pop(oldest_key, None)
            self._submissions[key] = now + self.window_seconds
            return 0

    def release(self, reporter_id: int, target_type: str, target_id: int) -> None:
        with self._lock:
            self._submissions.pop((reporter_id, target_type, target_id), None)

    def clear(self) -> None:
        with self._lock:
            self._submissions.clear()


report_target_limiter = ReportTargetRateLimiter()


account_login_limiter = AccountLoginRateLimiter(
    settings.login_account_rate_limit_attempts,
    settings.login_account_rate_limit_window_seconds,
)
