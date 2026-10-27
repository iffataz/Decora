"""Recommendation pipeline: room analysis → IKEA search → batched visual scoring → top 4."""
import logging
import math
from concurrent.futures import ThreadPoolExecutor

import decora
from apiloader import search_load

logger = logging.getLogger(__name__)


def _clean_float(value):
    """Normalise a pandas number for the templates.

    pandas uses NaN for a missing number, which Jinja cannot test for
    (`x is none` is False, and NaN is truthy), so it would render as "nan".
    Mapping it to None lets templates use a plain `{% if %}`.
    """
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def _clean_int(value):
    cleaned = _clean_float(value)
    return None if cleaned is None else int(cleaned)


def gen_everything(room_img, art):
    """Recommend up to 4 IKEA products of type `art` for the room in `room_img` (PIL image).

    Makes exactly 2 Gemini calls (analyse + one batched score). Returns a list of dicts
    with name, url, image, score, reason, price, rating, rating_count — best first.
    """
    room = decora.analyse_room(room_img)

    # Two searches: keyword-tailored results first (capped so they can't crowd out
    # the on-category plain search), then plain furniture-type results to fill to 10.
    keyword_df = search_load(f"{art} {room['search_keywords']}")
    plain_df = search_load(art)

    # cap is a running total across both DataFrames, not a per-DataFrame limit:
    # keyword results fill candidates up to 6, then plain results top it up to 10.
    candidates = []
    seen_ids = set()
    for df, cap in ((keyword_df, 6), (plain_df, 10)):
        for i in range(len(df)):
            row = df.iloc[i]
            image_url = row['mainImageUrl']
            if not isinstance(image_url, str) or not image_url or row['id'] in seen_ids:
                continue
            seen_ids.add(row['id'])
            candidates.append(row)
            if len(candidates) >= cap:
                break

    # Fetch all candidate images concurrently (I/O-bound), not sequentially.
    with ThreadPoolExecutor(max_workers=8) as executor:
        fetches = [executor.submit(decora._fetch_image, row['mainImageUrl']) for row in candidates]

    # rows and product_images are kept as parallel lists: a fetch failure skips
    # appending to both together, so rows[i] always describes product_images[i].
    rows = []
    product_images = []
    for row, future in zip(candidates, fetches, strict=True):
        try:
            product_images.append(future.result())
            rows.append(row)
        except Exception as e:
            # Logged rather than swallowed: a CDN or decode failure silently
            # shrinks the candidate set, which otherwise looks like bad ranking.
            logger.warning("skipping product %s, image fetch failed: %s", row['id'], e)
            continue

    if not rows:
        return []

    results = decora.score_products(room_img, room, product_images, art)

    # result["index"] refers to a position in product_images, so rows[index]
    # is what reunites a Gemini score with the product metadata it belongs to.
    scored = []
    for result in results:
        row = rows[result["index"]]
        scored.append({
            "name": row['name'],
            "url": row['pipUrl'],
            "image": row['mainImageUrl'],
            "score": result["score"],
            "reason": result["reason"],
            "price": row['salesPrice'],
            "rating": _clean_float(row['ratingValue']),
            "rating_count": _clean_int(row['ratingCount']),
        })

    # Prefer good fits (>=6); if none scored that well, fall back to mediocre-but-
    # plausible ones (>=3). A score of 1-2 means wrong category, so never show those.
    qualified = [p for p in scored if p['score'] >= 6]
    if not qualified:
        qualified = [p for p in scored if p['score'] >= 3]
    qualified.sort(key=lambda x: x['score'], reverse=True)
    return qualified[:4]
