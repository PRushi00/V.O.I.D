"""Per-request authentication and abuse protection for the Device Gateway.

Authentication is HMAC-SHA256 over the exact request body, keyed by the
per-device shared secret issued at pairing (void.device.identity). This is
on top of, not instead of, TLS: TLS (with the client pinning the server's
fingerprint - void.device.cert) protects the channel; the HMAC signature
additionally proves WHICH paired device sent THIS exact, unmodified body,
using only an established library primitive (:mod:`hmac`), never a home-rolled
scheme.

Replay/rate protection is intentionally simple (bounded in-memory state, no
external service) - appropriate for a single-user personal gateway, not a
public API.
"""
from __future__ import annotations

import hmac
import time
from collections import OrderedDict, deque
from hashlib import sha256

SIGNATURE_HEADER = "X-Void-Signature"

# A request's own 'timestamp' field must be within this many seconds of the
# server's clock, in either direction, or it is rejected as stale - this
# bounds how long a captured-and-replayed request could possibly be valid
# for even before the nonce cache is consulted.
MAX_CLOCK_SKEW_SECONDS = 60

# How long a (device_id, request_id) pair is remembered to reject exact
# replays - must be >= MAX_CLOCK_SKEW_SECONDS or a request could be replayed
# after its nonce is forgotten but before staleness would catch it.
REPLAY_WINDOW_SECONDS = 120


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    if not signature:
        return False
    expected = sign(secret, body)
    return hmac.compare_digest(expected, signature)


class ReplayGuard:
    """Tracks (device_id, request_id) pairs seen within the replay window.
    Bounded by time-based pruning, not by an unbounded set, so a long-running
    gateway process's memory use stays flat."""

    def __init__(self, window_seconds: float = REPLAY_WINDOW_SECONDS):
        self._window = window_seconds
        self._seen: dict[tuple[str, str], float] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            del self._seen[k]

    def check_and_record(self, device_id: str, request_id: str,
                         now: float | None = None) -> bool:
        """Returns True (and records it) the first time this pair is seen
        within the window; False if it is a replay."""
        now = time.time() if now is None else now
        self._prune(now)
        key = (device_id, request_id)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


class RateLimiter:
    """Fixed-window-free sliding counter per key (device id, or client IP
    for pre-auth attempts). Simple and dependency-free; sized for a single
    personal gateway, not for defending a public endpoint.

    The number of distinct keys is BOUNDED (``max_keys``, least-recently-used
    eviction): keys can come from unauthenticated input (an attacker-chosen
    ``device_id``), and V1 kept one entry per key forever (D-09). Evicting the
    LRU key only ever RESETS that key's counter, so it can weaken limiting for an
    evicted key but never blocks anyone."""

    def __init__(self, max_events: int, per_seconds: float, max_keys: int | None = 1024):
        self._max = max_events
        self._per = per_seconds
        self._max_keys = max_keys
        self._events: "OrderedDict[str, deque]" = OrderedDict()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        q = self._events.get(key)
        if q is None:
            q = self._events[key] = deque()
            if self._max_keys is not None:
                while len(self._events) > self._max_keys:
                    self._events.popitem(last=False)      # evict least-recently-used
        else:
            self._events.move_to_end(key)
        cutoff = now - self._per
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        return True


def is_stale(request_timestamp: float, now: float | None = None,
            max_skew: float = MAX_CLOCK_SKEW_SECONDS) -> bool:
    now = time.time() if now is None else now
    return abs(now - request_timestamp) > max_skew
