# Decora

Decora is a Flask web app that recommends IKEA furniture based on an inspiration image URL and a furniture type. It uses Google Gemini to analyse the room's style, colours, and mood, then searches IKEA listings and scores each product for compatibility.

## Features

- Room style analysis via Google Gemini vision (style, colours, mood, keywords)
- IKEA product search and AI-based compatibility scoring
- Top 4 recommendations with match score, price, and rating
- Deployed on Vercel

## Tech Stack

- Python, Flask
- Google Gemini API (`gemini-3.1-flash-lite-preview`)
- IKEA API client (`ikea-api`)
- HTML/CSS/JS (Colorlib template)

## Setup

Recommended Python: 3.10 or 3.11.

1. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/Scripts/activate  # Windows
   # or
   source venv/bin/activate       # macOS/Linux
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your keys:
   ```
   SECRET_KEY=your-secret-key
   GEMINI_API_KEY=your-gemini-api-key
   ```

4. Run the app:
   ```bash
   python model.py
   ```

5. Open `http://127.0.0.1:5000` in your browser.

## Usage

Go to **Products**, enter an image URL and a furniture type (e.g. "chair", "sofa"). Decora analyses the room and returns up to 4 IKEA products scored for visual compatibility.

## Deployment

Configured for Vercel via `vercel.json`. Set `SECRET_KEY` and `GEMINI_API_KEY` as environment variables in your Vercel project settings.

## Notes

- Free tier Gemini limits: 500 requests/day, 15 requests/minute.
- IKEA API results depend on external availability.
- UI template by Colorlib — keep the footer attribution per the license in `readme.txt`.
