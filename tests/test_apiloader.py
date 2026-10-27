"""IKEA catalogue parsing: resilience to the payload shape, validation, dedup."""
import pytest

import apiloader
from tests.conftest import ikea_payload, ikea_product


@pytest.fixture
def search(monkeypatch):
    """Run search_load against a canned IKEA payload (or exception)."""
    def run(payload, query="sofa"):
        def fake_run(endpoint):
            if isinstance(payload, BaseException):
                raise payload
            return payload

        monkeypatch.setattr(apiloader.ikea_api, "run", fake_run)
        return apiloader.search_load(query)

    return run


# ── Payload shapes that must degrade, not raise ─────────────────────────────

@pytest.mark.parametrize("payload,label", [
    ({}, "empty dict"),
    ({"searchResultPage": {}}, "no products key"),
    ({"searchResultPage": {"products": {}}}, "no main key"),
    ({"searchResultPage": {"products": {"main": {}}}}, "no items key"),
    ({"searchResultPage": {"products": None}}, "null midway"),
    ({"searchResultPage": {"products": {"main": {"items": None}}}}, "null items"),
    ({"searchResultPage": {"products": {"main": {"items": []}}}}, "empty items"),
    ({"searchResultPage": {"products": {"main": {"items": "nope"}}}}, "items is a string"),
    ({"searchResultPage": "unexpected"}, "string where a dict was expected"),
    ({"searchResultPage": {"products": {"main": {"items": ["x", 7, None]}}}}, "items are not dicts"),
    ({"searchResultPage": {"products": {"main": {"items": [{"no_product": {}}]}}}}, "item lacks product"),
    ({"searchResultPage": {"products": {"main": {"items": [{"product": "str"}]}}}}, "product is not a dict"),
])
def test_unexpected_payloads_return_an_empty_frame_with_the_right_columns(search, payload, label):
    """IKEA's shape is not a contract we control, so a change must mean
    "no results", never a KeyError surfacing as a 500."""
    df = search(payload)
    assert len(df) == 0, label
    assert list(df.columns) == apiloader.COLUMNS, label


def test_an_api_failure_is_wrapped_with_context(search):
    with pytest.raises(RuntimeError, match="IKEA API unavailable"):
        search(ConnectionError("connection reset"))


# ── Validation ──────────────────────────────────────────────────────────────

def test_a_well_formed_product_is_returned(search):
    df = search(ikea_payload(ikea_product(1)))
    assert len(df) == 1
    row = df.iloc[0]
    assert row["id"] == "1"
    assert row["name"] == "KIVIK"
    assert row["pipUrl"] == "https://ikea.test/p/1"


def test_sales_price_is_flattened_to_its_numeral(search):
    df = search(ikea_payload(ikea_product(1, price=1299)))
    assert df.iloc[0]["salesPrice"] == 1299


def test_a_missing_price_numeral_becomes_none(search):
    df = search(ikea_payload(ikea_product(1, salesPrice={})))
    assert df.iloc[0]["salesPrice"] is None


@pytest.mark.parametrize("field", ["id", "name", "typeName", "mainImageUrl", "pipUrl"])
def test_an_item_missing_a_required_field_is_skipped(search, field):
    product = ikea_product(1)
    del product[field]
    df = search(ikea_payload(product, ikea_product(2)))
    assert list(df["id"]) == ["2"]


def test_optional_fields_may_be_absent(search):
    product = ikea_product(1)
    del product["ratingValue"]
    del product["ratingCount"]
    df = search(ikea_payload(product))
    assert len(df) == 1
    assert df.iloc[0]["ratingValue"] is None


def test_one_malformed_item_does_not_lose_the_others(search):
    df = search(ikea_payload(
        ikea_product(1), {"id": "broken"}, ikea_product(3), {"nothing": "useful"},
    ))
    assert list(df["id"]) == ["1", "3"]


def test_items_dropped_in_bulk_are_logged_loudly(search, caplog):
    """If IKEA renames a required field, every item fails validation. Without a
    log line the app would just say "no matching products found" forever."""
    with caplog.at_level("WARNING"):
        df = search(ikea_payload({"id": "a"}, {"id": "b"}))
    assert len(df) == 0
    assert "no items passed validation" in caplog.text
    assert "skipped 2 of 2" in caplog.text


def test_the_skipped_count_matches_what_was_considered(search, caplog):
    with caplog.at_level("WARNING"):
        search(ikea_payload(ikea_product(1), {"id": "bad"}, ikea_product(3)))
    assert "skipped 1 of 3" in caplog.text


# ── Variants and dedup ──────────────────────────────────────────────────────

def test_variants_are_included_as_separate_products(search):
    product = ikea_product(1, gprDescription={
        "numberOfVariants": 2,
        "variants": [ikea_product(2, name="KIVIK grey"), ikea_product(3, name="KIVIK blue")],
    })
    df = search(ikea_payload(product))
    assert list(df["id"]) == ["1", "2", "3"]
    assert list(df["name"]) == ["KIVIK", "KIVIK grey", "KIVIK blue"]


def test_a_malformed_variant_is_skipped_but_its_parent_kept(search):
    product = ikea_product(1, gprDescription={"variants": [{"id": "broken"}]})
    df = search(ikea_payload(product))
    assert list(df["id"]) == ["1"]


@pytest.mark.parametrize("gpr", [
    {"variants": []},
    {"variants": None},
    {},
    None,
    "unexpected",
])
def test_odd_variant_containers_are_tolerated(search, gpr):
    df = search(ikea_payload(ikea_product(1, gprDescription=gpr)))
    assert list(df["id"]) == ["1"]


def test_duplicate_ids_are_deduped_keeping_the_first(search):
    df = search(ikea_payload(
        ikea_product(1, name="First"), ikea_product(1, name="Second"),
    ))
    assert list(df["id"]) == ["1"]
    assert df.iloc[0]["name"] == "First"


def test_a_variant_duplicating_its_parent_is_deduped(search):
    product = ikea_product(1, gprDescription={"variants": [ikea_product(1)]})
    df = search(ikea_payload(product))
    assert list(df["id"]) == ["1"]


# ── _dig ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data,keys,expected", [
    ({"a": {"b": {"c": 1}}}, ("a", "b", "c"), 1),
    ({"a": {"b": {}}}, ("a", "b", "c"), "DEFAULT"),
    ({"a": None}, ("a", "b"), "DEFAULT"),
    ({"a": "str"}, ("a", "b"), "DEFAULT"),
    ({}, ("a",), "DEFAULT"),
    ({"a": 0}, ("a",), 0),
    ({"a": False}, ("a",), False),
    ({"a": []}, ("a",), []),
])
def test_dig_walks_or_defaults(data, keys, expected):
    assert apiloader._dig(data, *keys, default="DEFAULT") == expected
