import os
import requests
from io import BytesIO
from PIL import Image
import google.generativeai as genai

questions_inb = ["color", "type of design furniture", "describe vibe"]


def _fetch_image(url: str) -> Image.Image:
    resp = requests.get(url, stream=True, timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def query(url: str, question: str, k: int, foreign: bool = True):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable not set")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash-lite")
    img = _fetch_image(url)

    if k == 1:
        # Binary yes/no check used when matching IKEA products against the room
        prompt = (
            f"{question} "
            "Reply with only the single word 'yes' or 'no', nothing else."
        )
        response = model.generate_content([img, prompt])
        answer = response.text.strip().lower()
        answer = "yes" if "yes" in answer else "no"
        return [{"answer": answer, "score": 1.0}]
    else:
        # Descriptive query — return k short answers
        prompt = (
            f"Look at this room or furniture image and answer: '{question}'. "
            f"Give exactly {k} different short answers (1–3 words each), "
            "separated by commas. Output only the answers, nothing else."
        )
        response = model.generate_content([img, prompt])
        answers = [a.strip() for a in response.text.strip().split(",")][:k]
        while len(answers) < k:
            answers.append(answers[-1] if answers else "unknown")
        return [{"answer": a, "score": 1.0} for a in answers]
