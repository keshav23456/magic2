"""Submission entry point (challenge-brief §7.1): compose(category, merchant, trigger, customer) -> dict.
The HTTP server lives in app/main.py; this is the same composer, callable synchronously."""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.composer import compose_async  # noqa: E402


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    msg = asyncio.run(compose_async(category, merchant, trigger, customer, timeout=25.0, allow_retry=True))
    return {k: msg[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}
