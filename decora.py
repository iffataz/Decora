"""Gemini vision layer: room analysis and batched product scoring (2 calls per search)."""
import logging
import os
import time
import httpx
import requests
from io import BytesIO
from PIL import Image
from pydantic import BaseModel
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

MODEL = "gemini-3.8-flash"
# 3.8 Flash slows down or 503s under demand spikes; flash-lite stays fast, so
# degrading a spiked call beats failing the whole search.
FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_TIMEOUT_MS = 30_000
# LOW is the lowest level 3.8 Flash accepts (MINIMAL errors); its default MEDIUM
# adds latency this task doesn't need. 3.5 Flash-Lite accepts LOW too.
THINKING = types.ThinkingConfig(thinking_level="LOW")
_client: genai.Client | None = None

logger = logging.getLogger(__name__)


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
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")
    if len(resp.content) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _prepare(img: Image.Image) -> Image.Image:
    img.thumbnail((768, 768))
    return img


def _generate_with_retries(client: genai.Client, **kwargs):
    """generate_content with one retry on transient errors/timeouts, then one
    attempt on FALLBACK_MODEL. Transient = 429/5xx or a client timeout."""
    for attempt in range(2):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            reason = f"HTTP {e.code}"
        except httpx.TimeoutException:
            reason = f"timeout after {GEMINI_TIMEOUT_MS // 1000}s"
        if attempt == 0:
            logger.warning("Gemini %s error (%s), retrying in 2s", kwargs["model"], reason)
            time.sleep(2)
    logger.warning("Gemini %s still failing (%s), falling back to %s", kwargs["model"], reason, FALLBACK_MODEL)
    return client.models.generate_content(**{**kwargs, "model": FALLBACK_MODEL})


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        )
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
    response = _generate_with_retries(
        client,
        model=MODEL,
        contents=[img, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RoomAnalysis,
            thinking_config=THINKING,
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
        f"If a product is clearly not a {furniture_type} (a different furniture category entirely), "
        "give it a score of 1 and say so in the reason.\n"
        "For each product return its 1-based product number, its score, and a one-sentence reason "
        "written for the end user explaining why it does or doesn't suit the room "
        "(e.g. \"Its light oak frame echoes the floor and warm neutrals of the room.\")."
    )
    contents.append(prompt)

    response = _generate_with_retries(
        client,
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[ProductScore],
            thinking_config=THINKING,
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
