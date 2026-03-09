import json
import os
import re
import requests
from io import BytesIO
from PIL import Image
import google.generativeai as genai


def _fetch_image(url: str) -> Image.Image:
    resp = requests.get(url, stream=True, timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _get_model():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable not set")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel("gemini-2.0-flash-lite")


def analyse_room(url: str) -> dict:
    """Single Gemini call to extract style, colours, mood, and search keywords from a room image."""
    model = _get_model()
    img = _fetch_image(url)
    prompt = (
        "Analyse this room image and reply ONLY with valid JSON (no markdown fences) in this exact format:\n"
        '{"style": "...", "colors": ["...", "...", "..."], "mood": "...", "search_keywords": "..."}\n'
        "style: interior design style in 2-4 words (e.g. Scandinavian minimalist)\n"
        "colors: list of 3 dominant colours\n"
        "mood: overall feel in 3-5 words\n"
        "search_keywords: 2-4 words for IKEA search"
    )
    response = model.generate_content([img, prompt])
    text = response.text.strip()
    # Strip accidental ```json fences
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def score_product(image_url: str, furniture_type: str, room: dict) -> int:
    """Ask Gemini to rate how well a product image suits the room. Returns integer 1-10."""
    try:
        model = _get_model()
        img = _fetch_image(image_url)
        colors = ", ".join(room.get("colors", []))
        prompt = (
            f"Rate how well this {furniture_type} suits a {room.get('style', 'modern')} room "
            f"with {colors} colours and a {room.get('mood', 'neutral')} mood. "
            "Reply with ONLY a single integer from 1 to 10, nothing else."
        )
        response = model.generate_content([img, prompt])
        match = re.search(r"\d+", response.text.strip())
        return int(match.group()) if match else 0
    except Exception:
        return 0
