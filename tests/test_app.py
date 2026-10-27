"""Flask routes: validation, rendering, error handling, rate limit responses."""
import io
import re

import pytest
from google.genai import errors as genai_errors
from PIL import Image

import ratelimit
from ratelimit import InMemoryBackend, Limiter

RESULT_ROWS = [
    dict(name="KIVIK", url="https://ikea.test/p/1", image="https://ikea.test/1.jpg",
         score=9, reason="Its oak frame echoes the floor.",
         price=899, rating=4.5, rating_count=12),
    dict(name="NOCTURNE", url="https://ikea.test/p/2", image="https://ikea.test/2.jpg",
         score=6, reason="Fine shape, cooler tone.",
         price=None, rating=None, rating_count=None),
]


@pytest.fixture
def search(app_client, webapp, monkeypatch, room_image):
    """POST a valid search with the fetch and pipeline faked."""
    def run(rows=RESULT_ROWS, pipeline_error=None, art="sofa", headers=None, **form):
        def fake_pipeline(img, furniture_type):
            if pipeline_error is not None:
                raise pipeline_error
            return rows

        monkeypatch.setattr(webapp.decora, "fetch_user_image", lambda url: room_image)
        monkeypatch.setattr(webapp, "gen_everything", fake_pipeline)
        data = dict(art=art, url="http://example.test/room.png")
        data.update(form)
        return app_client.post("/process_url", data=data, headers=headers or {})

    return run


def text(response):
    return response.get_data(as_text=True)


# ── Home page and static assets ─────────────────────────────────────────────

def test_home_page_renders(app_client):
    assert app_client.get("/").status_code == 200


def test_home_page_links_the_single_stylesheet(app_client):
    body = text(app_client.get("/"))
    assert re.search(r'href="[^"]*styles\.css"', body)
    assert re.search(r'href="[^"]*favicon\.svg"', body)


@pytest.mark.parametrize("pattern", [
    r'(?:src|href)="[^"]*bootstrap',
    r'(?:src|href)="[^"]*jquery',
    r'(?:src|href)="[^"]*styles1\.css',
    r'(?:src|href)="[^"]*/style\.css',
    r'class="[^"]*spinner-border',
    r"X-UA-Compatible",
])
def test_no_references_to_the_deleted_front_end(app_client, pattern):
    assert not re.search(pattern, text(app_client.get("/")), re.I)


@pytest.mark.parametrize("path,content_type", [
    ("/stat/styles.css", "text/css"),
    ("/stat/favicon.svg", "svg"),
])
def test_static_assets_are_served(app_client, path, content_type):
    """Flask derives static_url_path from the folder basename, so it is /stat."""
    response = app_client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["Content-Type"]


@pytest.mark.parametrize("path", [
    "/stat/style.css",
    "/stat/styles1.css",
    "/stat/bootstrap.min.css",
    "/stat/js/jquery-3.3.1.min.js",
    "/stat/js/bootstrap.min.js",
])
def test_deleted_assets_are_gone(app_client, path):
    assert app_client.get(path).status_code == 404


def test_the_dead_result_route_is_removed(app_client):
    assert app_client.get("/result").status_code == 404


def test_the_loading_overlay_is_announced_to_screen_readers(app_client):
    body = text(app_client.get("/"))
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body
    assert "hidden" in body, "the overlay starts hidden"


# ── Input validation ────────────────────────────────────────────────────────

def post(app_client, **data):
    return app_client.post("/process_url", data=data, follow_redirects=True)


def test_neither_photo_nor_url_is_rejected(app_client):
    assert "upload a room photo" in text(post(app_client, art="sofa")).lower()


def test_both_photo_and_url_is_rejected(app_client):
    body = text(post(app_client, art="sofa", url="http://example.test/a.png",
                     photo=(io.BytesIO(b"x"), "a.png")))
    assert "not both" in body


@pytest.mark.parametrize("art", [
    "", "   ", "a" * 51, "sofa<script>", "sofa; DROP TABLE", "söfa",
    "sofa\nbad", "sofa\tbad", "sofa_bed", "sofa-bed",
])
def test_invalid_furniture_types_are_rejected(app_client, art):
    body = text(post(app_client, art=art, url="http://example.test/a.png"))
    assert "valid furniture type" in body


@pytest.mark.parametrize("art", ["  sofa  ", "sofa\n", "\tsofa ", "sofa\t"])
def test_surrounding_whitespace_is_trimmed_rather_than_rejected(search, art):
    """People paste with stray whitespace; the route strips before validating."""
    assert search(art=art).status_code == 200


@pytest.mark.parametrize("art", ["sofa", "dining table", "2 seater sofa", "a" * 50])
def test_valid_furniture_types_are_accepted(search, art):
    """Digits are allowed, which the flash message now says."""
    assert search(art=art).status_code == 200


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.test/a.png", "not-a-url", "javascript:alert(1)",
])
def test_non_http_urls_get_a_useful_message(app_client, url):
    body = text(post(app_client, art="sofa", url=url))
    assert "starting with http" in body


def test_a_non_image_upload_is_rejected(app_client):
    body = text(post(app_client, art="sofa", photo=(io.BytesIO(b"not an image"), "a.txt")))
    assert "valid image file" in body


def test_a_corrupt_image_upload_is_rejected(app_client):
    """An image/* mimetype is not proof it decodes."""
    body = text(post(app_client, art="sofa",
                     photo=(io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"garbage"), "a.png")))
    assert "valid image" in body


def test_a_valid_upload_is_accepted(app_client, webapp, monkeypatch):
    monkeypatch.setattr(webapp, "gen_everything", lambda img, art: RESULT_ROWS)
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "blue").save(buf, "PNG")
    buf.seek(0)
    response = app_client.post("/process_url",
                               data=dict(art="sofa", photo=(buf, "room.png")))
    assert response.status_code == 200
    assert "KIVIK" in text(response)


def test_an_oversized_upload_is_rejected(app_client):
    big = io.BytesIO(b"x" * (9 * 1024 * 1024))
    body = text(post(app_client, art="sofa", photo=(big, "big.png")))
    assert "too large" in body.lower()


def test_an_unfetchable_url_is_reported_generically(app_client, webapp, monkeypatch):
    monkeypatch.setattr(webapp.decora, "fetch_user_image",
                        lambda url: (_ for _ in ()).throw(ValueError("boom")))
    body = text(post(app_client, art="sofa", url="http://example.test/a.png"))
    assert "could not load an image" in body.lower()


def test_an_ssrf_rejection_does_not_leak_why(app_client, webapp, monkeypatch):
    """A message distinguishing "blocked address" from "fetch failed" would turn
    the field into a port scanner."""
    monkeypatch.setattr(
        webapp.decora, "fetch_user_image",
        lambda url: (_ for _ in ()).throw(ValueError("URL resolves to a non-public address")),
    )
    body = text(post(app_client, art="sofa", url="http://169.254.169.254/latest/meta-data/"))
    assert "could not load an image" in body.lower()
    assert "non-public" not in body
    assert "169.254" not in body


# ── Results rendering ───────────────────────────────────────────────────────

def test_results_render_each_product(search):
    body = text(search())
    assert "KIVIK" in body
    assert "NOCTURNE" in body
    assert "Its oak frame echoes the floor." in body


def test_the_score_bar_width_tracks_the_score(search):
    assert "width: 90%" in text(search())


def test_price_and_rating_are_shown_when_present(search):
    body = text(search())
    assert "AU $899" in body
    assert "4.5" in body
    assert "(12)" in body


def test_absent_price_and_rating_render_nothing_rather_than_nan(search):
    """NOCTURNE has no price or rating; pandas NaN would print as "nan"."""
    body = text(search())
    assert body.count("product-tile__price") == 1
    assert body.count("product-tile__rating") == 1
    assert "nan" not in body.lower()


def test_product_links_open_safely(search):
    assert 'rel="noopener"' in text(search())


def test_the_score_bar_is_hidden_from_assistive_tech(search):
    """The adjacent label already reads "9/10 match" as text."""
    body = text(search())
    assert 'aria-hidden="true"' in body
    assert "9/10 match" in body


# ── Error paths ─────────────────────────────────────────────────────────────

def test_no_matches_shows_a_helpful_page(search):
    body = text(search(rows=[]))
    assert "No matching products found" in body


def test_a_gemini_outage_shows_a_friendly_message(search):
    err = genai_errors.APIError.__new__(genai_errors.APIError)
    err.code, err.message, err.status = 503, "overloaded", "UNAVAILABLE"
    body = text(search(pipeline_error=err))
    assert "AI stylist is busy" in body


def test_an_unexpected_error_does_not_leak_internals(search):
    """Exception text here can carry upstream URLs and keys."""
    leaky = RuntimeError("IKEA API unavailable: https://internal.test/x?key=abc123")
    body = text(search(pipeline_error=leaky))
    assert "Something went wrong on our end" in body
    assert "internal.test" not in body
    assert "abc123" not in body


def test_the_error_page_heading_defaults(app_client, webapp, monkeypatch):
    monkeypatch.setattr(webapp, "gen_everything", lambda img, art: [])
    monkeypatch.setattr(webapp.decora, "fetch_user_image",
                        lambda url: Image.new("RGB", (10, 10)))
    body = text(app_client.post("/process_url",
                                data=dict(art="sofa", url="http://example.test/a.png")))
    assert "Something went wrong" in body


# ── Rate limiting through the route ─────────────────────────────────────────

@pytest.fixture
def metered(monkeypatch):
    """Install a Limiter with given limits as the app's default."""
    def install(per_ip=0, global_daily=0):
        limiter = Limiter(backend=InMemoryBackend(), per_ip=per_ip,
                          per_ip_window=3600, global_daily=global_daily)
        monkeypatch.setattr(ratelimit, "_default", limiter)
        return limiter

    yield install
    monkeypatch.setattr(ratelimit, "_default", None)


def test_the_home_page_is_not_metered(app_client, metered):
    metered(per_ip=1)
    assert all(app_client.get("/").status_code == 200 for _ in range(10))


def test_over_the_per_ip_limit_returns_429(search, metered):
    metered(per_ip=2)
    headers = {"X-Real-IP": "198.51.100.1"}
    codes = [search(headers=headers).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_the_429_carries_retry_after_and_an_explanation(search, metered):
    metered(per_ip=1)
    headers = {"X-Real-IP": "198.51.100.2"}
    search(headers=headers)
    response = search(headers=headers)
    assert response.status_code == 429
    assert response.headers["Retry-After"].isdigit()
    body = text(response)
    assert "Too many searches" in body
    assert "run a few searches already" in body
    assert "daily search limit" not in body


def test_the_global_cap_message_differs_from_the_per_ip_one(search, metered):
    metered(per_ip=0, global_daily=1)
    search(headers={"X-Real-IP": "198.51.100.3"})
    body = text(search(headers={"X-Real-IP": "198.51.100.4"}))
    assert "daily search limit" in body
    assert "keeps the AI bill" in body


def test_a_different_ip_is_unaffected_by_another_ones_limit(search, metered):
    metered(per_ip=1)
    assert search(headers={"X-Real-IP": "198.51.100.5"}).status_code == 200
    assert search(headers={"X-Real-IP": "198.51.100.5"}).status_code == 429
    assert search(headers={"X-Real-IP": "198.51.100.6"}).status_code == 200


def test_rejected_input_does_not_consume_quota(app_client, search, metered):
    """Metering sits after the cheap checks, so a typo costs the visitor nothing."""
    metered(per_ip=1)
    headers = {"X-Real-IP": "198.51.100.7"}
    for _ in range(5):
        app_client.post("/process_url", data=dict(art="bad<<input>>",
                        url="http://example.test/a.png"), headers=headers)
    assert search(headers=headers).status_code == 200


def test_nothing_is_spent_past_the_limit(app_client, webapp, monkeypatch, metered, room_image):
    """The limiter runs before the URL fetch and before the two Gemini calls."""
    metered(per_ip=1)
    fetches, pipelines = [], []
    monkeypatch.setattr(webapp.decora, "fetch_user_image",
                        lambda url: (fetches.append(url), room_image)[1])
    monkeypatch.setattr(webapp, "gen_everything",
                        lambda img, art: (pipelines.append(art), RESULT_ROWS)[1])
    for _ in range(4):
        app_client.post("/process_url", data=dict(art="sofa", url="http://example.test/a.png"),
                        headers={"X-Real-IP": "198.51.100.8"})
    assert len(fetches) == 1
    assert len(pipelines) == 1
