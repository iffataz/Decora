"""IKEA catalogue search (Australia store) returning a validated pandas DataFrame."""
import logging
import ikea_api
import pandas as pd
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)

COLUMNS = [
    'id', 'name', 'typeName', 'mainImageUrl', 'contextualImageUrl',
    'pipUrl', 'ratingValue', 'ratingCount', 'salesPrice',
]


class ItemModel(BaseModel):
    id: str
    name: str
    typeName: str
    mainImageUrl: str
    contextualImageUrl: Optional[str] = None
    pipUrl: str
    ratingValue: Optional[float] = None
    ratingCount: Optional[int] = None
    salesPrice: dict


def _dig(data, *keys, default=None):
    """Walk nested dict keys, returning `default` if any level is missing or not a dict.

    IKEA's response shape is not part of a contract we control, so a changed or
    empty payload has to degrade to "no results" rather than raise KeyError.
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def search_load(query: str):
    """Search IKEA for `query` and return a DataFrame of products and their variants.

    Malformed items are skipped and results are deduped by id; if nothing validates,
    returns an empty DataFrame that still has the expected columns.
    """
    constants = ikea_api.Constants(country="au", language="en")
    search = ikea_api.Search(constants)
    endpoint = search.search(query)
    try:
        json_dump = ikea_api.run(endpoint)
    except Exception as e:
        raise RuntimeError(f"IKEA API unavailable: {e}") from e

    items = _dig(json_dump, 'searchResultPage', 'products', 'main', 'items', default=[])
    if not isinstance(items, list):
        logger.warning("search_load(%r): unexpected 'items' type %s", query, type(items).__name__)
        items = []
    if not items:
        logger.info("search_load(%r): IKEA returned no items", query)
        return pd.DataFrame(columns=COLUMNS)

    validated_items = []
    seen_ids = set()
    considered = 0
    dropped = 0

    def take(candidate) -> None:
        """Validate one raw item/variant and keep it if it is new and well-formed."""
        nonlocal considered, dropped
        considered += 1
        try:
            validated = ItemModel(**candidate)
        except Exception as e:
            dropped += 1
            # Logged rather than swallowed: if IKEA renames a required field,
            # every item fails here and the app would otherwise just report
            # "no matching products found" with no clue why.
            logger.debug("search_load(%r): skipping malformed item: %s", query, e)
            return
        if validated.id in seen_ids:
            return
        seen_ids.add(validated.id)
        validated_items.append(validated)

    for item in items:
        product = item.get('product') if isinstance(item, dict) else None
        if not isinstance(product, dict):
            considered += 1
            dropped += 1
            continue
        take(product)

        for variant in _dig(product, 'gprDescription', 'variants', default=[]):
            if isinstance(variant, dict):
                take(variant)

    if dropped:
        logger.warning(
            "search_load(%r): skipped %d of %d candidate item(s)", query, dropped, considered
        )
    if not validated_items:
        logger.warning("search_load(%r): no items passed validation", query)
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame([item.model_dump() for item in validated_items])
    df['salesPrice'] = df['salesPrice'].apply(lambda price: price.get('numeral'))
    return df
