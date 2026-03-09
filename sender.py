import decora
from apiloader import search_load


def gen_everything(url_specific, art):
    # Stage 1: single room analysis
    room = decora.analyse_room(url_specific)

    # Stage 2: enriched IKEA search using room keywords + furniture type
    query = f"{room['search_keywords']} {art}"
    search_df = search_load(query)

    # Stage 3: score up to 15 products
    scored = []
    for i in range(min(10, len(search_df))):
        row = search_df.iloc[i]
        image_url = row['contextualImageUrl'] or row['mainImageUrl']
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

    # Stage 4: filter score >= 6, sort descending, return top 4
    qualified = [p for p in scored if p['score'] >= 6]
    qualified.sort(key=lambda x: x['score'], reverse=True)
    return qualified[:4]
