import pandas as pd
from PIL import Image

import decora
import sender


def _df(*ids, image="http://img/{}"):
    return pd.DataFrame([{
        "id": i, "name": f"N{i}", "typeName": "Sofa", "mainImageUrl": image.format(i),
        "contextualImageUrl": None, "pipUrl": f"http://p/{i}", "ratingValue": 4.0,
        "ratingCount": 3, "salesPrice": 100.0,
    } for i in ids])


def test_find_candidates_caps_keyword_results_and_dedupes(monkeypatch):
    keyword = _df(*[f"k{i}" for i in range(8)])
    plain = _df("k0", "k1", *[f"p{i}" for i in range(8)])
    monkeypatch.setattr(sender, "search_load", lambda q: keyword if "oak" in q else plain)
    monkeypatch.setattr(decora, "_fetch_image", lambda url: Image.new("RGB", (4, 4)))

    rows, images = sender.find_candidates({"search_keywords": "oak"}, "sofa")

    ids = [r["id"] for r in rows]
    assert ids == [f"k{i}" for i in range(6)] + ["p0", "p1", "p2", "p3"]
    assert len(images) == len(rows)


def test_find_candidates_drops_failed_image_fetches_in_parallel(monkeypatch):
    monkeypatch.setattr(sender, "search_load", lambda q: _df("a", "b", "c"))

    def fetch(url):
        if url.endswith("/b"):
            raise OSError("boom")
        return Image.new("RGB", (4, 4), "red" if url.endswith("/a") else "blue")

    monkeypatch.setattr(decora, "_fetch_image", fetch)
    rows, images = sender.find_candidates({"search_keywords": "x"}, "sofa")
    assert [r["id"] for r in rows] == ["a", "c"]
    assert images[1].getpixel((0, 0)) == (0, 0, 255)


def test_score_candidates_maps_indices_back_to_rows(monkeypatch):
    df = _df("a", "b")
    rows = [df.iloc[0], df.iloc[1]]
    monkeypatch.setattr(decora, "score_products", lambda *a: [{"index": 1, "score": 8, "reason": "r"}])
    scored = sender.score_candidates(None, {}, rows, [None, None], "sofa")
    assert scored == [{
        "id": "b", "name": "Nb", "type_name": "Sofa", "url": "http://p/b", "image": "http://img/b",
        "score": 8, "reason": "r", "price": 100.0, "rating": 4.0, "rating_count": 3,
    }]


def test_score_candidates_empty_makes_no_call(monkeypatch):
    def fail(*a):
        raise AssertionError("should not call Gemini")
    monkeypatch.setattr(decora, "score_products", fail)
    assert sender.score_candidates(None, {}, [], [], "sofa") == []


def test_select_top_thresholds():
    s = lambda *scores: [{"id": str(i), "score": v} for i, v in enumerate(scores)]
    assert [p["score"] for p in sender.select_top(s(7, 9, 6, 5, 8, 10))] == [10, 9, 8, 7]
    assert [p["score"] for p in sender.select_top(s(3, 5, 2, 1))] == [5, 3]
    assert sender.select_top(s(1, 2)) == []
