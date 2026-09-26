"""Turns raw context dicts into a compact, LLM-friendly fact sheet.

Only facts present in the contexts go in here — the composer is told to use nothing else.
"""
import re
from typing import Any

LANG_NAMES = {"hi": "Hindi", "mr": "Marathi", "te": "Telugu", "kn": "Kannada", "ta": "Tamil", "bn": "Bengali", "gu": "Gujarati"}


def owner_name(merchant: dict) -> str:
    return (merchant.get("identity") or {}).get("owner_first_name") or ""


def salutation(category: dict, merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    first = owner_name(merchant)
    if (category.get("slug") or merchant.get("category_slug")) == "dentists":
        return f"Dr. {first}" if first else ident.get("name", "Doctor")
    return first or ident.get("name", "")


def merchant_language(merchant: dict) -> str:
    langs = (merchant.get("identity") or {}).get("languages") or ["en"]
    if "hi" in langs:
        return "Hindi-English code-mix (Hinglish, Roman script) — natural, not forced"
    return "English"


def customer_language(customer: dict | None) -> str | None:
    if not customer:
        return None
    pref = ((customer.get("identity") or {}).get("language_pref") or "english").lower()
    if pref in ("english", "en"):
        return "English"
    if pref == "hi":
        return "Hindi (Roman script, simple and respectful)"
    if pref.endswith("-en mix"):
        code = pref.split("-")[0]
        name = LANG_NAMES.get(code, code)
        if code == "hi":
            return "Hindi-English code-mix (Hinglish, Roman script)"
        # We keep regional mixes light: English base with a warm local greeting word at most
        return f"English with a light {name} touch (Roman script); keep it simple"
    return "English"


def resolve_digest_item(category: dict, trigger: dict) -> dict | None:
    payload = trigger.get("payload") or {}
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    if not wanted:
        return None
    for item in category.get("digest") or []:
        if item.get("id") == wanted:
            return item
    return None


def _active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in merchant.get("offers") or [] if o.get("status") == "active" and o.get("title")]


def _other_offers(merchant: dict) -> list[str]:
    return [f"{o.get('title')} ({o.get('status')})" for o in merchant.get("offers") or [] if o.get("status") != "active"]


def build_facts(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    ident = merchant.get("identity") or {}
    voice = category.get("voice") or {}
    hist = merchant.get("conversation_history") or []
    facts: dict[str, Any] = {
        "category": category.get("slug") or merchant.get("category_slug"),
        "merchant": {
            "name": ident.get("name"),
            "salutation": salutation(category, merchant),
            "locality": ident.get("locality"),
            "city": ident.get("city"),
            "verified_on_google": ident.get("verified"),
            "established_year": ident.get("established_year"),
            "language_to_use": merchant_language(merchant),
            "subscription": merchant.get("subscription"),
            "performance_30d": merchant.get("performance"),
            "active_offers": _active_offers(merchant),
            "past_offers": _other_offers(merchant),
            "customer_aggregate": merchant.get("customer_aggregate"),
            "signals": merchant.get("signals"),
            "review_themes": merchant.get("review_themes"),
            "recent_conversation": [
                {"from": h.get("from"), "body": h.get("body"), "engagement": h.get("engagement")} for h in hist[-4:]
            ],
        },
        "category_knowledge": {
            "voice_tone": voice.get("tone"),
            "register": voice.get("register"),
            "vocab_allowed": (voice.get("vocab_allowed") or [])[:16],
            "taboo_words": voice.get("vocab_taboo") or [],
            "offer_catalog": [o.get("title") for o in category.get("offer_catalog") or []],
            "peer_stats": category.get("peer_stats"),
            "seasonal_beats": category.get("seasonal_beats"),
            "trend_signals": category.get("trend_signals"),
        },
        "trigger": {
            "kind": trigger.get("kind"),
            "scope": trigger.get("scope"),
            "source": trigger.get("source"),
            "urgency": trigger.get("urgency"),
            "payload": trigger.get("payload"),
            "expires_at": trigger.get("expires_at"),
        },
    }
    item = resolve_digest_item(category, trigger)
    if item:
        facts["trigger"]["referenced_item"] = item
    if customer:
        facts["customer"] = {
            "name": (customer.get("identity") or {}).get("name"),
            "language_to_use": customer_language(customer),
            "age_band": (customer.get("identity") or {}).get("age_band"),
            "relationship": customer.get("relationship"),
            "state": customer.get("state"),
            "preferences": customer.get("preferences"),
            "consent_scope": (customer.get("consent") or {}).get("scope"),
        }
    # Patient/customer content library titles (useful for "want me to share X?")
    lib = category.get("patient_content_library") or []
    if lib:
        facts["category_knowledge"]["shareable_content_titles"] = [c.get("title") for c in lib][:5]
    return facts


# ---------- number whitelist (anti-fabrication check) ----------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _collect_numbers(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        _add_number(float(obj), out)
    elif isinstance(obj, str):
        for tok in _NUM_RE.findall(obj):
            try:
                _add_number(float(tok.replace(",", "")), out)
            except ValueError:
                pass


def _add_number(x: float, out: set[str]) -> None:
    for v in (x, abs(x), x * 100, abs(x * 100)):
        out.add(_norm(v))
        out.add(_norm(round(v)))
        out.add(_norm(round(v, 1)))


def _norm(v: float) -> str:
    return f"{float(v):.2f}".rstrip("0").rstrip(".")


def allowed_numbers(facts: dict) -> set[str]:
    out: set[str] = set()
    _collect_numbers(facts, out)
    return out


def unknown_numbers(body: str, allowed: set[str]) -> list[str]:
    """Numbers in the body that don't trace back to the contexts. Small numbers (<=12) are ignored
    (counts like '2 slots', '3 posts', times like '6pm')."""
    bad = []
    for tok in _NUM_RE.findall(body or ""):
        try:
            v = float(tok.replace(",", ""))
        except ValueError:
            continue
        if v <= 12:
            continue
        if _norm(v) not in allowed and _norm(round(v)) not in allowed:
            bad.append(tok)
    return bad
