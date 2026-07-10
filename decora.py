import os
import requests
from io import BytesIO
from PIL import Image
from pydantic import BaseModel
from google import genai
from google.genai import types

MODEL = "gemini-3.1-flash-lite-preview"
_client: genai.Client | None = None


class RoomAnalysis(BaseModel):
    style: str
    colors: list[str]
    mood: str
    search_keywords: str


class ProductScore(BaseModel):
    product: int
    score: int
    reason: str


def _fetch_image(url: str) -> Image.Image:
    resp = requests.get(url, stream=True, timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _prepare(img: Image.Image) -> Image.Image:
    img.thumbnail((768, 768))
    return img


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        _client = genai.Client(api_key=api_key)
    return _client


def analyse_room(img: Image.Image) -> dict:
    """Single Gemini call to extract style, colours, mood, and search keywords from a room image."""
    client = _get_client()
    img = _prepare(img)
    prompt = (
        "Analyse this room image and describe it.\n"
        "style: interior design style in 2-4 words (e.g. Scandinavian minimalist)\n"
        "colors: list of 3 dominant colours\n"
        "mood: overall feel in 3-5 words\n"
        "search_keywords: 2-4 concrete IKEA-search-friendly terms describing colours and materials "
        "visible in the room (e.g. 'white oak', 'rattan', 'black metal') - NOT style adjectives like "
        "'Scandinavian minimalist', since IKEA's keyword search cannot match those"
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=[img, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RoomAnalysis,
        ),
    )
    result = response.parsed
    if result is None:
        import json
        result = RoomAnalysis(**json.loads(response.text))
    data = result.model_dump()
    for key in ("style", "colors", "mood", "search_keywords"):
        if not data.get(key):
            raise ValueError(f"analyse_room: missing or empty '{key}' in Gemini response")
    return data


def score_products(room_img: Image.Image, room: dict, product_images: list[Image.Image], furniture_type: str) -> list[dict]:
    """Single Gemini call to comparatively score a batch of product images against the room."""
    client = _get_client()
    room_img = _prepare(room_img)
    colors = ", ".join(room.get("colors", []))

    contents = ["Room photo:", room_img]
    for i, product_img in enumerate(product_images):
        contents.append(f"Product {i + 1}:")
        contents.append(_prepare(product_img))

    prompt = (
        f"The room photo above has a {room.get('style', 'modern')} style, a colour palette of "
        f"{colors}, and a {room.get('mood', 'neutral')} mood. "
        f"Above are {len(product_images)} candidate {furniture_type} products, each shown on a plain "
        "white background and numbered in order (Product 1, Product 2, ...).\n"
        "Score each product from 1 to 10 on how well it suits the room, judged on:\n"
        "- colour harmony with the room's palette\n"
        "- style match with the room's style\n"
        "- material fit\n"
        f"- overall suitability of this {furniture_type} for the room's mood\n"
        "IMPORTANT: scores are comparative, not absolute - use the full 1-10 range to DISCRIMINATE "
        "between the products. The best-fitting product(s) should score highest, the worst-fitting "
        "lowest; do not give every product a similar score.\n"
        "For each product return its 1-based product number, its score, and a one-sentence reason "
        "written for the end user explaining why it does or doesn't suit the room "
        "(e.g. \"Its light oak frame echoes the floor and warm neutrals of the room.\")."
    )
    contents.append(prompt)

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[ProductScore],
        ),
    )
    results = response.parsed
    if results is None:
        import json
        results = [ProductScore(**item) for item in json.loads(response.text)]

    scored = []
    seen = set()
    for item in results:
        index = item.product - 1
        if index < 0 or index >= len(product_images) or index in seen:
            continue
        seen.add(index)
        score = max(1, min(10, item.score))
        scored.append({"index": index, "score": score, "reason": item.reason})
    return scored
