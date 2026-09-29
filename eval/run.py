"""Run the matching pipeline over the eval cases and save every scored candidate.

    python -m eval.run --name baseline                 # record IKEA cache, call Gemini
    python -m eval.run --name prompt-v2 --cache offline --repeats 3
    python -m eval.metrics eval/runs/prompt-v2.json --compare eval/runs/baseline.json

Needs GEMINI_API_KEY (read from .env too). IKEA searches and product images go
through eval/cache (see cache.py), so later runs see the same catalogue.
"""
import argparse
import json
import logging
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

import decora
import sender
from eval import metrics
from eval.cache import MODES, cached_io

EVAL_DIR = Path(__file__).resolve().parent


class _FallbackCounter(logging.Handler):
    """Counts decora's 'falling back to <FALLBACK_MODEL>' warnings."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        if "falling back" in record.getMessage():
            self.count += 1


def _clean(value):
    """Make pandas/numpy scalars JSON-safe (NaN → None)."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def load_room(source):
    if source.startswith(("http://", "https://")):
        return decora._fetch_image(source)
    path = Path(source)
    if not path.is_absolute():
        path = EVAL_DIR / path
    with Image.open(path) as img:
        return img.convert("RGB")


def run_case(case, repeats, rng):
    """Run one case: analyse once, retrieve once, score `repeats` times.

    Repeat 0 uses the pipeline's own candidate order (what production does);
    later repeats shuffle it to measure position bias / scoring noise.
    """
    timings = {}
    t0 = time.perf_counter()
    room_img = load_room(case["room"])

    t = time.perf_counter()
    room = decora.analyse_room(room_img.copy())
    timings["analyse_s"] = time.perf_counter() - t

    t = time.perf_counter()
    rows, images = sender.find_candidates(room, case["furniture_type"])
    timings["search_s"] = time.perf_counter() - t

    candidates = [{
        "id": str(row["id"]),
        "name": _clean(row["name"]),
        "type_name": _clean(row["typeName"]),
        "image": _clean(row["mainImageUrl"]),
        "url": _clean(row["pipUrl"]),
        "price": _clean(row["salesPrice"]),
        "rating": _clean(row["ratingValue"]),
        "rating_count": _clean(row["ratingCount"]),
    } for row in rows]

    runs = []
    timings["score_s"] = []
    for r in range(repeats):
        order = list(range(len(rows)))
        if r > 0:
            rng.shuffle(order)
        t = time.perf_counter()
        scored = sender.score_candidates(
            room_img.copy(), room,
            [rows[i] for i in order], [images[i].copy() for i in order],
            case["furniture_type"],
        )
        timings["score_s"].append(time.perf_counter() - t)
        top = sender.select_top(scored)
        runs.append({
            "order": [candidates[i]["id"] for i in order],
            "scores": {str(p["id"]): {"score": int(p["score"]), "reason": p["reason"]} for p in scored},
            "top": [str(p["id"]) for p in top],
        })

    # Total = one production request (analyse + search + first score), excluding extra repeats.
    timings["total_s"] = timings["analyse_s"] + timings["search_s"] + timings["score_s"][0]
    timings["wall_s"] = time.perf_counter() - t0
    return {"analysis": room, "candidates": candidates, "repeats": runs, "timings": timings}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default=str(EVAL_DIR / "cases.json"))
    parser.add_argument("--name", default=datetime.now().strftime("%Y%m%d-%H%M%S"),
                        help="run name; output goes to eval/runs/<name>.json")
    parser.add_argument("--cache", choices=MODES, default="record")
    parser.add_argument("--cache-dir", default=str(EVAL_DIR / "cache"))
    parser.add_argument("--repeats", type=int, default=1,
                        help="score each case N times with shuffled candidate order (N-1 extra Gemini calls)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--only", nargs="*", help="run only these case ids")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cases_path = Path(args.cases)
    if not cases_path.exists():
        sys.exit(f"{cases_path} not found — copy eval/cases.example.json to eval/cases.json and add room photos")
    cases = json.loads(cases_path.read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]

    counter = _FallbackCounter()
    logging.getLogger("decora").addHandler(counter)
    rng = random.Random(args.seed)

    results = []
    with cached_io(args.cache_dir, args.cache):
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case['id']} ({case['furniture_type']})", flush=True)
            counter.count = 0
            entry = {"id": case["id"], "room": case["room"], "furniture_type": case["furniture_type"]}
            try:
                entry.update(run_case(case, args.repeats, rng))
            except Exception as e:
                logging.exception("case %s failed", case["id"])
                entry["error"] = f"{type(e).__name__}: {e}"
            entry["model_fallbacks"] = counter.count
            results.append(entry)

    run = {
        "name": args.name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "model": decora.MODEL,
            "fallback_model": decora.FALLBACK_MODEL,
            "cache": args.cache,
            "repeats": args.repeats,
            "seed": args.seed,
        },
        "cases": results,
    }
    out = EVAL_DIR / "runs" / f"{args.name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(run, indent=1))
    print(f"\nSaved {out}\n")

    summary, _ = metrics.summarise(run, metrics.load_labels())
    print(metrics.format_summary(summary))


if __name__ == "__main__":
    main()
