# asgi-ratelimit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

ASGI middleware for token-bucket and sliding-window rate limiting.

## Features

- **Token Bucket**: Classic token bucket algorithm with refill rate
- **Sliding Window**: Precise sliding window log algorithm
- **ASGI-native**: Works with FastAPI, Starlette, and any ASGI framework
- **Async-first**: Built for high-performance async applications
- **Flexible**: Rate limit by IP, user ID, or custom keys

## Installation

```bash
pip install asgi-ratelimit
```

## Quick Start

### Token Bucket Example

```python
from asgi_ratelimit import RateLimitMiddleware
from starlette.applications import Starlette

app = Starlette()
app.add_middleware(
    RateLimitMiddleware,
    strategy="token_bucket",
    capacity=100,
    refill_rate=10,  # tokens per second
)
```

### Sliding Window Example

```python
app.add_middleware(
    RateLimitMiddleware,
    strategy="sliding_window",
    max_requests=100,
    window_seconds=60,
)
```

## Configuration

### Common Parameters

- `strategy`: `"token_bucket"` or `"sliding_window"`
- `key_func`: Custom function to extract rate limit key from request (default: client IP)

### Token Bucket Parameters

- `capacity`: Maximum tokens in bucket
- `refill_rate`: Tokens added per second

### Sliding Window Parameters

- `max_requests`: Maximum requests allowed in window
- `window_seconds`: Time window in seconds

## How It Works

### Token Bucket

Each client gets a bucket with a fixed capacity. Tokens are added at a constant rate. Each request consumes one token. If no tokens are available, the request is rate-limited.

### Sliding Window

Tracks exact timestamps of recent requests. Counts requests within the sliding time window. When the limit is exceeded, requests are rejected with 429 status.

## Response Headers

Rate limit information is included in response headers:

- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when limit resets

## Development

```bash
# Clone repository
git clone https://github.com/Abhi6662/asgi-ratelimit.git
cd asgi-ratelimit

# Install dependencies
poetry install

# Run tests
pytest

# Run tests with coverage
pytest --cov=src/asgi_ratelimit
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## Security

See [SECURITY.md](SECURITY.md) for security policy and vulnerability reporting.
