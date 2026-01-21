# Decora - UniHack2024

Decora is a Flask web app that recommends furniture based on an inspiration image URL and a furniture type. It uses a visual question answering (VQA) model to understand the image, then searches IKEA listings to return matching items.

## Features
- Image-based style and color understanding via VQA
- IKEA product search and filtering
- Web UI for submitting image URLs and browsing results

## Tech Stack
- Python, Flask
- Hugging Face Transformers (VQA)
- IKEA API client
- HTML/CSS/JS template assets

## Setup
Recommended Python: 3.10 or 3.11 (some packages do not yet provide wheels for 3.14).

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the app:
   ```bash
   python model.py
   ```
4. Open `http://127.0.0.1:5000` in your browser.

## Usage
- Go to Products and enter an image URL plus a furniture type (e.g., "chair", "sofa").
- Results open product pages in a new tab.

## Notes
- The first run downloads the VQA model (`dandelin/vilt-b32-finetuned-vqa`), which requires internet access and can take a few minutes.
- IKEA API results depend on external availability.
- UI template based on a Colorlib design; keep the footer attribution per the license in `readme.txt`.
