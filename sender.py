"""Recommendation pipeline: room analysis → IKEA search → batched visual scoring → top 4."""
from concurrent.futures import ThreadPoolExecutor

import decora
from apiloader import search_load


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

    with ThreadPoolExecutor(max_workers=8) as executor:
        fetches = [executor.submit(decora._fetch_image, row['mainImageUrl']) for row in candidates]

    rows = []
    product_images = []
    for row, future in zip(candidates, fetches):
        try:
            product_images.append(future.result())
            rows.append(row)
        except Exception:
            continue

    if not rows:
        return []

    results = decora.score_products(room_img, room, product_images, art)

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
            "rating": row['ratingValue'],
            "rating_count": row['ratingCount'],
        })

    # Fallback keeps mediocre-but-plausible fits; score 1-2 means wrong category, never show those.
    qualified = [p for p in scored if p['score'] >= 6]
    if not qualified:
        qualified = [p for p in scored if p['score'] >= 3]
    qualified.sort(key=lambda x: x['score'], reverse=True)
    return qualified[:4]
