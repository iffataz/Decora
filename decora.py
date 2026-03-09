from PIL import Image
from transformers import pipeline
import requests

questions_inb = ["color", "type of design furniture", "describe vibe"]


def query(url: str, question: str, k: int, foreign=True):
    vqa_pipeline = pipeline("visual-question-answering", model="dandelin/vilt-b32-finetuned-vqa")
    if foreign:
        image = Image.open(requests.get(url, stream=True).raw)
    else:
        image = Image.open(url)
    return vqa_pipeline(image, question, top_k=k)
