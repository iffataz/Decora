"""Per-IP and global rate limiting for the search endpoint.

A search costs two Gemini calls on a billed project, so an unmetered public
endpoint is an open-ended bill. Two limits are enforced:

- **per IP**, so one visitor cannot loop the form;
- **global per day**, so a spread-out or distributed burst still cannot run the
  bill past a known ceiling. Without this the per-IP limit alone is no real
  spend protection.

Two backends, chosen automatically:

- **Upstash Redis** when ``UPSTASH_REDIS_REST_URL`` and
  ``UPSTASH_REDIS_REST_TOKEN`` are set. Counters are shared, so the limits are
  exact. Reached over its REST API, so there is no extra dependency and no
  connection to hold open — which is what makes it work on serverless.
- **in-process** otherwise, so the app is protected out of the box with no
  configuration. Each serverless instance counts only its own requests, so the
  real ceiling is the configured limit times the number of warm instances, and
  counters reset when an instance recycles. Good enough to stop a loop from one
  browser; use Upstash if you need the limit to actually hold.

Both use a fixed window (one counter per window, expiring with it) rather than a
sliding window: it is one round trip instead of a sorted-set read, at the cost of
allowing up to 2x the limit across a window boundary.

If the backend errors the request is **allowed** — a limiter outage should not
take the site down. That does mean an attacker who can break Redis gets past the
limit, which is the right trade for this app but worth knowing.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


# 10 searches/hour/IP, 200/day overall (~400 Gemini calls). Override via env.
# 10 leaves room for a demo or a few rooms in a row while still capping one
# visitor at 20 Gemini calls an hour.
PER_IP_LIMIT = _int_env("RATE_LIMIT_PER_IP", 10)
PER_IP_WINDOW = _int_env("RATE_LIMIT_WINDOW_SECONDS", 3600)
GLOBAL_DAILY_LIMIT = _int_env("RATE_LIMIT_GLOBAL_PER_DAY", 200)
GLOBAL_WINDOW = 86_400

# Vercel's edge rewrites both of these, so they can be trusted there. Set
# TRUST_PROXY_HEADERS=0 when exposing the app directly, where a client can
# send whatever it likes and spoofing them would defeat the per-IP limit.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "1") != "0"

UPSTASH_TIMEOUT = 3


@dataclass(frozen=True)
class Decision:
    """Outcome of a limit check. `scope` names which limit denied the request."""
    allowed: bool
    retry_after: int = 0
    scope: str = ""


class InMemoryBackend:
    """Fixed-window counters in this process. Not shared between instances."""

    _MAX_KEYS = 4096

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, list] = {}  # key -> [expires_at, count]

    def incr(self, key: str, ttl: int) -> int | None:
        now = time.monotonic()
        with self._lock:
            if len(self._windows) > self._MAX_KEYS:
                # Unbounded growth would be a slow memory leak, since every
                # distinct client IP adds a key.
                self._windows = {
                    k: v for k, v in self._windows.items() if v[0] > now
                }
            window = self._windows.get(key)
            if window is None or window[0] <= now:
                self._windows[key] = [now + ttl, 1]
                return 1
            window[1] += 1
            return window[1]

    def reset(self) -> None:
        """Drop all counters. For tests."""
        with self._lock:
            self._windows.clear()


class UpstashBackend:
    """Fixed-window counters in Upstash Redis, shared across instances."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/") + "/pipeline"
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def incr(self, key: str, ttl: int) -> int | None:
        # EXPIRE .. NX sets the TTL only if the key has none, so the window is
        # anchored to its first request instead of sliding forward on each hit.
        body = [["INCR", key], ["EXPIRE", key, str(ttl), "NX"]]
        try:
            resp = self._session.post(self._url, json=body, timeout=UPSTASH_TIMEOUT)
            resp.raise_for_status()
            count = resp.json()[0]["result"]
            return int(count)
        except Exception as e:
            # Fail open: see the module docstring.
            logger.warning("rate limit backend unavailable, allowing request: %s", e)
            return None


def _make_backend():
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        logger.info("rate limiting via Upstash Redis (shared counters)")
        return UpstashBackend(url, token)
    logger.info(
        "rate limiting in-process: limits apply per serverless instance, not globally. "
        "Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN for exact limits."
    )
    return InMemoryBackend()


_backend = None


def backend():
    global _backend
    if _backend is None:
        _backend = _make_backend()
    return _backend


def client_ip(request) -> str:
    """Best-effort client address for the per-IP limit."""
    if TRUST_PROXY_HEADERS:
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Leftmost entry is the originating client.
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _window_key(prefix: str, ident: str, window: int) -> str:
    """Key naming the current fixed window, so it expires with it."""
    return f"decora:{prefix}:{int(time.time()) // window}:{ident}"


def check(ip: str) -> Decision:
    """Count this search against both limits and say whether to serve it.

    The per-IP limit is checked first, so a visitor who is already over their
    own limit does not consume global quota. A request allowed per-IP but denied
    globally does consume one of its IP's slots, which is a harmless over-count
    in the rare case where the global cap is the binding constraint.
    """
    store = backend()
    now = int(time.time())

    if PER_IP_LIMIT > 0:
        count = store.incr(_window_key("ip", ip, PER_IP_WINDOW), PER_IP_WINDOW)
        if count is not None and count > PER_IP_LIMIT:
            retry_after = PER_IP_WINDOW - (now % PER_IP_WINDOW)
            logger.info("rate limited %s: %d searches in window", ip, count)
            return Decision(False, retry_after, "ip")

    if GLOBAL_DAILY_LIMIT > 0:
        count = store.incr(_window_key("global", "all", GLOBAL_WINDOW), GLOBAL_WINDOW)
        if count is not None and count > GLOBAL_DAILY_LIMIT:
            retry_after = GLOBAL_WINDOW - (now % GLOBAL_WINDOW)
            logger.warning("global daily search cap of %d reached", GLOBAL_DAILY_LIMIT)
            return Decision(False, retry_after, "global")

    return Decision(True)


def describe_wait(seconds: int) -> str:
    """Render a retry delay for an end user: 'about 20 minutes'."""
    if seconds <= 90:
        return "about a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minutes"
    hours = round(seconds / 3600)
    return "about an hour" if hours == 1 else f"about {hours} hours"
