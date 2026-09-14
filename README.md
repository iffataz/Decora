# Decora

Decora is a Flask web app that recommends IKEA furniture for a room. Upload a room photo (or paste an image URL) and a furniture type; Gemini looks at the room, then visually compares IKEA candidates against it and returns the top matches — each with a one-sentence reason why it fits.

**Live on Vercel** — a search form on the home page, results rendered directly from the submission.

Demoing it? See the [demo guide](docs/DEMO.md) for a step-by-step walkthrough with talking points.

## How it works

```
User submits room photo/URL + furniture type
        ↓
analyse_room()    — 1 Gemini vision call → {style, colors, mood, search_keywords}
        ↓
search_load() ×2  — IKEA search: "{furniture type} {keywords}" (max 6 results)
                    + plain "{furniture type}" filling to 10 candidates, deduped
        ↓
score_products()  — 1 batched Gemini call: room photo + all candidate product
                    images together, scored comparatively (1-10) against a rubric
                    (colour harmony, style, material, mood) with a per-product reason
        ↓
Filter score ≥ 6, sort descending → top 4 results
(falls back to score ≥ 3 if nothing clears 6; scores 1-2 mean
 "wrong furniture category" and are never shown)
```

Each search makes exactly **2 Gemini API calls**. Calls use `gemini-3.8-flash` with
`thinking_level="LOW"` (its lowest supported level) and a 30 s timeout; on a 429/5xx or
timeout the call is retried once, then sent to `gemini-3.5-flash-lite`. 3.8 Flash's free
tier allows only ~20 requests/day, so production runs on a billed project. Candidate
product images are fetched in parallel and resized to ≤768 px before being sent.

### Why batched comparative scoring?

Two earlier designs performed worse:

- **Text-proxy scoring** (one call per product against a ~15-word text summary of the
  room) lost the visual information and produced undiscriminating scores — everything
  clustered around 7–9.
- **Pairwise visual scoring** (room + one product per call, commit `a6330a5`, reverted)
  compared the room against *contextual* product shots — the product staged in a
  different styled room — so the model effectively compared two rooms.

The current design shows Gemini the room and all candidates' **white-background**
product shots in a single call, which forces comparative ranking, and instructs it to
score wrong-category items 1 (IKEA keyword search occasionally returns off-category
products, e.g. TV benches for a floor-lamp query).

## Tech stack

| Layer | Tool |
|-------|------|
| Backend | Python, Flask |
| AI | Google Gemini `gemini-3.8-flash` (fallback `gemini-3.5-flash-lite`) via `google-genai` (structured JSON output) |
| Furniture data | `ikea-api` (Australia store) |
| Data wrangling | pandas, Pydantic |
| Frontend | Jinja2 templates, Bootstrap, custom CSS |
| Deployment | Vercel (serverless) |

## File structure

```
Decora/
├── api/
│   └── index.py          # Vercel serverless entry point
├── web/
│   ├── stat/
│   │   ├── bootstrap.min.css
│   │   ├── style.css      # legacy base stylesheet
│   │   ├── styles1.css    # BEM styles (search form, product tiles)
│   │   └── js/
│   │       ├── bootstrap.min.js
│   │       └── jquery-3.3.1.min.js
│   ├── base.html          # Shell: CSS text logo + slim footer
│   ├── index.html         # Home: hero, search form (photo upload or URL), how-it-works
│   ├── test_results.html  # Results: product tile grid with score bars and match reasons
│   └── error.html
├── apiloader.py           # IKEA search → pandas DataFrame (per-item validation, deduped)
├── decora.py              # Gemini vision: analyse_room(), score_products()
├── app.py                 # Flask routes (/, /process_url, /result)
├── sender.py              # Pipeline: analysis → search → batch-score → rank
├── requirements.txt
├── vercel.json
└── .env.example
```

## Local setup

Requires Python 3.10+.

```bash
python -m venv venv
source venv/Scripts/activate   # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

cp .env.example .env
# Edit .env and add your keys
```

```bash
python app.py
# → http://127.0.0.1:5000
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI Studio key from a project with billing enabled (3.8 Flash free tier is ~20 RPD) |
| `SECRET_KEY` | Flask session secret — any random string |

## Deployment

Configured for Vercel via `vercel.json`. Add `GEMINI_API_KEY` and `SECRET_KEY` in your Vercel project's environment variable settings, then push to deploy.

## Scoring engine

`decora.py` contains the two Gemini calls made per search:

- **`analyse_room(img)`** — takes a PIL image, returns `{style, colors, mood,
  search_keywords}` via Gemini structured output (`response_schema`). The keywords are
  concrete colour/material terms ("white oak", "rattan") rather than style adjectives,
  because IKEA's keyword search can't match the latter.
- **`score_products(room_img, room, product_images, furniture_type)`** — one batched
  call scoring every candidate against the room photo. Returns
  `[{index, score, reason}, ...]`; scores are clamped to 1–10, duplicate or
  out-of-range product indices are dropped, and API failures raise to the caller
  (no silent zero-scores).

Uploads are handled entirely in memory (8 MB cap) — nothing is written to disk, which
keeps the app compatible with Vercel's serverless filesystem. A module-level
`genai.Client` singleton is reused across calls.
