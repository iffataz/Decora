"""Minimal Upstash Redis REST client, shared by the rate limiter and result store.

Upstash is reached over HTTP rather than the Redis protocol, which is what makes
it usable from a serverless function: no dependency beyond `requests`, and no
connection to hold open across invocations.

Every call is fail-soft — a failure returns None rather than raising, and each
caller decides what that means for it (the limiter allows the request; the
result store treats it as a miss). A storage outage should degrade the app, not
take it down.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 3


class UpstashClient:
    def __init__(self, url: str, token: str) -> None:
        self.base_url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def pipeline(self, *commands) -> "list | None":
        """Run commands in one round trip; returns their results, or None.

        Each command is a sequence whose parts are stringified, since the REST
        API wants JSON strings. The return is the `result` of each command in
        order, so callers index by command position.
        """
        body = [[str(part) for part in command] for command in commands]
        try:
            response = self.session.post(f"{self.base_url}/pipeline", json=body, timeout=TIMEOUT)
            response.raise_for_status()
            return [entry.get("result") for entry in response.json()]
        except Exception as e:
            logger.warning("Upstash unavailable: %s", e)
            return None


def from_env() -> "UpstashClient | None":
    """A client when both credentials are set, otherwise None."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashClient(url, token)
    return None
