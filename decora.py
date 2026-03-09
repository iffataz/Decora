import os
import base64
import requests
from io import BytesIO
from PIL import Image

HF_API_URL = "https://api-inference.huggingface.co/models/dandelin/vilt-b32-finetuned-vqa"

questions_inb = ["color", "type of design furniture", "describe vibe"]


def _image_to_base64(url: str) -> str:
    resp = requests.get(url, stream=True, timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def query(url: str, question: str, k: int, foreign: bool = True):
    token = os.environ.get("HF_API_TOKEN")
    if not token:
        raise RuntimeError("HF_API_TOKEN environment variable not set")

    image_b64 = _image_to_base64(url)
    response = requests.post(
        HF_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "inputs": {"question": question, "image": image_b64},
            "parameters": {"top_k": k},
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()
