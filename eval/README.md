# Matching eval

A repeatable way to tell whether a change to Decora's matching (prompt, model,
retrieval, thresholds) makes recommendations better or worse.

```
eval/
├── cases.json         # rooms × furniture types to evaluate (start from cases.example.json)
├── rooms/             # room photos referenced by cases.json
├── labels.json        # your good / ok / bad judgements per (case, product)
├── cache/             # recorded IKEA searches + product images (see "Cache")
├── runs/<name>.json   # one file per eval run: every candidate, score, reason, timing
├── run.py             # runs the pipeline over the cases
├── metrics.py         # summarises a run, optionally against a baseline
└── label.py           # builds label.html for labelling candidates
```

## Workflow

**1. Add cases.** Copy `cases.example.json` to `cases.json`, put room photos in
`rooms/`, and edit the list. `room` is a path relative to `eval/` or an image URL.
Aim for 20–40 cases: varied styles, palettes and lighting, some cluttered and some
sparse, and a spread of furniture types (including ones IKEA search handles badly,
like "floor lamp"). Using one room with several furniture types is fine.
Photos in `rooms/` get committed, so only use ones you're happy to have in the repo.

**2. Run a baseline.** Needs `GEMINI_API_KEY` (read from `.env`) and access to IKEA.

```bash
python -m eval.run --name baseline --repeats 3
```

Each case costs `1 + repeats` Gemini calls. Repeat 0 uses the production candidate
order; the extra repeats shuffle it to measure how much order and noise change the picks.

**3. Label.**

```bash
python -m eval.label eval/runs/baseline.json
```

Open `eval/label.html` in a browser and mark each candidate good / ok / bad for its
room. Model scores are hidden by default so they don't anchor you. Click
**Download labels.json** and save it over `eval/labels.json`. Labels are per product,
so they carry over to future runs; when a change retrieves new products, rebuild the
page from the new run and label only the new ones (the page pre-fills existing labels,
and the "share of top-4 picks that are labelled" metric shows when you're behind).

**4. Change something, then compare.**

```bash
python -m eval.run --name shuffle-fix --cache offline --repeats 3
python -m eval.metrics eval/runs/shuffle-fix.json --compare eval/runs/baseline.json
```

`--cache offline` guarantees the new run sees exactly the same IKEA catalogue as the
baseline, so only your change differs. Add `--cases` to `metrics` for a per-case
breakdown.

## Metrics

Unlabelled (available from the first run):

| Metric | What it catches |
|---|---|
| off-category in top 4 / in pool | wrong furniture shown or retrieved. Uses IKEA's `typeName`; the synonym table in `metrics.py` is a heuristic, so extend it if a case is flagged wrongly |
| share of off-category candidates Gemini scored 1–2 | whether the scorer's category check works |
| repeated product names in top 4 | colour variants of one product crowding out alternatives |
| score spread | flat, undiscriminating scores (the old 7–9 clustering) |
| top-4 overlap across shuffled repeats, score jitter | position bias and noise (needs `--repeats` ≥ 2) |
| no results / nothing scored ≥ 6 | thresholds, retrieval failures |
| latency, fallback-model calls | cost of a change in speed and reliability |

Labelled:

| Metric | Meaning |
|---|---|
| precision@4 | average label of the top-4 picks (good = 1, ok = 0.5, bad = 0) |
| good@4 / bad@4 | good or bad products shown per case |
| cases with ≥ 1 good candidate | **retrieval ceiling**: if the pool has no good product, no scoring change can help |
| good candidates that made the top 4 | **ranking quality**: of the good products retrieved, how many got picked |

When precision is low, the last two tell you whether to work on retrieval or scoring.

## Cache

`run.py` routes IKEA searches and product-image downloads through `cache/`:

- `--cache record` (default) reuses cached responses and records new ones.
- `--cache offline` uses the cache only and fails on a miss.
- `--cache off` goes live every time.

Gemini is never cached. Product images are stored at 768 px (the size Gemini sees),
roughly 50–100 KB each. Committing `cache/` lets anyone, including a Claude Code
session that can't reach IKEA, rerun the eval against the same catalogue.
