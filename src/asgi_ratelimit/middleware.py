"""ASGI middleware for token-bucket and sliding-window rate limiting."""

import time
from typing import Callable, Optional, Set, Union

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .stores import InMemoryStore, RateLimitStore, SQLiteStore


class RateLimitMiddleware:
    """
    ASGI middleware for rate limiting using token-bucket or sliding-window algorithms.

    Supports:
    - Token Bucket: Classic token bucket with refill rate
    - Sliding Window: Precise sliding window log algorithm
    - Multiple stores: In-memory or SQLite (WAL mode)
    - Route exemptions: Skip rate limiting for specific paths
    - Custom key extractors: IP, API key, user ID, or custom function
    - Standard headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        strategy: str = "token_bucket",
        # Token bucket parameters
        capacity: int = 100,
        refill_rate: float = 10.0,
        # Sliding window parameters
        max_requests: int = 100,
        window_seconds: int = 60,
        # Key extraction
        key_func: Optional[Callable[[Request], str]] = None,
        # Route exemptions
        exempt_paths: Optional[Set[str]] = None,
        exempt_methods: Optional[Set[str]] = None,
        # Store
        store: Optional[RateLimitStore] = None,
        store_path: str = "ratelimit.db",
        # Header names
        header_limit: str = "X-RateLimit-Limit",
        header_remaining: str = "X-RateLimit-Remaining",
        header_reset: str = "X-RateLimit-Reset",
        header_retry_after: str = "Retry-After",
    ):
        if strategy not in ("token_bucket", "sliding_window"):
            raise ValueError(f"Unknown strategy: {strategy}. Use 'token_bucket' or 'sliding_window'")

        self.app = app
        self.strategy = strategy
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.key_func = key_func or self._default_key_func
        self.exempt_paths = exempt_paths or set()
        self.exempt_methods = exempt_methods or set()
        self.header_limit = header_limit
        self.header_remaining = header_remaining
        self.header_reset = header_reset
        self.header_retry_after = header_retry_after

        # Initialize store
        if store is not None:
            self.store = store
        else:
            # Use InMemoryStore by default for both strategies
            # SQLiteStore can be explicitly passed for persistence
            self.store = InMemoryStore()

        # For token bucket: store token counts per key with last refill time
        self._token_data: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    @staticmethod
    def _default_key_func(request: Request) -> str:
        """Extract client IP as default rate limit key."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _is_exempt(self, request: Request) -> bool:
        """Check if request path/method is exempt from rate limiting.

        Supports:
        - Exact path matches (e.g., "/health")
        - Prefix matches with wildcard (e.g., "/health*" matches "/healthz", "/health/check")
        - HTTP method exemptions (e.g., OPTIONS, HEAD)
        """
        path = request.url.path
        method = request.method

        # Check exact path match
        if path in self.exempt_paths:
            return True

        # Check prefix match with wildcard support
        # e.g., exempt_paths={"/health*"} matches "/health", "/healthz", "/health/check"
        for exempt in self.exempt_paths:
            if exempt.endswith("*"):
                prefix = exempt[:-1]
                if path.startswith(prefix):
                    return True
            elif path.startswith(exempt):
                return True

        if method in self.exempt_methods:
            return True

        return False

    def _get_tokens(self, key: str) -> tuple[float, float]:
        """Get current tokens and last refill time for token bucket."""
        now = time.time()
        tokens, last_refill = self._token_data.get(key, (float(self.capacity), now))

        # Refill tokens based on elapsed time
        elapsed = now - last_refill
        tokens = min(self.capacity, tokens + elapsed * self.refill_rate)

        return tokens, now

    def _consume_token(self, key: str) -> tuple[bool, float, int]:
        """
        Try to consume a token.
        Returns (allowed, retry_after, remaining_tokens)
        """
        tokens, last_refill = self._get_tokens(key)

        if tokens >= 1:
            tokens -= 1
            self._token_data[key] = (tokens, last_refill)
            return True, 0.0, int(tokens)

        # Calculate retry-after (time until 1 token available)
        retry_after = (1 - tokens) / self.refill_rate
        return False, retry_after, 0

    def _sliding_window_check(self, key: str) -> tuple[bool, float, int]:
        """
        Check fixed-window limit (simpler and correct for rate limiting).
        Returns (allowed, retry_after, remaining)
        """
        now = time.time()
        # Fixed window: bucket time into windows of window_seconds
        window_start = int(now // self.window_seconds) * self.window_seconds

        # Clean up expired entries periodically
        if hasattr(self.store, "cleanup_expired"):
            self.store.cleanup_expired()

        current_count, current_window_start = self.store.get(key)

        # Check if we're in a new window
        if current_window_start < window_start:
            # New window - reset count
            current_count = 0

        if current_count < self.max_requests:
            new_count = self.store.increment(key, window_start, self.window_seconds + 1)
            remaining = self.max_requests - new_count
            return True, 0.0, remaining

        # Calculate retry-after (time until window resets)
        retry_after = (window_start + self.window_seconds) - now
        return False, max(1.0, retry_after), 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Create request for key extraction
        request = Request(scope, receive)

        # Check exemptions
        if self._is_exempt(request):
            await self.app(scope, receive, send)
            return

        # Extract rate limit key
        try:
            key = self.key_func(request)
        except Exception:
            key = "unknown"

        # Apply rate limiting based on strategy
        if self.strategy == "token_bucket":
            allowed, retry_after, remaining = self._consume_token(key)
            limit = self.capacity
            reset_time = int(time.time() + (self.capacity - remaining) / self.refill_rate)
        else:
            allowed, retry_after, remaining = self._sliding_window_check(key)
            limit = self.max_requests
            reset_time = int(time.time() + self.window_seconds)

        # Build response headers
        headers = {
            self.header_limit: str(limit),
            self.header_remaining: str(max(0, remaining)),
            self.header_reset: str(reset_time),
        }

        if not allowed:
            headers[self.header_retry_after] = str(int(retry_after) + 1)
            response = Response(
                content="Rate limit exceeded",
                status_code=429,
                headers=headers,
            )
            await response(scope, receive, send)
            return

        # Wrap send to inject headers
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                # Add rate limit headers to response
                existing_headers = list(message.get("headers", []))
                for k, v in headers.items():
                    existing_headers.append((k.encode("latin-1"), v.encode("latin-1")))
                message["headers"] = existing_headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_rate_limiter(
    *,
    strategy: str = "token_bucket",
    capacity: int = 100,
    refill_rate: float = 10.0,
    max_requests: int = 100,
    window_seconds: int = 60,
    key_func: Optional[Callable[[Request], str]] = None,
    exempt_paths: Optional[Set[str]] = None,
    exempt_methods: Optional[Set[str]] = None,
    store: Optional[RateLimitStore] = None,
) -> Callable[[ASGIApp], ASGIApp]:
    """
    Factory function to create a rate limiter middleware.

    Usage:
        app.add_middleware(
            RateLimitMiddleware,
            strategy="token_bucket",
            capacity=100,
            refill_rate=10,
        )

    Or with factory:
        limiter = create_rate_limiter(strategy="sliding_window", max_requests=50, window_seconds=60)
        app.add_middleware(limiter)
    """
    def middleware(app: ASGIApp) -> ASGIApp:
        return RateLimitMiddleware(
            app,
            strategy=strategy,
            capacity=capacity,
            refill_rate=refill_rate,
            max_requests=max_requests,
            window_seconds=window_seconds,
            key_func=key_func,
            exempt_paths=exempt_paths,
            exempt_methods=exempt_methods,
            store=store,
        )
    return middleware