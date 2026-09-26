"""DeepInfra (OpenAI-compatible) client. Deterministic: temperature=0, fixed seed."""
import json
import os
import re
import time

import httpx

DEEPINFRA_URL = os.getenv("DEEPINFRA_URL", "https://api.deepinfra.com/v1/openai/chat/completions")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")

_client: httpx.AsyncClient | None = None


def api_key() -> str:
    return os.getenv("DEEPINFRA_API_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return _client


async def chat_json(system: str, user: str, timeout: float = 10.0, max_tokens: int = 350) -> dict | None:
    """Call the model and parse a JSON object from its reply. Returns None on any failure."""
    if not enabled():
        return None
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "seed": 42,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    t0 = time.time()
    try:
        r = await _get_client().post(
            DEEPINFRA_URL,
            json=body,
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=timeout,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        print(f"[llm] ok {time.time() - t0:.1f}s")
        return parse_json(text)
    except Exception as e:  # network, timeout, bad JSON — caller falls back
        print(f"[llm] error after {time.time() - t0:.1f}s: {type(e).__name__}: {str(e)[:200]}")
        return None


def parse_json(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
