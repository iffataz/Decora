"""Recommendation pipeline: room analysis → IKEA search → batched visual scoring → top 4.

gen_everything() is what the app calls. It is built from three steps —
find_candidates(), score_candidates() and select_top() — which the eval harness
(eval/run.py) also calls individually so it can record every scored candidate,
not just the final four.
"""
from concurrent.futures import ThreadPoolExecutor

import decora
from apiloader import search_load


def find_candidates(room, art):
    """Search IKEA for `art` and return (rows, product_images): up to 10 candidates
    whose images downloaded, as parallel lists (rows[i] describes product_images[i])."""
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

    # A fetch failure skips appending to both lists together, keeping them parallel.
    rows = []
    product_images = []
    for row, future in zip(candidates, fetches):
        try:
            product_images.append(future.result())
            rows.append(row)
        except Exception:
            continue
    return rows, product_images


def score_candidates(room_img, room, rows, product_images, art):
    """One batched Gemini call; returns a dict per scored product (unsorted, unfiltered)."""
    if not rows:
        return []
    results = decora.score_products(room_img, room, product_images, art)

    # result["index"] refers to a position in product_images, so rows[index]
    # is what reunites a Gemini score with the product metadata it belongs to.
    scored = []
    for result in results:
        row = rows[result["index"]]
        scored.append({
            "id": row['id'],
            "name": row['name'],
            "type_name": row['typeName'],
            "url": row['pipUrl'],
            "image": row['mainImageUrl'],
            "score": result["score"],
            "reason": result["reason"],
            "price": row['salesPrice'],
            "rating": row['ratingValue'],
            "rating_count": row['ratingCount'],
        })
    return scored


def select_top(scored, n=4):
    """Prefer good fits (>=6); if none scored that well, fall back to mediocre-but-
    plausible ones (>=3). A score of 1-2 means wrong category, so never show those."""
    qualified = [p for p in scored if p['score'] >= 6]
    if not qualified:
        qualified = [p for p in scored if p['score'] >= 3]
    qualified = sorted(qualified, key=lambda x: x['score'], reverse=True)
    return qualified[:n]


def gen_everything(room_img, art):
    """Recommend up to 4 IKEA products of type `art` for the room in `room_img` (PIL image).

    Makes exactly 2 Gemini calls (analyse + one batched score). Returns a list of dicts
    with name, url, image, score, reason, price, rating, rating_count — best first.
    """
    room = decora.analyse_room(room_img)
    rows, product_images = find_candidates(room, art)
    scored = score_candidates(room_img, room, rows, product_images, art)
    return select_top(scored)
