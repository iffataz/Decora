"""The result store: tokens, TTLs, the search-key index, and both backends."""
import json
import time

import pytest
from PIL import Image

import resultstore
from resultstore import InMemoryBackend, ResultStore, UpstashBackend

ITEMS = [
    {"name": "KIVIK", "url": "https://ikea.test/p/1", "image": "https://ikea.test/1.jpg",
     "score": 9, "reason": "Its oak frame echoes the floor.",
     "price": 899, "rating": 4.5, "rating_count": 12},
]


@pytest.fixture
def store():
    def build(ttl=None, backend=None):
        return ResultStore(backend=backend or InMemoryBackend(), ttl=ttl)

    return build


# ── Round trip ──────────────────────────────────────────────────────────────

def test_results_come_back_by_token(store):
    s = store()
    token = s.put_results(ITEMS)
    assert s.get(token) == {"items": ITEMS}


def test_an_unknown_token_is_a_miss(store):
    assert store().get("no-such-token") is None


def test_results_survive_repeated_reads(store):
    """A refresh must not consume the stored result."""
    s = store()
    token = s.put_results(ITEMS)
    assert all(s.get(token) == {"items": ITEMS} for _ in range(5))


def test_each_search_gets_its_own_token(store):
    s = store()
    assert s.put_results(ITEMS) != s.put_results(ITEMS)


def test_errors_are_stored_and_returned_as_errors(store):
    s = store()
    token = s.put_error("No matches yet", "Try a different photo.")
    assert s.get(token) == {"error": {"heading": "No matches yet", "message": "Try a different photo."}}


def test_an_error_with_no_heading_round_trips(store):
    s = store()
    payload = s.get(s.put_error(None, "Something went wrong."))
    assert payload["error"]["heading"] is None
    assert payload["error"]["message"] == "Something went wrong."


def test_an_unreadable_stored_value_is_a_miss(store):
    """Corrupt or foreign data in the store must not raise into the route."""
    backend = InMemoryBackend()
    s = store(backend=backend)
    backend.set(resultstore.RESULT_PREFIX + "tok", "}{ not json", 60)
    assert s.get("tok") is None


def test_a_stored_non_object_is_a_miss(store):
    backend = InMemoryBackend()
    s = store(backend=backend)
    backend.set(resultstore.RESULT_PREFIX + "tok", json.dumps([1, 2, 3]), 60)
    assert s.get("tok") is None


# ── Tokens ──────────────────────────────────────────────────────────────────

def test_tokens_are_unguessable_and_url_safe():
    tokens = {resultstore.new_token() for _ in range(500)}
    assert len(tokens) == 500, "tokens must not repeat"
    for token in tokens:
        assert len(token) >= 16
        assert all(c.isalnum() or c in "-_" for c in token)


# ── The search-key index ────────────────────────────────────────────────────

def test_an_identical_search_finds_the_stored_token(store):
    s = store()
    token = s.put_results(ITEMS, key="abc123")
    assert s.token_for("abc123") == token


def test_an_unknown_search_key_is_a_miss(store):
    assert store().token_for("never-seen") is None


def test_results_stored_without_a_key_are_not_indexed(store):
    s = store()
    s.put_results(ITEMS)
    assert s.token_for("abc123") is None


def test_errors_are_never_indexed_by_search_key(store):
    """A Gemini outage must not be remembered as this room's answer."""
    s = store()
    s.put_error("Busy", "Try again in a minute.")
    assert s.token_for("abc123") is None


def test_a_dangling_index_does_not_hand_out_a_dead_link(store):
    """If the result is gone but the index survives, report a miss so the
    visitor runs a fresh search instead of landing on an expired page."""
    backend = InMemoryBackend()
    s = store(backend=backend)
    token = s.put_results(ITEMS, key="abc123")
    backend._entries.pop(resultstore.RESULT_PREFIX + token)
    assert s.token_for("abc123") is None


def test_a_later_identical_search_can_replace_the_index(store):
    s = store()
    first = s.put_results(ITEMS, key="abc123")
    second = s.put_results(ITEMS, key="abc123")
    assert first != second
    assert s.token_for("abc123") == second


# ── TTLs ────────────────────────────────────────────────────────────────────

def test_results_expire(store):
    s = store(ttl=1)
    token = s.put_results(ITEMS, key="abc123")
    assert s.get(token) is not None
    time.sleep(1.1)
    assert s.get(token) is None
    assert s.token_for("abc123") is None


def test_errors_expire_sooner_than_results(store):
    """Short-lived on purpose: an outage shouldn't stick around for a day."""
    assert resultstore.ERROR_TTL < resultstore.DEFAULT_TTL


def test_the_error_ttl_is_used_for_errors(store):
    backend = InMemoryBackend()
    s = store(ttl=99_999, backend=backend)
    ttls = []
    original = backend.set
    backend.set = lambda key, value, ttl: (ttls.append(ttl), original(key, value, ttl))[1]
    s.put_error("h", "m")
    assert ttls == [resultstore.ERROR_TTL]


def test_the_documented_default_ttl(monkeypatch):
    """Quoted in the README."""
    monkeypatch.delenv("RESULT_TTL_SECONDS", raising=False)
    assert ResultStore.from_env(backend=InMemoryBackend()).ttl == 86_400


def test_the_ttl_is_configurable(monkeypatch):
    monkeypatch.setenv("RESULT_TTL_SECONDS", "600")
    assert ResultStore.from_env(backend=InMemoryBackend()).ttl == 600


@pytest.mark.parametrize("raw", ["banana", "", "1.5"])
def test_a_bad_ttl_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("RESULT_TTL_SECONDS", raw)
    assert ResultStore.from_env(backend=InMemoryBackend()).ttl == 86_400


# ── search_key ──────────────────────────────────────────────────────────────

def red_room(size=(400, 300)):
    return Image.new("RGB", size, "red")


def test_the_same_image_and_type_give_the_same_key():
    assert resultstore.search_key(red_room(), "sofa") == resultstore.search_key(red_room(), "sofa")


def test_a_different_furniture_type_gives_a_different_key():
    assert resultstore.search_key(red_room(), "sofa") != resultstore.search_key(red_room(), "lamp")


def test_a_different_image_gives_a_different_key():
    blue = Image.new("RGB", (400, 300), "blue")
    assert resultstore.search_key(red_room(), "sofa") != resultstore.search_key(blue, "sofa")


@pytest.mark.parametrize("variant", ["sofa", "SOFA", "  Sofa  ", "sOfA"])
def test_the_furniture_type_is_normalised(variant):
    """"Sofa" and "sofa" are the same search, so they should share a result."""
    assert resultstore.search_key(red_room(), variant) == resultstore.search_key(red_room(), "sofa")


def test_the_key_does_not_mutate_the_image():
    """The same image object goes on to the Gemini calls."""
    img = red_room((2000, 1500))
    resultstore.search_key(img, "sofa")
    assert img.size == (2000, 1500)


def test_a_non_rgb_image_is_handled():
    resultstore.search_key(Image.new("P", (40, 30)), "sofa")  # must not raise


def test_the_key_is_a_hex_digest():
    key = resultstore.search_key(red_room(), "sofa")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


# ── The in-process backend ──────────────────────────────────────────────────

def test_in_memory_round_trip():
    backend = InMemoryBackend()
    backend.set("k", "v", 60)
    assert backend.get("k") == "v"


def test_in_memory_expiry():
    backend = InMemoryBackend()
    backend.set("k", "v", 1)
    time.sleep(1.1)
    assert backend.get("k") is None


def test_in_memory_misses_are_none():
    assert InMemoryBackend().get("nope") is None


def test_in_memory_is_bounded_even_with_long_lived_entries():
    """Entries live for a day, so without hard eviction a busy instance would
    hold every result it ever served."""
    backend = InMemoryBackend()
    for i in range(backend._MAX_ENTRIES * 2):
        backend.set(f"k{i}", "v", 86_400)
    assert len(backend._entries) <= backend._MAX_ENTRIES


def test_in_memory_prefers_dropping_expired_entries():
    backend = InMemoryBackend()
    for i in range(backend._MAX_ENTRIES - 1):
        backend.set(f"old{i}", "v", 1)
    backend.set("keep", "v", 86_400)
    time.sleep(1.1)
    backend.set("trigger", "v", 86_400)
    assert backend.get("keep") == "v", "a live entry was evicted before expired ones"
    assert len(backend._entries) < 100


# ── The Upstash backend ─────────────────────────────────────────────────────

class FakeClient:
    """Stands in for UpstashClient with a dict for storage."""

    def __init__(self, fail=False):
        self.data = {}
        self.fail = fail
        self.commands = []

    def pipeline(self, *commands):
        self.commands.extend(commands)
        if self.fail:
            return None
        command = commands[0]
        verb = command[0]
        if verb == "SET":
            self.data[command[1]] = command[2]
            return ["OK"]
        if verb == "GET":
            return [self.data.get(command[1])]
        raise AssertionError(f"unexpected command {verb}")


def test_upstash_is_chosen_when_credentials_are_present(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://db.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert isinstance(resultstore.make_backend(), UpstashBackend)


def test_in_process_is_chosen_without_credentials(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert isinstance(resultstore.make_backend(), InMemoryBackend)


def test_upstash_round_trip(store):
    s = store(backend=UpstashBackend(FakeClient()))
    token = s.put_results(ITEMS, key="abc123")
    assert s.get(token) == {"items": ITEMS}
    assert s.token_for("abc123") == token


def test_upstash_sets_an_expiry(store):
    client = FakeClient()
    store(ttl=1234, backend=UpstashBackend(client)).put_results(ITEMS)
    command = client.commands[0]
    assert command[0] == "SET"
    assert command[3] == "EX"
    assert command[4] == 1234


def test_an_upstash_outage_reads_as_a_miss(store):
    """Degrade to "your link expired", which is recoverable."""
    s = store(backend=UpstashBackend(FakeClient(fail=True)))
    token = s.put_results(ITEMS, key="abc123")
    assert s.get(token) is None
    assert s.token_for("abc123") is None


def test_a_failed_write_is_logged(store, caplog):
    s = store(backend=UpstashBackend(FakeClient(fail=True)))
    with caplog.at_level("WARNING"):
        s.put_results(ITEMS)
    assert "could not store results" in caplog.text


def test_a_failed_write_still_returns_a_token(store):
    """The route redirects regardless; the visitor gets the expired page rather
    than a 500 on a search that actually succeeded."""
    s = store(backend=UpstashBackend(FakeClient(fail=True)))
    assert s.put_results(ITEMS)
