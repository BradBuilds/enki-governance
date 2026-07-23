"""
Lightweight in-process rate limiter for Enki API.

Uses a sliding window counter per IP. No external dependencies.
Designed for single-worker deployment (--workers 1).

Usage in main.py:
    from services.api.src.middleware.rate_limit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Per-path rate limit rules: (requests, window_seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/admin/challenges/issue": (10, 60),     # 10/min — prevent challenge store DoS
    "/webhooks": (60, 60),                   # 60/min — webhook ingestion
    "/ingest": (60, 60),                     # 60/min — device ingestion
    "/exports": (5, 60),                     # 5/min — heavy streaming queries
    "/assistant/chat": (6, 60),              # 6/min — hub install companion (paid Anthropic calls)
}

# Default for all other API routes
DEFAULT_LIMIT = (200, 60)  # 200/min


class _SlidingWindow:
    """Sliding window counter for a single key."""

    __slots__ = ("timestamps",)

    def __init__(self):
        self.timestamps: list[float] = []

    def hit(self, now: float, window: int) -> int:
        """Record a hit, prune old entries, return current count."""
        cutoff = now - window
        # Prune expired
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        self.timestamps.append(now)
        return len(self.timestamps)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiting middleware.

    Returns 429 with Retry-After header when limit exceeded.
    Skips health checks and static files.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        # key = (ip, path_prefix) → SlidingWindow
        self._windows: dict[tuple[str, str], _SlidingWindow] = defaultdict(_SlidingWindow)
        self._last_cleanup = time.monotonic()

    def _get_client_ip(self, request: Request) -> str:
        # Trust X-Forwarded-For from Caddy
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _match_rule(self, path: str) -> tuple[str, int, int]:
        """Find the most specific rate limit rule for a path."""
        for prefix, (limit, window) in RATE_LIMITS.items():
            if path.startswith(prefix):
                return prefix, limit, window
        return "default", DEFAULT_LIMIT[0], DEFAULT_LIMIT[1]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip health checks and docs
        if path in ("/health", "/health/ready", "/docs", "/openapi.json"):
            return await call_next(request)

        ip = self._get_client_ip(request)
        prefix, limit, window = self._match_rule(path)

        now = time.monotonic()
        key = (ip, prefix)
        count = self._windows[key].hit(now, window)

        # Periodic cleanup of stale windows (every 5 minutes)
        if now - self._last_cleanup > 300:
            self._cleanup(now)
            self._last_cleanup = now

        if count > limit:
            return JSONResponse(
                status_code=429,
                content={
                    "error_code": "API.RATE_LIMITED",
                    "detail": f"Rate limit exceeded ({limit} requests per {window}s)",
                },
                headers={"Retry-After": str(window)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response

    def _cleanup(self, now: float):
        """Remove windows with no recent activity."""
        stale = []
        for key, window in self._windows.items():
            if not window.timestamps or (now - window.timestamps[-1]) > 300:
                stale.append(key)
        for key in stale:
            del self._windows[key]
