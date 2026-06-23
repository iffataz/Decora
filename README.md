# Decora

Decora is a Flask web app that recommends IKEA furniture for a room. Paste an image URL and a furniture type; Gemini analyses the room's style, colours, and mood, then scores IKEA products for visual compatibility and returns the top matches.

**Live on Vercel** — two pages: a search form on the home page, and a results page.

## How it works

```
User submits room URL + furniture type
        ↓
analyse_room()   — 1 Gemini vision call → {style, colors, mood, search_keywords}
        ↓
search_load()    — IKEA catalogue search using room keywords + furniture type
        ↓
score_product()  — up to 10 Gemini calls, one per candidate product image
        ↓
Filter score ≥ 6, sort descending → top 4 results
(falls back to top 4 by score if nothing clears 6)
```

Each search makes at most **11 Gemini API calls** — well within the free-tier limit of 15 RPM.

## Tech stack

| Layer | Tool |
|-------|------|
| Backend | Python, Flask |
| AI | Google Gemini `gemini-3.1-flash-lite-preview` via `google-genai` |
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
│   │   ├── style.css
│   │   ├── styles1.css
│   │   └── js/
│   │       ├── bootstrap.min.js
│   │       └── jquery-3.3.1.min.js
│   ├── base.html          # Shell: CSS text logo + slim footer
│   ├── index.html         # Home: hero, search form, how-it-works
│   ├── test_results.html  # Results: product tile grid
│   └── error.html
├── apiloader.py           # IKEA search → pandas DataFrame
├── decora.py              # Gemini vision: analyse_room(), score_product()
├── model.py               # Flask routes (/, /process_url, /result)
├── sender.py              # Pipeline: analysis → search → score → rank
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
python model.py
# → http://127.0.0.1:5000
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI Studio key (free tier: 500 RPD, 15 RPM) |
| `SECRET_KEY` | Flask session secret — any random string |

## Deployment

Configured for Vercel via `vercel.json`. Add `GEMINI_API_KEY` and `SECRET_KEY` in your Vercel project's environment variable settings, then push to deploy.

## Scoring engine

`decora.py` contains two functions called per search:

- **`analyse_room(url)`** — fetches the room image, sends it to Gemini with a structured prompt, and returns a JSON dict: `{style, colors, mood, search_keywords}`.
- **`score_product(image_url, furniture_type, room)`** — fetches a product image and asks Gemini to rate (1–10) how well it suits the room's style, colours, and mood. Returns an integer; returns `0` on any error so the pipeline can continue.

A module-level `genai.Client` singleton is reused across all calls in a single request to avoid redundant initialisation.
