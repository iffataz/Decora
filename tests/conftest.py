"""Shared fixtures.

The environment is set at import time, before any test module imports `app`:
app.py raises at import when SECRET_KEY is missing, and the rate limits are
switched off so route tests aren't metered. Rate limit tests build their own
Limiter instead of relying on these.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("RATE_LIMIT_PER_IP", "0")
os.environ.setdefault("RATE_LIMIT_GLOBAL_PER_DAY", "0")

import io  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import resultstore  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_result_store(monkeypatch):
    """Give every test its own result store.

    Autouse on purpose: the store indexes successful searches by image and
    furniture type, so without this a second test posting the same photo would
    be served the first test's stored result and never run the pipeline.
    """
    store = resultstore.ResultStore(backend=resultstore.InMemoryBackend())
    monkeypatch.setattr(resultstore, "_default", store)
    return store


@pytest.fixture
def room_image():
    return Image.new("RGB", (120, 90), "beige")


@pytest.fixture
def product_image():
    return Image.new("RGB", (80, 80), "white")


@pytest.fixture
def png_bytes():
    """A small, valid PNG as raw bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    return buf.getvalue()


def ikea_product(item_id, name="KIVIK", price=899, rating=4.5, rating_count=12, **overrides):
    """One raw IKEA product dict, shaped like the live API's `items[].product`."""
    product = {
        "id": str(item_id),
        "name": name,
        "typeName": "Sofa",
        "mainImageUrl": f"https://ikea.test/{item_id}.jpg",
        "pipUrl": f"https://ikea.test/p/{item_id}",
        "salesPrice": {"numeral": price},
        "ratingValue": rating,
        "ratingCount": rating_count,
    }
    product.update(overrides)
    return product


def ikea_payload(*products):
    """Wrap raw products in the nesting `search_load` has to walk."""
    return {
        "searchResultPage": {
            "products": {"main": {"items": [{"product": p} for p in products]}}
        }
    }


@pytest.fixture
def app_client():
    """Flask test client with rate limiting off (see module docstring)."""
    import app as webapp

    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


@pytest.fixture
def webapp():
    import app as webapp

    return webapp
