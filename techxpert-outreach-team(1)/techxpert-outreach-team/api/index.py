"""Vercel serverless entrypoint. Exposes the Flask app as the serverless handler."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

# Vercel's Python runtime looks for `app` in api/index.py
