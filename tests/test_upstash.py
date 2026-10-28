"""The shared Upstash REST client: request shape and fail-soft behaviour."""
import pytest
import requests

import upstash


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else []
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"HTTP {self.status}")

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


@pytest.fixture
def client(monkeypatch):
    """A client whose HTTP session is faked; returns it plus the posts made."""
    def build(payload=None, status=200, raises=None):
        c = upstash.UpstashClient("https://db.upstash.io", "tok")
        posts = []

        def fake_post(url, json=None, timeout=None):
            posts.append({"url": url, "body": json, "timeout": timeout})
            if raises is not None:
                raise raises
            return FakeResponse(payload, status)

        monkeypatch.setattr(c.session, "post", fake_post)
        return c, posts

    return build


# ── Request shape ───────────────────────────────────────────────────────────

def test_commands_are_posted_to_the_pipeline_endpoint(client):
    c, posts = client(payload=[{"result": 1}])
    c.pipeline(["GET", "k"])
    assert posts[0]["url"] == "https://db.upstash.io/pipeline"
    assert posts[0]["timeout"] == upstash.TIMEOUT


def test_the_auth_token_is_sent_as_a_bearer_header():
    c = upstash.UpstashClient("https://db.upstash.io", "secret-token")
    assert c.session.headers["Authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("url", [
    "https://db.upstash.io",
    "https://db.upstash.io/",
    "https://db.upstash.io///",
])
def test_trailing_slashes_do_not_double_up(url):
    assert upstash.UpstashClient(url, "t").base_url == "https://db.upstash.io"


def test_command_parts_are_stringified(client):
    """The REST API wants JSON strings, so ints must not be sent raw."""
    c, posts = client(payload=[{"result": "OK"}])
    c.pipeline(["SET", "k", "v", "EX", 3600])
    assert posts[0]["body"] == [["SET", "k", "v", "EX", "3600"]]


def test_several_commands_go_in_one_round_trip(client):
    c, posts = client(payload=[{"result": 1}, {"result": 1}])
    c.pipeline(["INCR", "k"], ["EXPIRE", "k", 60, "NX"])
    assert len(posts) == 1, "a pipeline is one request, not one per command"
    assert posts[0]["body"] == [["INCR", "k"], ["EXPIRE", "k", "60", "NX"]]


# ── Results ─────────────────────────────────────────────────────────────────

def test_results_are_returned_in_command_order(client):
    c, _ = client(payload=[{"result": 7}, {"result": 1}])
    assert c.pipeline(["INCR", "k"], ["EXPIRE", "k", 60]) == [7, 1]


def test_a_null_result_is_preserved(client):
    """Redis GET on a missing key returns null; that is a miss, not a failure."""
    c, _ = client(payload=[{"result": None}])
    assert c.pipeline(["GET", "missing"]) == [None]


def test_a_missing_result_field_becomes_none(client):
    c, _ = client(payload=[{"error": "ERR something"}])
    assert c.pipeline(["GET", "k"]) == [None]


# ── Fail-soft ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raises", [
    requests.ConnectionError("unreachable"),
    requests.Timeout("too slow"),
    OSError("socket blew up"),
])
def test_transport_failures_return_none(client, raises):
    c, _ = client(raises=raises)
    assert c.pipeline(["GET", "k"]) is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_errors_return_none(client, status):
    c, _ = client(payload=[], status=status)
    assert c.pipeline(["GET", "k"]) is None


def test_an_unparseable_body_returns_none(client):
    c, _ = client(payload=ValueError("not json"))
    assert c.pipeline(["GET", "k"]) is None


def test_an_unexpected_body_shape_returns_none(client):
    c, _ = client(payload={"not": "a list"})
    assert c.pipeline(["GET", "k"]) is None


def test_a_failure_is_logged(client, caplog):
    c, _ = client(raises=requests.ConnectionError("unreachable"))
    with caplog.at_level("WARNING"):
        c.pipeline(["GET", "k"])
    assert "Upstash unavailable" in caplog.text


# ── Construction from the environment ───────────────────────────────────────

def test_from_env_builds_a_client_when_both_credentials_are_set(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://db.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    c = upstash.from_env()
    assert isinstance(c, upstash.UpstashClient)
    assert c.base_url == "https://db.upstash.io"


@pytest.mark.parametrize("env", [
    {},
    {"UPSTASH_REDIS_REST_URL": "https://db.upstash.io"},
    {"UPSTASH_REDIS_REST_TOKEN": "tok"},
    {"UPSTASH_REDIS_REST_URL": "", "UPSTASH_REDIS_REST_TOKEN": "tok"},
])
def test_from_env_returns_none_without_both_credentials(monkeypatch, env):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert upstash.from_env() is None
