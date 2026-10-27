"""The recommendation pipeline: candidate selection, index remapping, ranking."""
import pandas as pd
import pytest

import apiloader
import sender
from tests.conftest import ikea_product


def frame(*products):
    """A DataFrame shaped like search_load's output."""
    if not products:
        return pd.DataFrame(columns=apiloader.COLUMNS)
    rows = []
    for p in products:
        row = dict(p)
        price = p["salesPrice"]
        row["salesPrice"] = price.get("numeral") if isinstance(price, dict) else price
        row.setdefault("contextualImageUrl", None)
        rows.append(row)
    return pd.DataFrame(rows)[apiloader.COLUMNS]


ROOM = {
    "style": "Scandinavian minimalist",
    "colors": ["warm oak", "white"],
    "mood": "bright",
    "search_keywords": "white oak",
}


@pytest.fixture
def pipeline(monkeypatch, room_image, product_image):
    """Run gen_everything with the room analysis, IKEA search, image fetch and
    scoring all faked. Returns a runner plus a record of what was called."""
    record = {"queries": [], "fetched": []}

    def run(keyword_df, plain_df=None, score=None, fetch_fails=(), room=None):
        frames = {"keyword": keyword_df, "plain": plain_df if plain_df is not None else frame()}

        def fake_search_load(query):
            record["queries"].append(query)
            return frames["keyword"] if len(record["queries"]) == 1 else frames["plain"]

        def fake_fetch(url):
            record["fetched"].append(url)
            if url in fetch_fails:
                raise OSError(f"fetch failed for {url}")
            return product_image

        def fake_score(room_img, room_dict, product_images, art):
            record["scored_count"] = len(product_images)
            if score is None:
                return [
                    {"index": i, "score": 10 - i, "reason": f"Reason {i}."}
                    for i in range(len(product_images))
                ]
            return score(product_images)

        monkeypatch.setattr(sender, "search_load", fake_search_load)
        monkeypatch.setattr(sender.decora, "analyse_room", lambda img: room or ROOM)
        monkeypatch.setattr(sender.decora, "_fetch_image", fake_fetch)
        monkeypatch.setattr(sender.decora, "score_products", fake_score)
        return sender.gen_everything(room_image, "sofa")

    run.record = record
    return run


# ── Search queries ──────────────────────────────────────────────────────────

def test_the_two_searches_combine_keywords_then_fall_back_to_plain(pipeline):
    pipeline(frame(ikea_product(1)))
    assert pipeline.record["queries"] == ["sofa white oak", "sofa"]


# ── Candidate selection ─────────────────────────────────────────────────────

def test_keyword_results_are_capped_at_six_then_topped_up_to_ten(pipeline):
    """The cap is a running total across both frames, not a per-frame limit."""
    keyword = frame(*[ikea_product(i) for i in range(1, 21)])
    plain = frame(*[ikea_product(i) for i in range(100, 120)])
    out = pipeline(keyword, plain)
    assert pipeline.record["scored_count"] == 10
    # The fetches run in a thread pool, so compare the set, not the order.
    ids = {url.split("/")[-1].removesuffix(".jpg") for url in pipeline.record["fetched"]}
    assert ids == {"1", "2", "3", "4", "5", "6", "100", "101", "102", "103"}, (
        "six from the keyword search, then topped up from the plain one"
    )
    assert len(out) == 4


def test_an_empty_plain_search_leaves_the_keyword_cap_in_place(pipeline):
    """Documenting a quirk: the 6-cap on keyword results is not topped back up
    from the keyword frame, so an empty plain search yields 6 candidates, not 10."""
    pipeline(frame(*[ikea_product(i) for i in range(1, 21)]), frame())
    assert pipeline.record["scored_count"] == 6


def test_a_thin_keyword_search_is_topped_up_to_ten(pipeline):
    pipeline(frame(ikea_product(1), ikea_product(2)),
             frame(*[ikea_product(i) for i in range(100, 120)]))
    assert pipeline.record["scored_count"] == 10


def test_duplicates_across_the_two_searches_are_deduped(pipeline):
    pipeline(frame(ikea_product(1), ikea_product(2)),
             frame(ikea_product(2), ikea_product(3)))
    assert pipeline.record["scored_count"] == 3


def test_fewer_candidates_than_the_cap_is_fine(pipeline):
    assert len(pipeline(frame(ikea_product(1), ikea_product(2)))) == 2


@pytest.mark.parametrize("bad_url", ["", None, float("nan"), 123])
def test_candidates_without_a_usable_image_url_are_skipped(pipeline, bad_url):
    out = pipeline(frame(ikea_product(1, mainImageUrl=bad_url), ikea_product(2)))
    assert [r["name"] for r in out] == ["KIVIK"]
    assert pipeline.record["scored_count"] == 1


def test_no_candidates_at_all_returns_empty_without_scoring(pipeline):
    assert pipeline(frame(), frame()) == []
    assert "scored_count" not in pipeline.record, "should not call Gemini with nothing to score"


# ── Index remapping: the bug that mislabels plausibly ───────────────────────

def test_scores_stay_attached_to_the_right_products(pipeline):
    """rows and product_images are parallel lists; if they drift, every product
    gets another's score and reason, which looks entirely plausible on screen."""
    products = [ikea_product(i, name=f"SOFA-{i}", price=100 * i) for i in range(1, 5)]
    out = pipeline(
        frame(*products),
        score=lambda imgs: [
            {"index": 3, "score": 10, "reason": "best"},
            {"index": 0, "score": 9, "reason": "second"},
            {"index": 2, "score": 8, "reason": "third"},
            {"index": 1, "score": 7, "reason": "fourth"},
        ],
    )
    assert [(r["name"], r["score"], r["reason"]) for r in out] == [
        ("SOFA-4", 10, "best"),
        ("SOFA-1", 9, "second"),
        ("SOFA-3", 8, "third"),
        ("SOFA-2", 7, "fourth"),
    ]
    assert [r["price"] for r in out] == [400, 100, 300, 200]


def test_a_failed_image_fetch_keeps_the_remaining_rows_aligned(pipeline):
    """The dropped product must not shift every later score by one."""
    products = [ikea_product(i, name=f"SOFA-{i}") for i in range(1, 5)]
    out = pipeline(
        frame(*products),
        fetch_fails={"https://ikea.test/2.jpg"},
        score=lambda imgs: [
            {"index": i, "score": 10 - i, "reason": f"r{i}"} for i in range(len(imgs))
        ],
    )
    assert pipeline.record["scored_count"] == 3, "the unfetchable product is not scored"
    assert [r["name"] for r in out] == ["SOFA-1", "SOFA-3", "SOFA-4"]
    assert [r["score"] for r in out] == [10, 9, 8]


def test_every_image_failing_returns_empty_without_scoring(pipeline):
    out = pipeline(
        frame(ikea_product(1), ikea_product(2)),
        fetch_fails={"https://ikea.test/1.jpg", "https://ikea.test/2.jpg"},
    )
    assert out == []
    assert "scored_count" not in pipeline.record


def test_a_failed_fetch_is_logged(pipeline, caplog):
    """A silent drop shrinks the candidate set and looks like bad ranking."""
    with caplog.at_level("WARNING"):
        pipeline(frame(ikea_product(7)), fetch_fails={"https://ikea.test/7.jpg"})
    assert "skipping product 7" in caplog.text


# ── Ranking and filtering ───────────────────────────────────────────────────

def test_results_are_sorted_best_first_and_capped_at_four(pipeline):
    # Split across both searches: the keyword frame alone is capped at 6.
    out = pipeline(
        frame(*[ikea_product(i, name=f"S{i}") for i in range(1, 5)]),
        frame(*[ikea_product(i, name=f"S{i}") for i in range(5, 9)]),
        score=lambda imgs: [
            {"index": i, "score": s, "reason": "r"}
            for i, s in enumerate([6, 10, 7, 9, 8, 6, 10, 7])
        ],
    )
    assert [r["score"] for r in out] == [10, 10, 9, 8]
    assert len(out) == 4


def test_scores_below_six_are_hidden_when_better_ones_exist(pipeline):
    out = pipeline(
        frame(ikea_product(1), ikea_product(2)),
        score=lambda imgs: [
            {"index": 0, "score": 6, "reason": "ok"},
            {"index": 1, "score": 5, "reason": "poor"},
        ],
    )
    assert [r["score"] for r in out] == [6]


def test_mediocre_matches_are_shown_when_nothing_clears_six(pipeline):
    out = pipeline(
        frame(ikea_product(1), ikea_product(2)),
        score=lambda imgs: [
            {"index": 0, "score": 5, "reason": "meh"},
            {"index": 1, "score": 3, "reason": "worse"},
        ],
    )
    assert [r["score"] for r in out] == [5, 3]


@pytest.mark.parametrize("score", [1, 2])
def test_wrong_category_scores_are_never_shown(pipeline, score):
    """1-2 means "not the furniture type asked for", so it must not surface
    even when it is the only thing left."""
    out = pipeline(
        frame(ikea_product(1)),
        score=lambda imgs: [{"index": 0, "score": score, "reason": "Not a sofa."}],
    )
    assert out == []


def test_a_mix_of_wrong_category_and_mediocre_keeps_only_the_mediocre(pipeline):
    out = pipeline(
        frame(ikea_product(1), ikea_product(2)),
        score=lambda imgs: [
            {"index": 0, "score": 1, "reason": "Not a sofa."},
            {"index": 1, "score": 4, "reason": "Passable."},
        ],
    )
    assert [r["reason"] for r in out] == ["Passable."]


def test_an_empty_score_list_yields_no_results(pipeline):
    assert pipeline(frame(ikea_product(1)), score=lambda imgs: []) == []


# ── The shape handed to the template ────────────────────────────────────────

def test_each_result_carries_the_fields_the_template_reads(pipeline):
    out = pipeline(frame(ikea_product(1)))
    assert set(out[0]) == {
        "name", "url", "image", "score", "reason", "price", "rating", "rating_count",
    }


def test_nan_ratings_become_none_so_jinja_can_test_them(pipeline):
    """pandas uses NaN for a missing number; Jinja cannot test for NaN, so it
    would render as the string "nan" on the page."""
    out = pipeline(frame(
        ikea_product(1, rating=float("nan"), rating_count=float("nan")),
    ))
    assert out[0]["rating"] is None
    assert out[0]["rating_count"] is None


def test_present_ratings_arrive_as_clean_types(pipeline):
    out = pipeline(frame(ikea_product(1, rating=4.5, rating_count=12)))
    assert out[0]["rating"] == 4.5
    assert isinstance(out[0]["rating_count"], int) and out[0]["rating_count"] == 12


@pytest.mark.parametrize("value,expected", [
    (None, None), (float("nan"), None), (4.5, 4.5), (0, 0.0), (3, 3.0),
])
def test_clean_float(value, expected):
    assert sender._clean_float(value) == expected or (
        expected is None and sender._clean_float(value) is None
    )


@pytest.mark.parametrize("value,expected", [
    (None, None), (float("nan"), None), (12.0, 12), (12.7, 12),
])
def test_clean_int(value, expected):
    assert sender._clean_int(value) == expected
