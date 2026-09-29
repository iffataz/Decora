import json
import sys

import pandas as pd
import pytest
from PIL import Image

import decora
from eval import cache, label, metrics, run


@pytest.mark.parametrize("ft, type_name, expected", [
    ("sofa", "3-seat sofa", True),
    ("couch", "3-seat sofa", True),
    ("sofa", "Sofa-bed", True),
    ("table", "Coffee table", True),
    ("table", "Table lamp", False),
    ("floor lamp", "Floor lamp", True),
    ("floor lamp", "TV bench", False),
    ("rug", "Rug, low pile", True),
    ("dresser", "Chest of 3 drawers", True),
    ("chair", "Armchair", True),
    ("bookshelf", "Bookcase", True),
    ("sofa", None, False),
])
def test_category_match(ft, type_name, expected):
    assert metrics.category_match(ft, type_name) is expected


def _case(scores_by_repeat, tops, candidates=None, **extra):
    candidates = candidates or [
        {"id": "a", "name": "KIVIK", "type_name": "3-seat sofa"},
        {"id": "b", "name": "KIVIK", "type_name": "3-seat sofa"},
        {"id": "c", "name": "LACK", "type_name": "TV bench"},
    ]
    return {
        "id": "c1", "furniture_type": "sofa", "candidates": candidates,
        "repeats": [{"scores": {k: {"score": v, "reason": ""} for k, v in s.items()}, "top": t}
                    for s, t in zip(scores_by_repeat, tops)],
        "timings": {"total_s": 2.0}, "model_fallbacks": 0, **extra,
    }


def test_case_metrics_unlabelled():
    m = metrics.case_metrics(_case(
        [{"a": 9, "b": 8, "c": 1}, {"a": 7, "b": 9, "c": 2}],
        [["a", "b"], ["b", "a"]],
    ))
    assert m["pool_off_category"] == 1
    assert m["top_off_category"] == 0
    assert m["off_category_caught"] == 1.0
    assert m["top_duplicate_names"] == 1
    assert m["top_stability"] == 1.0
    assert m["score_jitter"] == pytest.approx((1.0 + 0.5 + 0.5) / 3)
    assert not m["threshold_fallback"]


def test_case_metrics_labelled_splits_retrieval_and_ranking():
    case = _case([{"a": 9, "b": 8, "c": 1}], [["a", "b"]])
    labels = {"c1": {"a": "bad", "b": "good", "c": "good"}}
    m = metrics.case_metrics(case, labels)
    assert m["precision_at_4"] == 0.5
    assert m["good_at_4"] == 1 and m["bad_at_4"] == 1
    assert m["pool_good"] == 2
    assert m["ranking_recall"] == 0.5


def test_summary_and_compare_format():
    runs = {"cases": [_case([{"a": 9, "b": 8, "c": 1}], [["a", "b"]]), {"id": "x", "error": "boom"}]}
    summary, per_case = metrics.summarise(runs, {"c1": {"a": "good", "b": "ok"}})
    assert summary["errors"] == 1 and summary["cases"] == 2
    assert summary["precision_at_4"] == 0.75
    assert summary["top_label_coverage"] == 1.0
    text = metrics.format_summary(summary, {**summary, "precision_at_4": 0.5})
    assert "+0.25 +" in text


def _seed_cache(cache_dir, queries, df):
    (cache_dir / "search").mkdir(parents=True)
    (cache_dir / "images").mkdir()
    for q in queries:
        payload = {"query": q, "columns": list(df.columns), "rows": json.loads(df.to_json(orient="records"))}
        (cache_dir / "search" / f"{cache._key(q)}.json").write_text(json.dumps(payload))
    for url in df["mainImageUrl"]:
        Image.new("RGB", (8, 8), "white").save(cache_dir / "images" / f"{cache._key(url)}.jpg")


def test_offline_run_end_to_end(tmp_path, monkeypatch):
    """eval.run over one case using only the cache and fake Gemini calls."""
    df = pd.DataFrame([{
        "id": pid, "name": name, "typeName": tn, "mainImageUrl": f"http://img/{pid}",
        "contextualImageUrl": None, "pipUrl": f"http://p/{pid}", "ratingValue": float("nan"),
        "ratingCount": None, "salesPrice": 199.0,
    } for pid, name, tn in [("s1", "KIVIK", "3-seat sofa"), ("s2", "GLOSTAD", "2-seat sofa"),
                            ("t1", "LACK", "TV bench")]])
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ["sofa white oak", "sofa"], df)

    rooms = tmp_path / "rooms"
    rooms.mkdir()
    Image.new("RGB", (64, 48), "beige").save(rooms / "r.jpg")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "r-sofa", "room": str(rooms / "r.jpg"), "furniture_type": "sofa"}]))

    monkeypatch.setattr(decora, "analyse_room", lambda img: {
        "style": "Scandi", "colors": ["white"], "mood": "calm", "search_keywords": "white oak"})
    by_id = {"KIVIK": 9, "GLOSTAD": 6, "LACK": 1}

    def fake_score(room_img, room, images, ft):
        # Score by position, so shuffled repeats see the same per-product scores.
        order = fake_score.orders.pop(0)
        return [{"index": i, "score": by_id[name], "reason": name} for i, name in enumerate(order)]
    fake_score.orders = []

    real_score_candidates = run.sender.score_candidates

    def spy(room_img, room, rows, images, art):
        fake_score.orders.append([r["name"] for r in rows])
        return real_score_candidates(room_img, room, rows, images, art)

    monkeypatch.setattr(decora, "score_products", fake_score)
    monkeypatch.setattr(run.sender, "score_candidates", spy)
    monkeypatch.setattr(run, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(metrics, "load_labels", lambda *a: {})
    monkeypatch.setattr(sys, "argv", ["run", "--cases", str(cases), "--name", "t", "--cache", "offline",
                                      "--cache-dir", str(cache_dir), "--repeats", "2"])
    run.main()

    out = json.loads((tmp_path / "runs" / "t.json").read_text())
    case = out["cases"][0]
    assert "error" not in case, case.get("error")
    assert [c["id"] for c in case["candidates"]] == ["s1", "s2", "t1"]
    assert case["candidates"][0]["rating"] is None
    assert all(r["top"] == ["s1", "s2"] for r in case["repeats"])
    summary, _ = metrics.summarise(out)
    assert summary["top_stability"] == 1.0
    assert summary["top_off_category_per_case"] == 0

    html = label.render(label.build_cases([out]), {"r-sofa": {"s1": "good"}})
    assert '"s1": "good"' in html and "__DATA__" not in html


def test_offline_cache_miss_raises(tmp_path):
    with cache.cached_io(tmp_path, "offline"):
        with pytest.raises(cache.CacheMiss):
            run.sender.search_load("sofa")
    assert run.sender.search_load.__module__ == "apiloader"
