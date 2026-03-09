import sys
import os

# Ensure the project root is on the path so imports work correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from model import app  # noqa: F401 — Vercel expects 'app' as the WSGI callable
