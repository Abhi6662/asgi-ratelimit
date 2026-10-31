"""Storage backends for rate limit counters."""

import time
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Tuple


class RateLimitStore(ABC):
    """Abstract base class for rate limit storage."""

    @abstractmethod
    def get(self, key: str) -> Tuple[int, float]:
        """Get current count and window start for a key. Returns (count, window_start)."""
        pass

    @abstractmethod
    def set(self, key: str, count: int, window_start: float, ttl: int) -> None:
        """Set count and window start for a key with TTL in seconds."""
        pass

    @abstractmethod
    def increment(self, key: str, window_start: float, ttl: int) -> int:
        """Increment counter for key. Returns new count."""
        pass

    @abstractmethod
    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns number removed."""
        pass


class InMemoryStore(RateLimitStore):
    """Thread-safe in-memory store for rate limiting."""

    def __init__(self):
        self._data: dict[str, tuple[int, float]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Tuple[int, float]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return 0, 0.0
            count, window_start = entry
            return count, window_start

    def set(self, key: str, count: int, window_start: float, ttl: int) -> None:
        with self._lock:
            self._data[key] = (count, window_start)

    def increment(self, key: str, window_start: float, ttl: int) -> int:
        with self._lock:
            current_count, current_window = self._data.get(key, (0, 0.0))
            if current_window != window_start:
                current_count = 0
            current_count += 1
            self._data[key] = (current_count, window_start)
            return current_count

    def cleanup_expired(self) -> int:
        with self._lock:
            now = time.time()
            expired = [k for k, (_, ws) in self._data.items() if now - ws > 3600]
            for k in expired:
                del self._data[k]
            return len(expired)


class SQLiteStore(RateLimitStore):
    """SQLite-backed persistent store with WAL mode for rate limiting."""

    def __init__(self, db_path: str = "ratelimit.db"):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ratelimit (
                    key TEXT PRIMARY KEY,
                    count INTEGER NOT NULL,
                    window_start REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_expires ON ratelimit(expires_at)"
            )
            conn.commit()
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(self, key: str) -> Tuple[int, float]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT count, window_start FROM ratelimit WHERE key = ? AND expires_at > ?",
                    (key, time.time()),
                ).fetchone()
                if row is None:
                    return 0, 0.0
                return row[0], row[1]
            finally:
                conn.close()

    def set(self, key: str, count: int, window_start: float, ttl: int) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                expires_at = time.time() + ttl
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ratelimit (key, count, window_start, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, count, window_start, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

    def increment(self, key: str, window_start: float, ttl: int) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                now = time.time()
                expires_at = now + ttl
                row = conn.execute(
                    "SELECT count, window_start FROM ratelimit WHERE key = ? AND expires_at > ?",
                    (key, now),
                ).fetchone()

                if row is None or row[1] != window_start:
                    count = 1
                else:
                    count = row[0] + 1

                conn.execute(
                    """
                    INSERT OR REPLACE INTO ratelimit (key, count, window_start, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, count, window_start, expires_at),
                )
                conn.commit()
                return count
            finally:
                conn.close()

    def cleanup_expired(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                now = time.time()
                cursor = conn.execute(
                    "DELETE FROM ratelimit WHERE expires_at <= ?", (now,)
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()