"""Image fetching: the SSRF guard, redirect walking and the streamed size cap."""
import io

import pytest
import requests
from PIL import Image, UnidentifiedImageError

import decora


class FakeResponse:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308) and "Location" in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── The SSRF guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://[fd00:ec2::254]/latest/meta-data/",   # IPv6 metadata
    "http://127.0.0.1:5000/",                     # loopback
    "http://localhost/room.png",
    "http://[::1]/room.png",                      # IPv6 loopback
    "http://10.0.0.1/room.png",                   # RFC1918
    "http://172.16.0.1/room.png",
    "http://192.168.1.1/room.png",
    "http://0.0.0.0/",                            # unspecified
    "http://255.255.255.255/",                    # broadcast
    "http://224.0.0.1/",                          # multicast
])
def test_private_and_special_addresses_are_refused(url):
    with pytest.raises(ValueError):
        decora._assert_public_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/room.png",
    "gopher://example.com/",
    "//example.com/room.png",       # scheme-relative, no scheme
])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(ValueError, match="http"):
        decora._assert_public_url(url)


def test_url_without_a_host_is_refused():
    with pytest.raises(ValueError, match="no host"):
        decora._assert_public_url("http:///room.png")


@pytest.mark.parametrize("url", [
    "https://1.1.1.1/room.jpg",
    "http://8.8.8.8/room.png",
    "https://93.184.216.34:8443/room.png",
])
def test_public_addresses_are_allowed(url):
    decora._assert_public_url(url)  # must not raise


def test_unresolvable_host_is_refused():
    with pytest.raises(ValueError, match="could not resolve"):
        decora._assert_public_url("http://no-such-host.invalid/room.png")


def test_a_host_resolving_to_both_public_and_private_is_refused(monkeypatch):
    """One private address among several is enough to refuse the whole host."""
    def fake_getaddrinfo(host, port, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(decora.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        decora._assert_public_url("http://split-horizon.test/room.png")


# ── Redirect walking ────────────────────────────────────────────────────────

@pytest.fixture
def fetch_chain(monkeypatch):
    """Serve a url -> FakeResponse map, recording the urls actually requested."""
    def install(chain):
        requested = []

        def fake_get(url, **kwargs):
            assert kwargs.get("allow_redirects") is False, (
                "redirects must not be auto-followed, or a public URL could "
                "bounce to a private one unchecked"
            )
            requested.append(url)
            return chain[url]

        monkeypatch.setattr(decora.requests, "get", fake_get)
        return requested

    return install


def test_fetches_a_public_image(fetch_chain, png_bytes):
    requested = fetch_chain({"http://8.8.8.8/a.png": FakeResponse(body=png_bytes)})
    img = decora.fetch_user_image("http://8.8.8.8/a.png")
    assert img.size == (40, 30)
    assert img.mode == "RGB"
    assert requested == ["http://8.8.8.8/a.png"]


def test_follows_a_redirect_between_public_hosts(fetch_chain, png_bytes):
    requested = fetch_chain({
        "http://8.8.8.8/a.png": FakeResponse(302, {"Location": "http://1.1.1.1/b.png"}),
        "http://1.1.1.1/b.png": FakeResponse(body=png_bytes),
    })
    assert decora.fetch_user_image("http://8.8.8.8/a.png").size == (40, 30)
    assert requested == ["http://8.8.8.8/a.png", "http://1.1.1.1/b.png"]


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:5000/admin",
    "http://10.0.0.5/internal",
])
def test_a_redirect_to_a_private_address_is_refused_before_it_is_requested(fetch_chain, target):
    """The whole point of walking redirects by hand: the second hop is never issued."""
    requested = fetch_chain({
        "http://8.8.8.8/a.png": FakeResponse(302, {"Location": target}),
        target: FakeResponse(body=b"SECRET"),
    })
    with pytest.raises(ValueError, match="non-public"):
        decora.fetch_user_image("http://8.8.8.8/a.png")
    assert requested == ["http://8.8.8.8/a.png"], "the private hop was requested!"


def test_relative_redirect_is_resolved_against_the_current_url(fetch_chain, png_bytes):
    requested = fetch_chain({
        "http://8.8.8.8/dir/a.png": FakeResponse(302, {"Location": "/other/b.png"}),
        "http://8.8.8.8/other/b.png": FakeResponse(body=png_bytes),
    })
    assert decora.fetch_user_image("http://8.8.8.8/dir/a.png").size == (40, 30)
    assert requested[-1] == "http://8.8.8.8/other/b.png"


def test_redirect_loop_terminates(fetch_chain):
    requested = fetch_chain({
        "http://8.8.8.8/a.png": FakeResponse(302, {"Location": "http://8.8.8.8/a.png"}),
    })
    with pytest.raises(ValueError, match="too many redirects"):
        decora.fetch_user_image("http://8.8.8.8/a.png")
    assert len(requested) == decora.MAX_REDIRECTS + 1


def test_redirect_without_a_location_is_an_error(fetch_chain):
    fetch_chain({"http://8.8.8.8/a.png": FakeResponse(302, {"Location": ""})})
    with pytest.raises(ValueError):
        decora.fetch_user_image("http://8.8.8.8/a.png")


def test_http_error_propagates(fetch_chain):
    fetch_chain({"http://8.8.8.8/a.png": FakeResponse(404)})
    with pytest.raises(requests.HTTPError):
        decora.fetch_user_image("http://8.8.8.8/a.png")


# ── The streamed size cap ───────────────────────────────────────────────────

def test_oversize_content_length_is_refused_without_reading_the_body():
    resp = FakeResponse(headers={"Content-Length": str(decora.MAX_IMAGE_BYTES + 1)})
    with pytest.raises(ValueError, match="too large"):
        decora._read_image(resp)


def test_oversize_body_is_refused_when_content_length_is_omitted():
    """Content-Length is only a hint, so the running total is what enforces the cap."""
    resp = FakeResponse(body=b"x" * (decora.MAX_IMAGE_BYTES + 1024))
    with pytest.raises(ValueError, match="too large"):
        decora._read_image(resp)


def test_oversize_body_is_refused_when_content_length_lies():
    resp = FakeResponse(
        headers={"Content-Length": "10"},
        body=b"x" * (decora.MAX_IMAGE_BYTES + 1024),
    )
    with pytest.raises(ValueError, match="too large"):
        decora._read_image(resp)


def test_non_numeric_content_length_does_not_crash(png_bytes):
    resp = FakeResponse(headers={"Content-Length": "banana"}, body=png_bytes)
    assert decora._read_image(resp).size == (40, 30)


def test_a_body_at_the_cap_is_accepted(png_bytes):
    assert len(png_bytes) < decora.MAX_IMAGE_BYTES
    resp = FakeResponse(headers={"Content-Length": str(len(png_bytes))}, body=png_bytes)
    assert decora._read_image(resp).size == (40, 30)


def test_malformed_body_fails_at_fetch_time_not_later():
    """img.load() forces the decode here, so a bad body can't blow up mid-prompt."""
    with pytest.raises(UnidentifiedImageError):
        decora._read_image(FakeResponse(body=b"this is not an image"))


def test_palette_image_is_converted_to_rgb():
    buf = io.BytesIO()
    Image.new("P", (20, 20)).save(buf, "PNG")
    assert decora._read_image(FakeResponse(body=buf.getvalue())).mode == "RGB"


def test_decompression_bomb_limit_is_set():
    """A byte cap alone won't stop a small file decoding to gigabytes of pixels."""
    assert Image.MAX_IMAGE_PIXELS is not None
    assert Image.MAX_IMAGE_PIXELS <= 50_000_000


# ── _prepare ────────────────────────────────────────────────────────────────

def test_prepare_downscales_to_the_gemini_bound():
    out = decora._prepare(Image.new("RGB", (2000, 1500)))
    assert max(out.size) <= 768
    assert out.size == (768, 576), "aspect ratio should be preserved"


def test_prepare_does_not_mutate_the_callers_image():
    """room_img is prepared by analyse_room and reused by score_products."""
    original = Image.new("RGB", (2000, 1500))
    decora._prepare(original)
    assert original.size == (2000, 1500)


def test_prepare_leaves_a_small_image_alone():
    out = decora._prepare(Image.new("RGB", (100, 80)))
    assert out.size == (100, 80)


# ── The trusted path keeps the size cap but skips host validation ───────────

def test_trusted_fetch_does_not_validate_the_host(monkeypatch, png_bytes):
    """IKEA's CDN is trusted, so _fetch_image must not pay for DNS validation."""
    called = []
    monkeypatch.setattr(decora, "_assert_public_url",
                        lambda url: called.append(url))
    monkeypatch.setattr(decora.requests, "get",
                        lambda url, **kw: FakeResponse(body=png_bytes))
    assert decora._fetch_image("https://ikea.test/a.jpg").size == (40, 30)
    assert called == []


def test_trusted_fetch_still_enforces_the_size_cap(monkeypatch):
    monkeypatch.setattr(
        decora.requests, "get",
        lambda url, **kw: FakeResponse(body=b"x" * (decora.MAX_IMAGE_BYTES + 1)),
    )
    with pytest.raises(ValueError, match="too large"):
        decora._fetch_image("https://ikea.test/a.jpg")


# ── Address classification, independent of DNS ──────────────────────────────

@pytest.mark.parametrize("address", [
    "0.0.0.0",              # unspecified
    "127.0.0.1",            # loopback
    "10.0.0.1",             # RFC1918
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",      # link-local / cloud metadata
    "100.64.0.1",           # CGNAT shared space
    "192.0.2.1",            # documentation range
    "224.0.0.1",            # multicast (still reports is_global=True)
    "239.255.255.250",
    "240.0.0.1",            # reserved
    "255.255.255.255",      # broadcast
    "::",                   # IPv6 unspecified
    "::1",                  # IPv6 loopback
    "fe80::1",              # IPv6 link-local
    "fd00:ec2::254",        # IPv6 unique-local
    "ff02::1",              # IPv6 multicast
])
def test_non_public_addresses_are_classified_as_such(address):
    """Pinned per-address so a stdlib change to is_global can't silently widen
    what the URL field can reach."""
    assert decora._is_public_address(address) is False


@pytest.mark.parametrize("address", [
    "1.1.1.1",
    "8.8.8.8",
    "93.184.216.34",
    "2606:4700:4700::1111",
])
def test_public_addresses_are_classified_as_public(address):
    assert decora._is_public_address(address) is True
