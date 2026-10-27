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

GLOBAL_WINDOW = 86_400
UPSTASH_TIMEOUT = 3

# Defaults, overridable per Limiter and from the environment. 10/hour/IP leaves
# room for a demo or a few rooms in a row while still capping one visitor at 20
# Gemini calls an hour; 200/day overall is ~400 Gemini calls.
DEFAULT_PER_IP = 10
DEFAULT_PER_IP_WINDOW = 3600
DEFAULT_GLOBAL_DAILY = 200


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


def trust_proxy_headers() -> bool:
    """Whether to believe X-Real-IP / X-Forwarded-For.

    Vercel's edge rewrites both, so they can be trusted there. Set
    TRUST_PROXY_HEADERS=0 when exposing the app directly, where a client can
    send whatever it likes and spoofing them would defeat the per-IP limit.
    """
    return os.environ.get("TRUST_PROXY_HEADERS", "1") != "0"


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

    def incr(self, key: str, ttl: int) -> "int | None":
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

    def count(self, key: str) -> int:
        """Current value of a counter, 0 if absent or expired. For tests."""
        with self._lock:
            window = self._windows.get(key)
            if window is None or window[0] <= time.monotonic():
                return 0
            return window[1]


class UpstashBackend:
    """Fixed-window counters in Upstash Redis, shared across instances."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/") + "/pipeline"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def incr(self, key: str, ttl: int) -> "int | None":
        # EXPIRE .. NX sets the TTL only if the key has none, so the window is
        # anchored to its first request instead of sliding forward on each hit.
        body = [["INCR", key], ["EXPIRE", key, str(ttl), "NX"]]
        try:
            resp = self.session.post(self._url, json=body, timeout=UPSTASH_TIMEOUT)
            resp.raise_for_status()
            return int(resp.json()[0]["result"])
        except Exception as e:
            # Fail open: see the module docstring.
            logger.warning("rate limit backend unavailable, allowing request: %s", e)
            return None


def make_backend():
    """Upstash when its credentials are set, otherwise in-process counters."""
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


class Limiter:
    """Counts searches against a per-IP limit and a global daily ceiling.

    Limits are held on the instance rather than read from the environment on
    each call, so tests can build one directly instead of reloading the module.
    A limit of 0 disables that check.
    """

    def __init__(self, backend=None, per_ip=None, per_ip_window=None, global_daily=None):
        self._backend = backend if backend is not None else make_backend()
        self.per_ip = DEFAULT_PER_IP if per_ip is None else per_ip
        self.per_ip_window = DEFAULT_PER_IP_WINDOW if per_ip_window is None else per_ip_window
        self.global_daily = DEFAULT_GLOBAL_DAILY if global_daily is None else global_daily

    @classmethod
    def from_env(cls, backend=None) -> "Limiter":
        return cls(
            backend=backend,
            per_ip=_int_env("RATE_LIMIT_PER_IP", DEFAULT_PER_IP),
            per_ip_window=_int_env("RATE_LIMIT_WINDOW_SECONDS", DEFAULT_PER_IP_WINDOW),
            global_daily=_int_env("RATE_LIMIT_GLOBAL_PER_DAY", DEFAULT_GLOBAL_DAILY),
        )

    @property
    def backend(self):
        return self._backend

    def ip_key(self, ip: str) -> str:
        return _window_key("ip", ip, self.per_ip_window)

    def global_key(self) -> str:
        return _window_key("global", "all", GLOBAL_WINDOW)

    def check(self, ip: str) -> Decision:
        """Count this search against both limits and say whether to serve it.

        The per-IP limit is checked first, so a visitor who is already over
        their own limit does not consume global quota. A request allowed per-IP
        but denied globally does consume one of its IP's slots, which is a
        harmless over-count in the rare case where the global cap binds.
        """
        now = int(time.time())

        if self.per_ip > 0:
            count = self._backend.incr(self.ip_key(ip), self.per_ip_window)
            if count is not None and count > self.per_ip:
                logger.info("rate limited %s: %d searches in window", ip, count)
                return Decision(False, self.per_ip_window - (now % self.per_ip_window), "ip")

        if self.global_daily > 0:
            count = self._backend.incr(self.global_key(), GLOBAL_WINDOW)
            if count is not None and count > self.global_daily:
                logger.warning("global daily search cap of %d reached", self.global_daily)
                return Decision(False, GLOBAL_WINDOW - (now % GLOBAL_WINDOW), "global")

        return Decision(True)


def _window_key(prefix: str, ident: str, window: int) -> str:
    """Key naming the current fixed window, so it expires with it."""
    return f"decora:{prefix}:{int(time.time()) // window}:{ident}"


_default: "Limiter | None" = None


def default_limiter() -> Limiter:
    """The process-wide limiter, built from the environment on first use."""
    global _default
    if _default is None:
        _default = Limiter.from_env()
    return _default


def check(ip: str) -> Decision:
    return default_limiter().check(ip)


def client_ip(request, trust_proxy=None) -> str:
    """Best-effort client address for the per-IP limit."""
    if trust_proxy is None:
        trust_proxy = trust_proxy_headers()
    if trust_proxy:
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Leftmost entry is the originating client.
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def describe_wait(seconds: int) -> str:
    """Render a retry delay for an end user: 'about 20 minutes'."""
    if seconds <= 90:
        return "about a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minutes"
    hours = round(seconds / 3600)
    return "about an hour" if hours == 1 else f"about {hours} hours"
