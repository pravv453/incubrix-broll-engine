"""Demo access token, rate limiting, and callback signing."""
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque


def sign_body(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of the raw request body, sent as X-IncuBrix-Signature."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_body(secret, body), signature)


class RateLimiter:
    """Sliding-window limiter per client key (in-process; one API replica in the demo)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Returns None if allowed, else seconds until the next slot frees up."""
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return round(60 - (now - q[0]), 1)
            q.append(now)
            return None
