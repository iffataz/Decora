import decora
from apiloader import search_load


def gen_everything(url_specific, art):
    room = decora.analyse_room(url_specific)

    query = f"{room['search_keywords']} {art}"
    search_df = search_load(query)

    scored = []
    for i in range(min(10, len(search_df))):
        row = search_df.iloc[i]
        ctx = row['contextualImageUrl']
        image_url = ctx if isinstance(ctx, str) else row['mainImageUrl']
        if not image_url:
            continue
        score = decora.score_product(image_url, art, room)
        scored.append({
            "name": row['name'],
            "url": row['pipUrl'],
            "image": row['mainImageUrl'],
            "score": score,
            "price": row['salesPrice'],
            "rating": row['ratingValue'],
            "rating_count": row['ratingCount'],
        })

    qualified = [p for p in scored if p['score'] >= 6]
    if not qualified:
        qualified = sorted(scored, key=lambda x: x['score'], reverse=True)
    qualified.sort(key=lambda x: x['score'], reverse=True)
    return qualified[:4]
