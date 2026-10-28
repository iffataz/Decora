"""Rate limiting: both backends, window behaviour, IP extraction, fail-open."""
import time

import pytest

import ratelimit
from ratelimit import InMemoryBackend, Limiter, UpstashBackend


@pytest.fixture
def limiter():
    """A Limiter over fresh in-process counters, configured per test."""
    def build(per_ip=10, per_ip_window=3600, global_daily=0, backend=None):
        return Limiter(
            backend=backend if backend is not None else InMemoryBackend(),
            per_ip=per_ip,
            per_ip_window=per_ip_window,
            global_daily=global_daily,
        )

    return build


# ── The per-IP limit ────────────────────────────────────────────────────────

def test_requests_up_to_the_limit_are_allowed_then_denied(limiter):
    rl = limiter(per_ip=3)
    assert [rl.check("1.2.3.4").allowed for _ in range(5)] == [True, True, True, False, False]


def test_each_ip_has_its_own_budget(limiter):
    rl = limiter(per_ip=1)
    assert rl.check("1.2.3.4").allowed is True
    assert rl.check("1.2.3.4").allowed is False
    assert rl.check("5.6.7.8").allowed is True


def test_a_denial_names_the_scope_and_a_retry_delay(limiter):
    rl = limiter(per_ip=1, per_ip_window=3600)
    rl.check("1.2.3.4")
    decision = rl.check("1.2.3.4")
    assert decision.allowed is False
    assert decision.scope == "ip"
    assert 0 < decision.retry_after <= 3600


def test_the_window_expires_and_releases(limiter):
    rl = limiter(per_ip=2, per_ip_window=1)
    assert [rl.check("9.9.9.9").allowed for _ in range(3)] == [True, True, False]
    time.sleep(1.1)
    assert rl.check("9.9.9.9").allowed is True


def test_a_per_ip_limit_of_zero_disables_the_check(limiter):
    rl = limiter(per_ip=0)
    assert all(rl.check("1.2.3.4").allowed for _ in range(50))


# ── The global daily cap ────────────────────────────────────────────────────

def test_the_global_cap_trips_across_distinct_ips(limiter):
    """The per-IP limit alone is not spend protection: IPs are cheap."""
    rl = limiter(per_ip=0, global_daily=3)
    verdicts = [rl.check(f"10.0.0.{i}").allowed for i in range(5)]
    assert verdicts == [True, True, True, False, False]


def test_a_global_denial_names_the_global_scope(limiter):
    rl = limiter(per_ip=0, global_daily=1)
    rl.check("10.0.0.1")
    decision = rl.check("10.0.0.2")
    assert decision.scope == "global"
    assert 0 < decision.retry_after <= ratelimit.GLOBAL_WINDOW


def test_a_global_limit_of_zero_disables_the_check(limiter):
    rl = limiter(per_ip=0, global_daily=0)
    assert all(rl.check(f"10.0.0.{i}").allowed for i in range(50))


def test_an_ip_denial_does_not_consume_global_quota(limiter):
    """Per-IP is checked first so a visitor over their own limit cannot burn
    through the shared daily budget."""
    rl = limiter(per_ip=1, global_daily=10)
    for _ in range(6):
        rl.check("7.7.7.7")
    assert rl.backend.count(rl.global_key()) == 1


def test_both_limits_apply_together(limiter):
    """Only allowed requests consume global quota, so the two limits compose:
    three searches get through in total, whichever IPs they come from."""
    rl = limiter(per_ip=2, global_daily=3)

    # 1.1.1.1 spends two global slots, then hits its own limit.
    assert [rl.check("1.1.1.1").allowed for _ in range(3)] == [True, True, False]
    assert rl.backend.count(rl.global_key()) == 2

    # 2.2.2.2 takes the last global slot, then is denied globally, not per-IP.
    assert rl.check("2.2.2.2").allowed is True
    second = rl.check("2.2.2.2")
    assert (second.allowed, second.scope) == (False, "global")

    # A fresh IP is now denied globally too, despite its own budget being clean.
    third = rl.check("3.3.3.3")
    assert (third.allowed, third.scope) == (False, "global")


# ── The in-process backend ──────────────────────────────────────────────────

def test_in_memory_counters_are_pruned_so_they_cannot_leak(limiter):
    """Every distinct client IP adds a key, so unbounded growth is a slow leak."""
    backend = InMemoryBackend()
    for i in range(backend._MAX_KEYS + 500):
        backend.incr(f"key-{i}", 1)
    assert len(backend._windows) > backend._MAX_KEYS
    time.sleep(1.1)
    backend.incr("trigger-the-prune", 60)
    assert len(backend._windows) < 100


def test_in_memory_count_reports_zero_for_an_unknown_key():
    assert InMemoryBackend().count("never-seen") == 0


def test_in_memory_count_reports_zero_once_the_window_has_passed():
    backend = InMemoryBackend()
    backend.incr("k", 1)
    assert backend.count("k") == 1
    time.sleep(1.1)
    assert backend.count("k") == 0


# ── The Upstash backend ─────────────────────────────────────────────────────

@pytest.fixture
def upstash_backend():
    """An UpstashBackend over a fake client; returns it plus the commands sent."""
    sent = []
    counter = {"n": 0}

    class FakeClient:
        def pipeline(self, *commands):
            sent.append(list(commands))
            counter["n"] += 1
            return [counter["n"], 1]

    return UpstashBackend(FakeClient()), sent


def test_upstash_is_chosen_when_credentials_are_present(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://db.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert isinstance(ratelimit.make_backend(), UpstashBackend)


@pytest.mark.parametrize("env", [
    {},
    {"UPSTASH_REDIS_REST_URL": "https://db.upstash.io"},   # token missing
    {"UPSTASH_REDIS_REST_TOKEN": "tok"},                   # url missing
])
def test_in_process_is_chosen_without_both_credentials(monkeypatch, env):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert isinstance(ratelimit.make_backend(), InMemoryBackend)


def test_upstash_enforces_the_limit(limiter, upstash_backend):
    backend, _ = upstash_backend
    rl = limiter(per_ip=2, backend=backend)
    assert [rl.check("4.4.4.4").allowed for _ in range(4)] == [True, True, False, False]


def test_upstash_sends_incr_then_a_non_extending_expire(limiter, upstash_backend):
    """EXPIRE .. NX anchors the window to its first request instead of sliding
    it forward on every hit, which would never let the window close."""
    backend, sent = upstash_backend
    limiter(per_ip=5, per_ip_window=3600, backend=backend).check("4.4.4.4")
    incr, expire = sent[0]
    assert incr[0] == "INCR"
    assert expire[0] == "EXPIRE"
    assert expire[1] == incr[1], "both commands must target the same key"
    assert expire[2] == 3600
    assert expire[3] == "NX"


@pytest.mark.parametrize("returned", [None, [], [None, None]])
def test_an_unusable_backend_reply_fails_open(limiter, returned):
    """A limiter outage must not take the site down. The trade is that an
    attacker who can break Redis gets past the limit."""
    class DeadClient:
        def pipeline(self, *commands):
            return returned

    rl = limiter(per_ip=1, backend=UpstashBackend(DeadClient()))
    assert all(rl.check("4.4.4.4").allowed for _ in range(5))


def test_failing_open_is_logged(limiter, caplog):
    class DeadClient:
        def pipeline(self, *commands):
            return None

    with caplog.at_level("WARNING"):
        limiter(per_ip=1, backend=UpstashBackend(DeadClient())).check("4.4.4.4")
    assert "backend unavailable" in caplog.text


# ── Configuration from the environment ──────────────────────────────────────

def test_limits_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_IP", "7")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("RATE_LIMIT_GLOBAL_PER_DAY", "99")
    rl = Limiter.from_env(backend=InMemoryBackend())
    assert (rl.per_ip, rl.per_ip_window, rl.global_daily) == (7, 60, 99)


@pytest.mark.parametrize("raw", ["not-a-number", "1.5", "", "   "])
def test_a_bad_env_value_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("RATE_LIMIT_PER_IP", raw)
    assert ratelimit._int_env("RATE_LIMIT_PER_IP", 10) == 10


def test_an_unset_env_value_uses_the_default(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_PER_IP", raising=False)
    assert ratelimit._int_env("RATE_LIMIT_PER_IP", 10) == 10


def test_the_documented_defaults(monkeypatch):
    """These numbers are quoted in the README and .env.example."""
    for name in ("RATE_LIMIT_PER_IP", "RATE_LIMIT_WINDOW_SECONDS", "RATE_LIMIT_GLOBAL_PER_DAY"):
        monkeypatch.delenv(name, raising=False)
    rl = Limiter.from_env(backend=InMemoryBackend())
    assert (rl.per_ip, rl.per_ip_window, rl.global_daily) == (10, 3600, 200)


# ── Window keys ─────────────────────────────────────────────────────────────

def test_the_window_key_changes_between_windows(limiter, monkeypatch):
    rl = limiter(per_ip_window=100)
    monkeypatch.setattr(ratelimit.time, "time", lambda: 1000.0)
    first = rl.ip_key("1.2.3.4")
    monkeypatch.setattr(ratelimit.time, "time", lambda: 1100.0)
    assert rl.ip_key("1.2.3.4") != first


def test_the_window_key_is_stable_within_a_window(limiter, monkeypatch):
    rl = limiter(per_ip_window=100)
    monkeypatch.setattr(ratelimit.time, "time", lambda: 1000.0)
    first = rl.ip_key("1.2.3.4")
    monkeypatch.setattr(ratelimit.time, "time", lambda: 1099.0)
    assert rl.ip_key("1.2.3.4") == first


def test_ip_and_global_keys_do_not_collide(limiter):
    rl = limiter()
    assert rl.ip_key("all") != rl.global_key()


# ── Client address extraction ───────────────────────────────────────────────

class FakeRequest:
    def __init__(self, headers=None, remote_addr="203.0.113.9"):
        self.headers = headers or {}
        self.remote_addr = remote_addr


def test_x_real_ip_is_preferred():
    req = FakeRequest({"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "198.51.100.8"})
    assert ratelimit.client_ip(req, trust_proxy=True) == "198.51.100.7"


def test_the_leftmost_forwarded_for_entry_is_the_client():
    req = FakeRequest({"X-Forwarded-For": "198.51.100.8, 10.0.0.1, 10.0.0.2"})
    assert ratelimit.client_ip(req, trust_proxy=True) == "198.51.100.8"


@pytest.mark.parametrize("headers", [
    {},
    {"X-Real-IP": ""},
    {"X-Real-IP": "   "},
    {"X-Forwarded-For": ""},
])
def test_empty_proxy_headers_fall_back_to_the_socket_address(headers):
    assert ratelimit.client_ip(FakeRequest(headers), trust_proxy=True) == "203.0.113.9"


def test_proxy_headers_are_ignored_when_not_trusted():
    """Exposed directly, a client can send these itself, which would let it
    rotate its way around the per-IP limit at will."""
    req = FakeRequest({"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "1.2.3.4"})
    assert ratelimit.client_ip(req, trust_proxy=False) == "203.0.113.9"


def test_no_address_at_all_is_handled():
    assert ratelimit.client_ip(FakeRequest({}, remote_addr=None), trust_proxy=True) == "unknown"


@pytest.mark.parametrize("value,expected", [("0", False), ("1", True), ("", True), ("yes", True)])
def test_trust_proxy_headers_env(monkeypatch, value, expected):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", value)
    assert ratelimit.trust_proxy_headers() is expected


def test_trust_proxy_headers_defaults_to_true(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    assert ratelimit.trust_proxy_headers() is True


# ── Wait text ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,expected", [
    (1, "about a minute"),
    (30, "about a minute"),
    (90, "about a minute"),
    (300, "about 5 minutes"),
    (3540, "about 59 minutes"),
    (3600, "about an hour"),
    (7200, "about 2 hours"),
    (86400, "about 24 hours"),
])
def test_describe_wait(seconds, expected):
    assert ratelimit.describe_wait(seconds) == expected
