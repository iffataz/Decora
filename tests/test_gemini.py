"""The Gemini layer: retry/fallback behaviour and response handling.

No network: a fake client records the calls and returns canned responses.
`time.sleep` is patched out so the retry backoff doesn't slow the suite.
"""
import json

import httpx
import pytest
from google.genai import errors as genai_errors

import decora


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(decora.time, "sleep", slept.append)
    return slept


def api_error(code):
    """A genai APIError with a given HTTP code, without its network constructor."""
    err = genai_errors.APIError.__new__(genai_errors.APIError)
    err.code = code
    err.message = f"HTTP {code}"
    err.status = "ERROR"
    return err


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes):
        self.models = FakeModels(outcomes)

    @property
    def calls(self):
        return self.models.calls

    @property
    def models_used(self):
        return [c["model"] for c in self.models.calls]


class FakeResponse:
    """Mimics a genai response: `parsed` when the SDK could parse it, else `text`."""

    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text


# ── _generate_with_retries ──────────────────────────────────────────────────

def test_a_successful_call_is_not_retried():
    client = FakeClient("first-response")
    assert decora._generate_with_retries(client, model=decora.MODEL) == "first-response"
    assert client.models_used == [decora.MODEL]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_errors_are_retried_once_on_the_same_model(code, no_sleep):
    client = FakeClient(api_error(code), "second-response")
    assert decora._generate_with_retries(client, model=decora.MODEL) == "second-response"
    assert client.models_used == [decora.MODEL, decora.MODEL]
    assert no_sleep == [2], "should back off once before retrying"


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_non_transient_errors_raise_immediately(code, no_sleep):
    """A bad key or malformed request won't fix itself; retrying wastes the budget."""
    client = FakeClient(api_error(code))
    with pytest.raises(genai_errors.APIError):
        decora._generate_with_retries(client, model=decora.MODEL)
    assert client.models_used == [decora.MODEL]
    assert no_sleep == []


def test_two_transient_failures_fall_back_to_the_lite_model():
    client = FakeClient(api_error(503), api_error(503), "lite-response")
    assert decora._generate_with_retries(client, model=decora.MODEL) == "lite-response"
    assert client.models_used == [decora.MODEL, decora.MODEL, decora.FALLBACK_MODEL]


def test_timeouts_are_treated_as_transient():
    client = FakeClient(httpx.TimeoutException("too slow"), "second-response")
    assert decora._generate_with_retries(client, model=decora.MODEL) == "second-response"
    assert client.models_used == [decora.MODEL, decora.MODEL]


def test_timeout_then_timeout_falls_back():
    client = FakeClient(
        httpx.TimeoutException("too slow"),
        httpx.TimeoutException("still too slow"),
        "lite-response",
    )
    assert decora._generate_with_retries(client, model=decora.MODEL) == "lite-response"
    assert client.models_used[-1] == decora.FALLBACK_MODEL


def test_a_failure_on_the_fallback_model_propagates():
    client = FakeClient(api_error(503), api_error(503), api_error(503))
    with pytest.raises(genai_errors.APIError):
        decora._generate_with_retries(client, model=decora.MODEL)
    assert len(client.calls) == 3


def test_the_worst_case_is_three_calls():
    """Pins the retry budget: two on the main model, one on the fallback.

    Each can take up to GEMINI_TIMEOUT_MS, so the worst case for one Gemini
    call is ~92s. A search makes two such calls, which is well past the 60s
    Vercel maxDuration -- a retry storm is cut off by the platform.
    """
    client = FakeClient(api_error(503), api_error(503), "lite-response")
    decora._generate_with_retries(client, model=decora.MODEL)
    assert len(client.calls) == 3
    worst_case_s = 3 * (decora.GEMINI_TIMEOUT_MS / 1000) + 2
    assert worst_case_s > 60, "if this drops under 60s, the comment above is stale"


def test_config_is_passed_through_unchanged_to_the_fallback():
    client = FakeClient(api_error(503), api_error(503), "lite-response")
    decora._generate_with_retries(client, model=decora.MODEL, contents=["x"], config="CFG")
    fallback_call = client.calls[-1]
    assert fallback_call["contents"] == ["x"]
    assert fallback_call["config"] == "CFG"
    assert fallback_call["model"] == decora.FALLBACK_MODEL


# ── analyse_room ────────────────────────────────────────────────────────────

ROOM = {
    "style": "Scandinavian minimalist",
    "colors": ["warm oak", "white", "dusty blue"],
    "mood": "bright, airy",
    "search_keywords": "white oak, blue fabric",
}


@pytest.fixture
def client_of(monkeypatch):
    """Install a FakeClient as the module's Gemini client."""
    def install(*outcomes):
        client = FakeClient(*outcomes)
        monkeypatch.setattr(decora, "_get_client", lambda: client)
        return client

    return install


def test_analyse_room_returns_the_parsed_fields(client_of, room_image):
    client_of(FakeResponse(parsed=decora.RoomAnalysis(**ROOM)))
    assert decora.analyse_room(room_image) == ROOM


def test_analyse_room_falls_back_to_parsing_raw_text(client_of, room_image):
    """`parsed` is None when the SDK couldn't deserialise; the JSON is still there."""
    client_of(FakeResponse(parsed=None, text=json.dumps(ROOM)))
    assert decora.analyse_room(room_image) == ROOM


@pytest.mark.parametrize("missing", ["style", "colors", "mood", "search_keywords"])
def test_analyse_room_rejects_an_empty_field(client_of, room_image, missing):
    """An empty field would poison the IKEA query or the scoring prompt."""
    incomplete = dict(ROOM, **{missing: [] if missing == "colors" else ""})
    client_of(FakeResponse(parsed=decora.RoomAnalysis(**incomplete)))
    with pytest.raises(ValueError, match=missing):
        decora.analyse_room(room_image)


def test_analyse_room_sends_one_image_and_one_prompt(client_of, room_image):
    client = client_of(FakeResponse(parsed=decora.RoomAnalysis(**ROOM)))
    decora.analyse_room(room_image)
    contents = client.calls[0]["contents"]
    assert len(contents) == 2
    assert max(contents[0].size) <= 768, "image should be downscaled before sending"
    assert "search_keywords" in contents[1]


# ── score_products ──────────────────────────────────────────────────────────

def scores(*triples):
    return FakeResponse(parsed=[
        decora.ProductScore(product=p, score=s, reason=r) for p, s, r in triples
    ])


def test_score_products_maps_one_based_numbers_to_zero_based_indices(client_of, room_image, product_image):
    client_of(scores((1, 9, "Great."), (2, 4, "Meh."), (3, 1, "Not a sofa.")))
    out = decora.score_products(room_image, ROOM, [product_image] * 3, "sofa")
    assert out == [
        {"index": 0, "score": 9, "reason": "Great."},
        {"index": 1, "score": 4, "reason": "Meh."},
        {"index": 2, "score": 1, "reason": "Not a sofa."},
    ]


@pytest.mark.parametrize("raw,expected", [(0, 1), (-5, 1), (11, 10), (999, 10), (1, 1), (10, 10)])
def test_scores_are_clamped_to_the_rubric_range(client_of, room_image, product_image, raw, expected):
    client_of(scores((1, raw, "Reason.")))
    out = decora.score_products(room_image, ROOM, [product_image], "sofa")
    assert out[0]["score"] == expected


@pytest.mark.parametrize("product_number", [0, -1, 4, 99])
def test_out_of_range_product_numbers_are_dropped(client_of, room_image, product_image, product_number):
    """A hallucinated index would otherwise attach a score to the wrong product."""
    client_of(scores((product_number, 8, "Phantom."), (1, 7, "Real.")))
    out = decora.score_products(room_image, ROOM, [product_image] * 3, "sofa")
    assert out == [{"index": 0, "score": 7, "reason": "Real."}]


def test_duplicate_product_numbers_keep_only_the_first(client_of, room_image, product_image):
    client_of(scores((1, 9, "First."), (1, 2, "Second.")))
    out = decora.score_products(room_image, ROOM, [product_image] * 2, "sofa")
    assert out == [{"index": 0, "score": 9, "reason": "First."}]


def test_score_products_falls_back_to_parsing_raw_text(client_of, room_image, product_image):
    client_of(FakeResponse(parsed=None, text=json.dumps(
        [{"product": 1, "score": 8, "reason": "From text."}]
    )))
    out = decora.score_products(room_image, ROOM, [product_image], "sofa")
    assert out == [{"index": 0, "score": 8, "reason": "From text."}]


def test_score_products_sends_the_room_then_every_product(client_of, room_image, product_image):
    client = client_of(scores((1, 5, "r")))
    decora.score_products(room_image, ROOM, [product_image] * 3, "floor lamp")
    contents = client.calls[0]["contents"]
    # "Room photo:", room, ("Product N:", img) x3, prompt
    assert contents[0] == "Room photo:"
    assert [c for c in contents if isinstance(c, str) and c.startswith("Product ")] == [
        "Product 1:", "Product 2:", "Product 3:"
    ]
    prompt = contents[-1]
    assert "3 candidate floor lamp products" in prompt
    assert "warm oak, white, dusty blue" in prompt, "room colours belong in the prompt"
    assert "Scandinavian minimalist" in prompt


def test_score_products_tolerates_a_room_dict_missing_keys(client_of, room_image, product_image):
    """score_products uses .get with defaults, so a partial room must not crash."""
    client_of(scores((1, 5, "r")))
    out = decora.score_products(room_image, {}, [product_image], "sofa")
    assert out == [{"index": 0, "score": 5, "reason": "r"}]


def test_an_empty_score_list_is_returned_as_empty(client_of, room_image, product_image):
    client_of(FakeResponse(parsed=[]))
    assert decora.score_products(room_image, ROOM, [product_image], "sofa") == []


def test_api_failures_raise_rather_than_scoring_zero(client_of, room_image, product_image):
    """Silent zero-scores would look like 'no good matches' instead of an outage."""
    client_of(api_error(400))
    with pytest.raises(genai_errors.APIError):
        decora.score_products(room_image, ROOM, [product_image], "sofa")
