"""Short-lived storage for finished searches, so a result gets its own URL.

Two jobs, one store:

- **Post/Redirect/Get.** A search POSTs, its outcome is stored under an
  unguessable token, and the browser is redirected to ``/r/<token>``. Refreshing
  that page re-reads the store instead of re-submitting the form, so a reload no
  longer costs two Gemini calls or raises a browser resubmit warning.
- **Repeat searches.** A successful outcome is also indexed by a key derived
  from the room image and furniture type, so running the same search again
  redirects to the result already computed rather than paying for it twice.

Backends match the rate limiter's: Upstash Redis when its credentials are set,
otherwise an in-process dict.

The in-process backend is the honest weak point. Unlike the rate limiter, where
a per-instance counter merely makes the limit loose, a per-instance *result*
store means the redirect can land on a different serverless instance and find
nothing — the visitor waited 20 seconds and then sees "results expired". It also
means a shared link only works for whoever is routed to the instance holding it.
So: set up Upstash if links need to work reliably or be shareable at all. The
miss is handled gracefully either way, and `/r/<token>` is cheap to retry.

Failures are treated as a miss, never an error: a storage outage degrades the
app to "your link expired", which is recoverable, rather than breaking a search
that already succeeded.
"""
import hashlib
import json
import logging
import os
import secrets
import threading
import time

import upstash

logger = logging.getLogger(__name__)

# A stored result holds IKEA prices and ratings, which drift, so it is not kept
# indefinitely; a day is long enough for a shared link to stay useful.
DEFAULT_TTL = 86_400
# Failures get a token too, so refreshing an error page doesn't re-run the
# pipeline, but they are short-lived and never indexed by search key: a Gemini
# outage must not be remembered as this room's answer for a day.
ERROR_TTL = 300

TOKEN_BYTES = 16
RESULT_PREFIX = "decora:result:"
SEARCH_PREFIX = "decora:search:"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


def new_token() -> str:
    """An unguessable handle for one search outcome.

    Results are not secret, but they describe someone's room, so the token is
    random rather than sequential: nobody should be able to walk other people's
    results by incrementing a number.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def search_key(room_img, furniture_type: str) -> str:
    """A stable key for a (room image, furniture type) pair.

    The image is reduced to a small thumbnail before hashing, so the key depends
    on what the room looks like rather than on the exact bytes of the upload.
    This is best-effort: a re-encoded copy of the same photo still hashes
    differently, which costs a cache miss and nothing else.
    """
    thumb = room_img.copy()
    thumb.thumbnail((256, 256))
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    digest = hashlib.sha256()
    digest.update(thumb.tobytes())
    digest.update(b"\x00")
    digest.update(furniture_type.strip().lower().encode("utf-8"))
    return digest.hexdigest()


class InMemoryBackend:
    """TTL'd values in this process. Not shared between serverless instances."""

    _MAX_ENTRIES = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple] = {}  # key -> (expires_at, value)

    def set(self, key: str, value: str, ttl: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._entries) >= self._MAX_ENTRIES:
                self._prune(now)
            self._entries[key] = (now + ttl, value)
        return True

    def get(self, key: str) -> "str | None":
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry[0] <= time.monotonic():
                del self._entries[key]
                return None
            return entry[1]

    def _prune(self, now: float) -> None:
        """Drop expired entries; if that frees nothing, drop the oldest.

        Called with the lock held. The hard eviction matters because entries
        live for a day: without it a busy instance would hold every result it
        ever served.
        """
        self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
        if len(self._entries) >= self._MAX_ENTRIES:
            oldest = sorted(self._entries, key=lambda k: self._entries[k][0])
            for key in oldest[: len(oldest) // 4 or 1]:
                del self._entries[key]


class UpstashBackend:
    """TTL'd values in Upstash Redis, shared across instances."""

    def __init__(self, client) -> None:
        self._client = client

    def set(self, key: str, value: str, ttl: int) -> bool:
        return self._client.pipeline(["SET", key, value, "EX", ttl]) is not None

    def get(self, key: str) -> "str | None":
        results = self._client.pipeline(["GET", key])
        if not results:
            return None
        return results[0]


def make_backend():
    client = upstash.from_env()
    if client is not None:
        logger.info("result store: Upstash Redis (shared, links work across instances)")
        return UpstashBackend(client)
    logger.info(
        "result store in-process: result links work only on the instance that "
        "served the search. Set UPSTASH_REDIS_REST_URL and "
        "UPSTASH_REDIS_REST_TOKEN to make them shareable."
    )
    return InMemoryBackend()


class ResultStore:
    """Stores search outcomes by token, and indexes successes by search key."""

    def __init__(self, backend=None, ttl=None):
        self._backend = backend if backend is not None else make_backend()
        self.ttl = DEFAULT_TTL if ttl is None else ttl

    @classmethod
    def from_env(cls, backend=None) -> "ResultStore":
        return cls(backend=backend, ttl=_int_env("RESULT_TTL_SECONDS", DEFAULT_TTL))

    @property
    def backend(self):
        return self._backend

    def put_results(self, items, key=None) -> str:
        """Store a successful search; index it by `key` so a repeat reuses it."""
        token = new_token()
        if not self._backend.set(RESULT_PREFIX + token, json.dumps({"items": items}), self.ttl):
            logger.warning("could not store results; the link will not resolve")
        if key:
            # Best effort: if this fails the result is still reachable by token,
            # the next identical search just pays for itself again.
            self._backend.set(SEARCH_PREFIX + key, token, self.ttl)
        return token

    def put_error(self, heading, message) -> str:
        """Store a failed search so refreshing its page doesn't re-run it."""
        token = new_token()
        payload = json.dumps({"error": {"heading": heading, "message": message}})
        self._backend.set(RESULT_PREFIX + token, payload, ERROR_TTL)
        return token

    def get(self, token: str) -> "dict | None":
        """The stored outcome, or None if it is unknown or expired."""
        raw = self._backend.get(RESULT_PREFIX + token)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("discarding unreadable stored result for %s", token)
            return None
        return payload if isinstance(payload, dict) else None

    def token_for(self, key: str) -> "str | None":
        """The token of an earlier identical search, if one is still stored.

        The index can outlive the result it points at only if they were written
        with different TTLs, which they aren't — but a missing result is checked
        for anyway, so a dangling index never sends a visitor to a dead link.
        """
        token = self._backend.get(SEARCH_PREFIX + key)
        if not token:
            return None
        if self.get(token) is None:
            return None
        return token


_default = None


def default_store() -> ResultStore:
    """The process-wide store, built from the environment on first use."""
    global _default
    if _default is None:
        _default = ResultStore.from_env()
    return _default
