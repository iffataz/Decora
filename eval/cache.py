"""Record/replay cache for the pipeline's non-Gemini I/O (IKEA search + image downloads).

Caching IKEA makes eval runs repeatable (the live catalogue and search ranking drift)
and lets runs happen where IKEA is unreachable, as long as the cache was recorded
somewhere it was. Gemini calls are never cached: they are what's being evaluated.

Modes:
  off     — no cache, everything live
  record  — read from the cache when present, otherwise fetch live and store
  offline — cache only; a miss raises CacheMiss
"""
import contextlib
import hashlib
import json
from pathlib import Path

import pandas as pd
from PIL import Image

import apiloader
import decora
import sender

MODES = ("off", "record", "offline")


class CacheMiss(RuntimeError):
    pass


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


@contextlib.contextmanager
def cached_io(cache_dir, mode="record"):
    """Patch search_load and _fetch_image with cached versions for the duration."""
    if mode not in MODES:
        raise ValueError(f"cache mode must be one of {MODES}, got {mode!r}")
    if mode == "off":
        yield
        return

    cache_dir = Path(cache_dir)
    search_dir = cache_dir / "search"
    image_dir = cache_dir / "images"
    search_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    real_search = apiloader.search_load
    real_fetch = decora._fetch_image

    def search_load(query):
        path = search_dir / f"{_key(query)}.json"
        if path.exists():
            data = json.loads(path.read_text())
            return pd.DataFrame(data["rows"], columns=data["columns"])
        if mode == "offline":
            raise CacheMiss(f"no cached IKEA search for {query!r}")
        df = real_search(query)
        payload = {"query": query, "columns": list(df.columns), "rows": json.loads(df.to_json(orient="records"))}
        path.write_text(json.dumps(payload, indent=1))
        return df

    def fetch_image(url):
        path = image_dir / f"{_key(url)}.jpg"
        if path.exists():
            with Image.open(path) as img:
                return img.convert("RGB")
        if mode == "offline":
            raise CacheMiss(f"no cached image for {url}")
        img = real_fetch(url)
        # Store at the size Gemini sees (decora._prepare caps at 768px) to keep the cache small.
        stored = img.copy()
        stored.thumbnail((768, 768))
        stored.save(path, "JPEG", quality=90)
        return img

    # sender imported search_load by name, so patch it there; _fetch_image is
    # always looked up through the decora module.
    sender.search_load = search_load
    decora._fetch_image = fetch_image
    try:
        yield
    finally:
        sender.search_load = real_search
        decora._fetch_image = real_fetch
