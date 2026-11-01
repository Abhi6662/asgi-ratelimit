"""Tests for asgi-ratelimit middleware."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_ratelimit import RateLimitMiddleware, InMemoryStore, SQLiteStore
from asgi_ratelimit.middleware import create_rate_limiter


def _homepage(request):
    return PlainTextResponse("OK")


def _health(request):
    return PlainTextResponse("healthy")


# --- Token Bucket Tests ---


def test_token_bucket_allows_requests_under_limit():
    """Token bucket should allow requests under capacity."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=5,
        refill_rate=10.0,
    )

    client = TestClient(app, raise_server_exceptions=False)

    for i in range(5):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert int(response.headers["X-RateLimit-Remaining"]) == 4 - i


def test_token_bucket_blocks_over_limit():
    """Token bucket should return 429 when capacity exceeded."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=2,
        refill_rate=0.1,  # Very slow refill
    )

    client = TestClient(app, raise_server_exceptions=False)

    # First 2 requests OK
    response = client.get("/")
    assert response.status_code == 200
    response = client.get("/")
    assert response.status_code == 200

    # 3rd request blocked
    response = client.get("/")
    assert response.status_code == 429
    # With refill_rate=0.1, 1 token takes 10s; Retry-After = int(10) + 1 = 11
    assert response.headers["Retry-After"] in ("10", "11")


def test_token_bucket_refill_over_time():
    """Token bucket should refill tokens over time."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=2,
        refill_rate=100.0,  # Fast refill for test
    )

    client = TestClient(app, raise_server_exceptions=False)

    # Exhaust bucket
    client.get("/")
    client.get("/")
    response = client.get("/")
    assert response.status_code == 429

    # Wait for refill (0.01s should give ~1 token at 100/s)
    import time
    time.sleep(0.02)

    response = client.get("/")
    assert response.status_code == 200


def test_token_bucket_headers_present():
    """All standard rate limit headers should be present."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=10,
        refill_rate=5.0,
    )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers
    assert "X-RateLimit-Reset" in response.headers


def test_token_bucket_custom_key_func():
    """Custom key function should be used for rate limiting."""
    def api_key_func(request):
        return request.headers.get("X-API-Key", "anonymous")

    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=1,
        refill_rate=0.1,
        key_func=api_key_func,
    )

    client = TestClient(app, raise_server_exceptions=False)

    # First request with key A
    response = client.get("/", headers={"X-API-Key": "key-a"})
    assert response.status_code == 200

    # Second request with same key blocked
    response = client.get("/", headers={"X-API-Key": "key-a"})
    assert response.status_code == 429

    # Different key has separate bucket
    response = client.get("/", headers={"X-API-Key": "key-b"})
    assert response.status_code == 200


def test_token_bucket_route_exemption():
    """Exempt paths should bypass rate limiting."""
    app = Starlette(routes=[
        Route("/", _homepage, methods=["GET"]),
        Route("/health", _health, methods=["GET"]),
    ])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=1,
        refill_rate=0.1,
        exempt_paths={"/health", "/metrics"},
    )

    client = TestClient(app, raise_server_exceptions=False)

    # Root is limited
    client.get("/")
    response = client.get("/")
    assert response.status_code == 429

    # Health is exempt
    response = client.get("/health")
    assert response.status_code == 200
    response = client.get("/health")
    assert response.status_code == 200


# --- Sliding Window Tests ---


def test_sliding_window_allows_requests_under_limit():
    """Sliding window should allow requests under max_requests."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="sliding_window",
        max_requests=3,
        window_seconds=60,
    )

    client = TestClient(app, raise_server_exceptions=False)

    for i in range(3):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "3"
        # After increment, remaining = max - (i+1)
        assert int(response.headers["X-RateLimit-Remaining"]) == 3 - (i + 1)


def test_sliding_window_blocks_over_limit():
    """Sliding window should return 429 when max_requests exceeded."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="sliding_window",
        max_requests=2,
        window_seconds=60,
    )

    client = TestClient(app, raise_server_exceptions=False)

    client.get("/")
    client.get("/")
    response = client.get("/")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


# --- Store Tests ---


def test_in_memory_store_basic():
    """InMemoryStore basic operations."""
    store = InMemoryStore()
    count, ws = store.get("test-key")
    assert count == 0
    assert ws == 0.0

    store.set("test-key", 5, 100.0, 60)
    count, ws = store.get("test-key")
    assert count == 5
    assert ws == 100.0

    new_count = store.increment("test-key", 100.0, 60)
    assert new_count == 6


def test_in_memory_store_separate_keys():
    """InMemoryStore keeps separate counters per key."""
    store = InMemoryStore()

    store.increment("key-a", 100.0, 60)
    store.increment("key-a", 100.0, 60)
    store.increment("key-b", 100.0, 60)

    count_a, _ = store.get("key-a")
    count_b, _ = store.get("key-b")

    assert count_a == 2
    assert count_b == 1


def test_sqlite_store_persistence():
    """SQLiteStore persists data across connections."""
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = SQLiteStore(db_path)
        store.increment("persist-key", 100.0, 60)
        store.increment("persist-key", 100.0, 60)

        # New connection should see data
        store2 = SQLiteStore(db_path)
        count, _ = store2.get("persist-key")
        assert count == 2
    finally:
        os.unlink(db_path)


def test_sqlite_store_wal_mode():
    """SQLiteStore uses WAL mode for concurrent access."""
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = SQLiteStore(db_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.upper() == "WAL"
    finally:
        os.unlink(db_path)


# --- Factory Function Tests ---


def test_create_rate_limiter_factory():
    """create_rate_limiter factory should return middleware."""
    limiter = create_rate_limiter(strategy="token_bucket", capacity=5, refill_rate=1.0)
    app = Starlette(routes=[Route("/", _homepage, methods=["GET"])])
    app.add_middleware(limiter)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")
    assert response.status_code == 200


def test_exempt_methods():
    """Exempt HTTP methods should bypass rate limiting."""
    app = Starlette(routes=[Route("/", _homepage, methods=["GET", "OPTIONS", "HEAD"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="token_bucket",
        capacity=1,
        refill_rate=0.1,
        exempt_methods={"OPTIONS", "HEAD"},
    )

    client = TestClient(app, raise_server_exceptions=False)

    # GET is limited
    client.get("/")
    response = client.get("/")
    assert response.status_code == 429

    # OPTIONS is exempt
    response = client.options("/")
    assert response.status_code == 200


# --- Integration Test with FastAPI-like app ---


def test_fastapi_style_integration():
    """Integration test with FastAPI-style routing."""
    async def endpoint(request):
        return PlainTextResponse("OK")

    app = Starlette(routes=[Route("/api/test", endpoint, methods=["GET"])])
    app.add_middleware(
        RateLimitMiddleware,
        strategy="sliding_window",
        max_requests=10,
        window_seconds=60,
        store=InMemoryStore(),
    )

    client = TestClient(app, raise_server_exceptions=False)

    for i in range(10):
        response = client.get("/api/test")
        assert response.status_code == 200

    response = client.get("/api/test")
    assert response.status_code == 429
