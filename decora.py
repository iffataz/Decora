"""Gemini vision layer: room analysis and batched product scoring (2 calls per search)."""
import ipaddress
import json
import logging
import os
import socket
import time
import httpx
import requests
from io import BytesIO
from urllib.parse import urljoin, urlparse
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


MAX_IMAGE_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 10
MAX_REDIRECTS = 3

# A 10 MB file can decode to gigabytes of pixels, so the byte cap alone isn't
# enough; cap decoded size too. Pillow raises DecompressionBombError past this.
Image.MAX_IMAGE_PIXELS = 50_000_000


def _read_image(resp: requests.Response) -> Image.Image:
    """Read an image from a streamed response, aborting past MAX_IMAGE_BYTES.

    Content-Length is only a hint (a hostile server can omit or lie about it),
    so the running total over the streamed chunks is what actually enforces the cap.
    """
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")

    chunks = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ValueError("image too large")
        chunks.append(chunk)

    img = Image.open(BytesIO(b"".join(chunks)))
    img.load()  # force decode here so a malformed body fails at the fetch, not mid-prompt
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _fetch_image(url: str) -> Image.Image:
    """Fetch an image from a trusted URL (IKEA's CDN). No host validation."""
    with requests.get(url, timeout=FETCH_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        return _read_image(resp)


def _assert_public_url(url: str) -> None:
    """Raise ValueError unless `url` is http(s) and resolves only to public addresses.

    Stops the room-image URL field being used to reach the cloud metadata
    endpoint, localhost, or anything else on the private network.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host: {host}") from e

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_global is False for private, loopback, link-local and reserved ranges.
        if not ip.is_global or ip.is_multicast:
            raise ValueError("URL resolves to a non-public address")


def fetch_user_image(url: str) -> Image.Image:
    """Fetch an image from a user-supplied URL, refusing private network targets.

    Redirects are walked by hand so every hop is validated; letting requests
    follow them would allow a public URL to bounce to an internal one.

    This narrows but does not close the hole: the host is validated at resolve
    time and connected to a moment later, so a DNS record that changes in
    between (rebinding) is still theoretically possible. Closing that needs
    connecting to the validated IP directly with an explicit Host header.
    """
    for _ in range(MAX_REDIRECTS + 1):
        _assert_public_url(url)
        with requests.get(url, timeout=FETCH_TIMEOUT, stream=True, allow_redirects=False) as resp:
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location")
                if not location:
                    raise ValueError("redirect without a Location header")
                url = urljoin(url, location)
                continue
            resp.raise_for_status()
            return _read_image(resp)
    raise ValueError("too many redirects")


def _prepare(img: Image.Image) -> Image.Image:
    """Downscale a copy to <=768 px. Copied so callers keep their original image."""
    out = img.copy()
    out.thumbnail((768, 768))
    return out


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
