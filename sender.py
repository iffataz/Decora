from concurrent.futures import ThreadPoolExecutor

import decora
from apiloader import search_load


def gen_everything(url_specific, art):
    room_img = decora._fetch_image(url_specific)
    room = decora.analyse_room(room_img)

    query = f"{room['search_keywords']} {art}"
    search_df = search_load(query)

    candidates = []
    for i in range(len(search_df)):
        row = search_df.iloc[i]
        image_url = row['mainImageUrl']
        if not isinstance(image_url, str) or not image_url:
            continue
        candidates.append(row)
        if len(candidates) == 10:
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

    qualified = [p for p in scored if p['score'] >= 6]
    if not qualified:
        qualified = sorted(scored, key=lambda x: x['score'], reverse=True)
    qualified.sort(key=lambda x: x['score'], reverse=True)
    return qualified[:4]
