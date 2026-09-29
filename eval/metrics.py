"""Metrics over an eval run (eval/runs/*.json), with or without human labels.

    python -m eval.metrics eval/runs/new.json [--compare eval/runs/baseline.json]

Unlabelled metrics catch structural problems (off-category picks, duplicate colour
variants, flat scores, instability). Labelled metrics (eval/labels.json) measure
whether the picks are actually good, and split failures into retrieval (no good
product among the candidates) vs ranking (good products present but not picked).
"""
import argparse
import json
import re
import statistics
from itertools import combinations
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
LABEL_VALUES = {"good": 1.0, "ok": 0.5, "bad": 0.0}

# Furniture-type words → IKEA typeName endings that count as the same category.
# Heuristic: extend it when a case reports a false off-category hit.
SYNONYMS = {
    "couch": ["sofa", "sofa-bed"],
    "sofa": ["sofa", "sofa-bed"],
    "bookshelf": ["bookcase", "shelving unit", "shelf"],
    "bookcase": ["bookcase", "shelving unit"],
    "shelf": ["shelf", "shelving unit", "bookcase"],
    "lamp": ["lamp"],
    "light": ["lamp", "light"],
    "chair": ["chair"],
    "dresser": ["drawer"],
    "drawers": ["drawer"],
    "carpet": ["rug"],
    "bed": ["bed", "bed frame"],
    "tv stand": ["tv bench", "tv unit"],
    "tv unit": ["tv bench", "tv unit"],
    "nightstand": ["bedside table"],
    "side table": ["side table", "bedside table"],
    "wardrobe": ["wardrobe"],
    "curtains": ["curtain"],
    "mirror": ["mirror"],
}


def _singular(word):
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _norm(text):
    words = re.findall(r"[a-z0-9-]+", text.lower())
    return " ".join(_singular(w) for w in words)


def category_match(furniture_type, type_name):
    """True if IKEA's typeName looks like the requested furniture type.

    Compares the end of typeName (before any comma — "Rug, low pile" → "rug") so a
    "table lamp" does not count as a "table", but "coffee table" does.
    """
    if not type_name:
        return False
    main = _norm(type_name.split(",")[0])
    ft = _norm(furniture_type)
    head = ft.split()[-1] if ft else ft
    terms = {ft, head}
    for key in (furniture_type.lower().strip(), ft, head):
        terms.update(_norm(t) for t in SYNONYMS.get(key, []))
    return any(t and main.endswith(t) for t in terms)


def _top_ids(repeat):
    return repeat["top"]


def case_metrics(case, labels=None):
    """Metrics for one case. Repeat 0 (original candidate order) is the primary run."""
    if case.get("error"):
        return {"id": case["id"], "error": case["error"]}
    cands = {c["id"]: c for c in case["candidates"]}
    ft = case["furniture_type"]
    primary = case["repeats"][0]
    scores = {pid: s["score"] for pid, s in primary["scores"].items()}
    top = _top_ids(primary)

    off_cat = {pid for pid, c in cands.items() if not category_match(ft, c.get("type_name"))}
    flagged = {pid for pid, s in scores.items() if s <= 2}
    names = [cands[pid]["name"] for pid in top]

    m = {
        "id": case["id"],
        "furniture_type": ft,
        "n_candidates": len(cands),
        "n_top": len(top),
        "empty": not top,
        "threshold_fallback": bool(top) and max(scores.values(), default=0) < 6,
        "pool_off_category": len(off_cat),
        "top_off_category": sum(pid in off_cat for pid in top),
        # Of the candidates the heuristic says are off-category, how many did Gemini score 1-2?
        "off_category_caught": (len(off_cat & flagged) / len(off_cat)) if off_cat else None,
        "top_duplicate_names": len(names) - len(set(names)),
        "score_std": statistics.pstdev(scores.values()) if len(scores) > 1 else 0.0,
        "score_range": (max(scores.values()) - min(scores.values())) if scores else 0,
        "seconds": case["timings"]["total_s"],
        "model_fallbacks": case.get("model_fallbacks", 0),
    }

    if len(case["repeats"]) > 1:
        tops = [set(_top_ids(r)) for r in case["repeats"]]
        jac = [len(a & b) / len(a | b) if a | b else 1.0 for a, b in combinations(tops, 2)]
        m["top_stability"] = statistics.mean(jac)
        per_product = []
        for pid in cands:
            vals = [r["scores"][pid]["score"] for r in case["repeats"] if pid in r["scores"]]
            if len(vals) > 1:
                per_product.append(statistics.pstdev(vals))
        m["score_jitter"] = statistics.mean(per_product) if per_product else 0.0

    case_labels = (labels or {}).get(case["id"], {})
    if case_labels:
        top_vals = [LABEL_VALUES[case_labels[pid]] for pid in top if pid in case_labels]
        pool_good = [pid for pid in cands if case_labels.get(pid) == "good"]
        good_in_top = sum(case_labels.get(pid) == "good" for pid in top)
        m.update({
            "top_labelled": len(top_vals),
            "precision_at_4": statistics.mean(top_vals) if top_vals else None,
            "good_at_4": good_in_top,
            "bad_at_4": sum(case_labels.get(pid) == "bad" for pid in top),
            "pool_good": len(pool_good),
            # Ranking quality: of the good products that were available, how many made the top 4?
            "ranking_recall": (good_in_top / min(4, len(pool_good))) if pool_good else None,
        })
    return m


def _mean(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return statistics.mean(float(v) for v in vals)


def summarise(run, labels=None):
    per_case = [case_metrics(c, labels) for c in run["cases"]]
    ok = [m for m in per_case if "error" not in m]
    labelled = [m for m in ok if "precision_at_4" in m]
    summary = {
        "cases": len(per_case),
        "errors": len(per_case) - len(ok),
        "empty_rate": _mean(ok, "empty"),
        "threshold_fallback_rate": _mean(ok, "threshold_fallback"),
        "top_off_category_per_case": _mean(ok, "top_off_category"),
        "pool_off_category_per_case": _mean(ok, "pool_off_category"),
        "off_category_caught": _mean(ok, "off_category_caught"),
        "top_duplicate_names_per_case": _mean(ok, "top_duplicate_names"),
        "score_std": _mean(ok, "score_std"),
        "top_stability": _mean(ok, "top_stability"),
        "score_jitter": _mean(ok, "score_jitter"),
        "seconds_per_case": _mean(ok, "seconds"),
        "model_fallbacks_per_case": _mean(ok, "model_fallbacks"),
        "labelled_cases": len(labelled),
        "top_label_coverage": (
            sum(m["top_labelled"] for m in labelled) / sum(m["n_top"] for m in labelled)
            if labelled and sum(m["n_top"] for m in labelled) else None
        ),
        "precision_at_4": _mean(labelled, "precision_at_4"),
        "good_at_4": _mean(labelled, "good_at_4"),
        "bad_at_4": _mean(labelled, "bad_at_4"),
        "pool_has_good_rate": (sum(m["pool_good"] > 0 for m in labelled) / len(labelled)) if labelled else None,
        "ranking_recall": _mean(labelled, "ranking_recall"),
    }
    return summary, per_case


# (key, description, higher_is_better)
SUMMARY_ROWS = [
    ("errors", "cases that errored", False),
    ("empty_rate", "cases with no results", False),
    ("threshold_fallback_rate", "cases where nothing scored >= 6", False),
    ("top_off_category_per_case", "off-category products in top 4 (heuristic)", False),
    ("pool_off_category_per_case", "off-category candidates in pool (heuristic)", False),
    ("off_category_caught", "share of off-category candidates Gemini scored 1-2", True),
    ("top_duplicate_names_per_case", "repeated product names (colour variants) in top 4", False),
    ("score_std", "score spread per case (std dev)", True),
    ("top_stability", "top-4 overlap across shuffled repeats (Jaccard)", True),
    ("score_jitter", "per-product score std dev across repeats", False),
    ("seconds_per_case", "latency per case (s)", False),
    ("model_fallbacks_per_case", "Gemini fallback-model calls per case", False),
    ("labelled_cases", "cases with labels", None),
    ("top_label_coverage", "share of top-4 picks that are labelled", True),
    ("precision_at_4", "precision@4 (good=1, ok=0.5, bad=0)", True),
    ("good_at_4", "good products in top 4", True),
    ("bad_at_4", "bad products in top 4", False),
    ("pool_has_good_rate", "cases with >= 1 good candidate (retrieval ceiling)", True),
    ("ranking_recall", "good candidates that made the top 4", True),
]


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_summary(summary, baseline=None):
    lines = []
    width = max(len(d) for _, d, _ in SUMMARY_ROWS)
    header = f"{'metric':<{width}}  {'value':>8}"
    if baseline:
        header += f"  {'baseline':>8}  {'delta':>7}"
    lines.append(header)
    lines.append("-" * len(header))
    for key, desc, higher_better in SUMMARY_ROWS:
        v = summary.get(key)
        line = f"{desc:<{width}}  {_fmt(v):>8}"
        if baseline:
            b = baseline.get(key)
            line += f"  {_fmt(b):>8}"
            if isinstance(v, (int, float)) and isinstance(b, (int, float)) and v != b:
                d = v - b
                mark = ""
                if higher_better is not None:
                    mark = " +" if (d > 0) == higher_better else " -"
                line += f"  {d:>+7.2f}{mark}"
        lines.append(line)
    return "\n".join(lines)


def load_labels(path=EVAL_DIR / "labels.json"):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="run JSON from eval/run.py")
    parser.add_argument("--compare", help="baseline run JSON to diff against")
    parser.add_argument("--labels", default=str(EVAL_DIR / "labels.json"))
    parser.add_argument("--cases", action="store_true", help="also print per-case metrics")
    args = parser.parse_args()

    labels = load_labels(args.labels)
    summary, per_case = summarise(json.loads(Path(args.run).read_text()), labels)
    baseline = None
    if args.compare:
        baseline, _ = summarise(json.loads(Path(args.compare).read_text()), labels)
    print(format_summary(summary, baseline))
    if args.cases:
        print()
        for m in per_case:
            print(json.dumps(m))


if __name__ == "__main__":
    main()
