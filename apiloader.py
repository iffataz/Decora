import ikea_api
import pandas as pd
from pydantic import BaseModel
from typing import Optional


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


def search_load(query: str):
    constants = ikea_api.Constants(country="au", language="en")
    search = ikea_api.Search(constants)
    endpoint = search.search(query)
    try:
        json_dump = ikea_api.run(endpoint)
    except Exception as e:
        raise RuntimeError(f"IKEA API unavailable: {e}") from e

    items = json_dump['searchResultPage']['products']['main']['items']

    pure_items = []
    for item in items:
        pure_items.append(item['product'])

    validated_items = []
    for item in pure_items:
        validated_items.append(ItemModel(**item))
        if item.get('gprDescription', {}).get('numberOfVariants', 0) > 0:
            variants = item.get('gprDescription', {}).get('variants', [])
            for variant in variants:
                validated_items.append(ItemModel(**variant))

    df = pd.DataFrame([item.model_dump() for item in validated_items])

    def extract_numeral(sales_price):
        return sales_price.get('numeral')

    df['salesPrice'] = df['salesPrice'].apply(extract_numeral)
    return df
