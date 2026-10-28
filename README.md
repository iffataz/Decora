# Decora

[![CI](https://github.com/iffataz/Decora/actions/workflows/ci.yml/badge.svg)](https://github.com/iffataz/Decora/actions/workflows/ci.yml)

Decora is a Flask web app that recommends IKEA furniture for a room. Upload a room photo (or paste an image URL) and a furniture type; Gemini looks at the room, then visually compares IKEA candidates against it and returns the top matches — each with a one-sentence reason why it fits.

**Live on Vercel** — a search form on the home page; each search redirects to its own
result URL that can be refreshed, bookmarked or shared.

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
| Frontend | Jinja2 templates, hand-written CSS (no framework) |
| Rate limiting, result store | In-process, or Upstash Redis when configured |
| Deployment | Vercel (serverless) |
| Tests / lint | pytest, ruff, GitHub Actions |

## File structure

```
Decora/
├── api/
│   └── index.py          # Vercel serverless entry point
├── web/
│   ├── stat/
│   │   ├── styles.css     # The whole stylesheet: tokens, base, components
│   │   └── favicon.svg
│   ├── base.html          # Shell: CSS text logo, flash messages, slim footer
│   ├── index.html         # Home: hero, search form (photo upload or URL), how-it-works
│   ├── results.html       # Results: product tile grid with score bars and match reasons
│   └── error.html
├── apiloader.py           # IKEA search → pandas DataFrame (per-item validation, deduped)
├── decora.py              # Gemini vision: analyse_room(), score_products()
├── app.py                 # Flask routes (/, /process_url, /r/<token>)
├── ratelimit.py           # Per-IP + global search limits (in-process or Redis)
├── resultstore.py         # Stores search outcomes so results get their own URL
├── upstash.py             # Shared Upstash Redis REST client
├── tests/                 # pytest suite; no network, no API keys needed
├── .github/workflows/ci.yml
├── pyproject.toml         # ruff + pytest config
├── requirements-dev.txt
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

The rate limits apply locally too, so you get 10 searches an hour by default. While
developing, put `RATE_LIMIT_PER_IP=0` and `RATE_LIMIT_GLOBAL_PER_DAY=0` in your `.env`.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | yes | Google AI Studio key from a project with billing enabled (3.8 Flash free tier is ~20 RPD) |
| `SECRET_KEY` | yes | Flask session secret — any random string |
| `RATE_LIMIT_PER_IP` | no | Searches per IP per window (default 10, `0` disables) |
| `RATE_LIMIT_WINDOW_SECONDS` | no | Length of that window (default 3600) |
| `RATE_LIMIT_GLOBAL_PER_DAY` | no | Ceiling on searches per day across all visitors (default 200, `0` disables) |
| `UPSTASH_REDIS_REST_URL` | no | Upstash Redis REST URL — makes the limits exact across instances |
| `UPSTASH_REDIS_REST_TOKEN` | no | Token for the above |
| `TRUST_PROXY_HEADERS` | no | `0` to ignore `X-Real-IP` / `X-Forwarded-For` when not behind Vercel |
| `RESULT_TTL_SECONDS` | no | How long a result link stays alive (default 86400) |

## Result links

A search is a POST, but its result is a GET. `POST /process_url` runs the pipeline,
stores the outcome under an unguessable token and answers **303 See Other** pointing
at `/r/<token>`; the browser follows it with a GET and that is the page the visitor
lands on. Three things fall out of that:

- **Refresh is free and safe.** Reloading re-reads the store instead of re-submitting
  the form, so it costs no Gemini calls and raises no "confirm resubmission" prompt.
- **Results have an address.** The link can be bookmarked or sent to someone.
- **A repeat search is free.** A successful outcome is also indexed by a key derived
  from the room image and the furniture type, so running the same search again
  redirects straight to the page already computed. The key is a hash of a small
  thumbnail, so it is best-effort: a re-encoded copy of the same photo misses the
  cache and simply runs again.

Failures get a token too — so refreshing an error page doesn't re-run the pipeline —
but they expire in five minutes and are never indexed by search key, because a Gemini
outage must not be remembered as this room's answer for a day.

Rate limiting applies to the POST only. `/r/<token>` is cheap and idempotent, so
refreshing or sharing a link never consumes anyone's quota. A reused search does
still cost a slot, since metering deliberately runs before the image fetch.

### How long links last, and when they work

`RESULT_TTL_SECONDS` (default 24 hours) caps both the link and the repeat-search
reuse. It is not unlimited because stored results hold IKEA prices and ratings, which
drift.

Whether a link works at all depends on the backend, exactly as with rate limiting:

- **Upstash Redis** (`UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`): the store
  is shared, so links resolve from any instance and are genuinely shareable.
- **No configuration:** the store lives in the serverless instance's memory. This is a
  sharper limitation than it is for rate limiting, where a per-instance counter only
  makes the limit loose — here, a redirect that lands on a different instance finds
  nothing, and the visitor who just waited 20 seconds sees "that link has expired". A
  shared link only works for whoever is routed to the instance holding it.

So: add Upstash if result links need to work reliably. The miss is handled gracefully
either way — the expired page explains itself and links back to a fresh search — and
a storage outage degrades the app to that page rather than failing a search that
already succeeded.

## Rate limiting

Each search costs two Gemini calls on a billed project, so the endpoint is metered
(`ratelimit.py`). Two limits apply, both to `POST /process_url` only — browsing the
site is free:

- **per IP**, 10 searches an hour by default, so one visitor cannot loop the form;
- **global per day**, 200 by default, so a spread-out or distributed burst still
  cannot run the bill past a known ceiling. The per-IP limit alone is not spend
  protection, since IPs are cheap.

Metering happens after the cheap form validation but before the URL fetch and the
Gemini calls, so a typo in the form costs the visitor nothing while nothing past the
limit costs you anything. Over-limit requests get HTTP 429 with a `Retry-After`
header and a page saying roughly how long to wait.

### Choosing a backend

**No configuration** (the default): counters live in each serverless instance's
memory. There is nothing to set up, but each instance counts only its own requests,
so the real ceiling is the limit times the number of warm instances, and counters
reset when an instance recycles. This stops someone looping the form in a browser; it
will not hold a precise limit.

**Upstash Redis** (set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`):
counters are shared, so both limits are exact. Its REST API needs no extra dependency
and holds no connection open, which is what makes it work on serverless. The free tier
covers this app comfortably. The backend is picked automatically when both variables
are present.

Either way, if the backend errors the request is **allowed** — a limiter outage
shouldn't take the site down. Both limits use a fixed window, which costs one round
trip instead of a sorted-set read but allows up to 2x the limit right at a window
boundary.

Behind Vercel, the client address comes from `X-Real-IP` (falling back to the leftmost
`X-Forwarded-For` entry), which Vercel's edge rewrites. Running the app exposed
directly, a client can send those headers itself, so set `TRUST_PROXY_HEADERS=0` to
use the socket address only.

## Tests

```bash
pip install -r requirements-dev.txt
pytest          # 396 tests, ~8 seconds
ruff check .
```

Nothing in the suite touches the network or needs an API key: Gemini, the IKEA
catalogue and image fetching are all faked, so it runs offline and costs nothing.
`tests/conftest.py` sets dummy credentials and switches the rate limits off before
`app` is imported.

What it covers, roughly in order of how badly a regression would hurt:

| Area | File | The failure it guards against |
|------|------|-------------------------------|
| URL fetching | `test_fetching.py` | The room-image URL field reaching the private network — every address class and every redirect hop, pinned per address so a stdlib change can't widen it |
| Scoring | `test_sender.py` | A score landing on the wrong product. The index remapping is the nastiest bug in the app: it mislabels every result and still looks plausible |
| Gemini layer | `test_gemini.py` | Retries hammering a dead key, or a hallucinated product number attaching a score to a product that wasn't scored |
| Catalogue | `test_apiloader.py` | A change in IKEA's response shape turning into a 500, or silent whole-batch validation failure |
| Rate limiting | `test_ratelimit.py` | Limits not holding, counters leaking memory, or a limiter outage taking the site down |
| Routes | `test_app.py` | Leaking internals in an error page, NaN rendering as "nan", or spend past the rate limit |
| Result links | `test_resultstore.py` | A link resolving to the wrong search, a cached outage, or the in-process store growing without bound |
| Redis transport | `test_upstash.py` | A storage outage raising instead of degrading |

CI runs ruff once and the suite on Python 3.10 (the floor the dependencies allow)
and 3.13, on every push to `main`, every pull request, and on demand.

## Deployment

Configured for Vercel via `vercel.json`. Add `GEMINI_API_KEY` and `SECRET_KEY` in your
Vercel project's environment variable settings, then push to deploy.

`vercel.json` sets `maxDuration: 60` on the function. A search takes roughly 18-30 s,
which is longer than Vercel's default function timeout, so without this the request is
killed mid-pipeline. 60 s is the ceiling on the Hobby plan; Pro allows up to 300.

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

## Fetching images by URL

The *room image URL* field makes the server fetch a URL the user chose, so it goes
through `decora.fetch_user_image()` rather than the plain fetcher used for IKEA's CDN:

- the scheme must be `http` or `https`;
- the host is resolved and every address it returns must be public — private,
  loopback, link-local, reserved and multicast ranges are refused, which keeps the
  field from reaching the cloud metadata endpoint or anything on the internal network;
- redirects are walked by hand (max 3) so each hop is validated too, since a public URL
  is otherwise free to bounce to an internal one;
- the body is streamed with a running 10 MB cap, because `Content-Length` is only a hint
  a hostile server can omit, and `Image.MAX_IMAGE_PIXELS` caps the *decoded* size so a
  small compressed file can't expand into gigabytes of pixels.

Every rejection reaches the user as the same "Could not load an image from that URL",
deliberately: a message that distinguished "blocked address" from "fetch failed" would
turn the field into a port scanner.

One residual gap: the host is validated at resolve time and connected to a moment
later, so DNS rebinding between the two is still theoretically possible. Closing it
means connecting to the validated IP with an explicit `Host` header.
