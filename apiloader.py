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

    columns = [
        'id', 'name', 'typeName', 'mainImageUrl', 'contextualImageUrl',
        'pipUrl', 'ratingValue', 'ratingCount', 'salesPrice',
    ]

    validated_items = []
    seen_ids = set()
    for item in pure_items:
        try:
            validated = ItemModel(**item)
        except Exception:
            pass
        else:
            if validated.id not in seen_ids:
                seen_ids.add(validated.id)
                validated_items.append(validated)

        if item.get('gprDescription', {}).get('numberOfVariants', 0) > 0:
            variants = item.get('gprDescription', {}).get('variants', [])
            for variant in variants:
                try:
                    validated_variant = ItemModel(**variant)
                except Exception:
                    continue
                if validated_variant.id in seen_ids:
                    continue
                seen_ids.add(validated_variant.id)
                validated_items.append(validated_variant)

    if not validated_items:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame([item.model_dump() for item in validated_items])

    def extract_numeral(sales_price):
        return sales_price.get('numeral')

    df['salesPrice'] = df['salesPrice'].apply(extract_numeral)
    return df
