import json
import os
import re
import requests
from io import BytesIO
from PIL import Image
from google import genai

MODEL = "gemini-3.1-flash-lite-preview"
_client: genai.Client | None = None


def _fetch_image(url: str) -> Image.Image:
    resp = requests.get(url, stream=True, timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        _client = genai.Client(api_key=api_key)
    return _client


def analyse_room(url: str) -> tuple[dict, Image.Image]:
    """Single Gemini call to extract style, colours, mood, and search keywords from a room image.
    Returns the analysis dict and the fetched PIL image (reused for scoring)."""
    client = _get_client()
    img = _fetch_image(url)
    prompt = (
        "Analyse this room image and reply ONLY with valid JSON (no markdown fences) in this exact format:\n"
        '{"style": "...", "colors": ["...", "...", "..."], "mood": "...", "search_keywords": "..."}\n'
        "style: interior design style in 2-4 words (e.g. Scandinavian minimalist)\n"
        "colors: list of 3 dominant colours\n"
        "mood: overall feel in 3-5 words\n"
        "search_keywords: 2-4 words for IKEA search"
    )
    response = client.models.generate_content(model=MODEL, contents=[img, prompt])
    text = response.text.strip()
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    required = {"style", "colors", "mood", "search_keywords"}
    if not required.issubset(data):
        raise ValueError(f"Gemini response missing keys: {required - data.keys()}")
    return data, img


def score_product(image_url: str, furniture_type: str, room_img: Image.Image) -> int:
    """Direct visual comparison: show Gemini both the room and the product image.
    Returns an integer 1-10; returns 0 on any error."""
    try:
        client = _get_client()
        product_img = _fetch_image(image_url)
        prompt = (
            f"The first image is a room. The second is a {furniture_type}. "
            "Rate how well this piece fits the room's style, colours, and mood. "
            "Reply with ONLY a single integer from 1 to 10, nothing else."
        )
        response = client.models.generate_content(
            model=MODEL, contents=[room_img, product_img, prompt]
        )
        match = re.search(r"\d+", response.text.strip())
        if not match:
            return 0
        return max(1, min(10, int(match.group())))
    except Exception:
        return 0
