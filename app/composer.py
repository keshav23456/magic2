"""compose(category, merchant, trigger, customer) -> message dict.

Pipeline: facts -> trigger playbook -> LLM (temp 0, JSON) -> validation (URLs, taboos, fabricated numbers,
CTA shape) -> one corrective retry -> deterministic fallback if the LLM is unavailable or keeps failing.
"""
import json
import re

from . import llm
from .facts import allowed_numbers, build_facts, salutation, unknown_numbers
from .playbooks import get_playbook

VALID_CTAS = {"binary_yes_no", "binary_confirm_cancel", "open_ended", "multi_choice_slot", "none"}
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)

SYSTEM_PROMPT = """You are Vera, magicpin's merchant-growth assistant on WhatsApp. You write ONE message.

HARD RULES
1. Use ONLY facts in the FACTS JSON. Never invent numbers, dates, names, offers, competitors, studies or sources.
   If a number isn't in FACTS, don't write it. Quote sources exactly as given (e.g. "JIDA Oct 2026, p.14").
2. Open with the salutation given in FACTS (e.g. "Dr. Meera," or "Suresh,"). No preamble, no "hope you're well",
   no self-introduction, no "I'm Vera".
3. Say WHY NOW in the first 1-2 sentences, using the trigger payload.
4. Anchor on at least one concrete, verifiable fact (a number, date, price, headline, peer benchmark).
   Comparing the merchant's number to the peer benchmark is strong when relevant.
5. Offers must be service+price from the merchant's active offers or the category offer catalog
   (e.g. "Dental Cleaning @ ₹299"). Never "X% off".
6. Exactly ONE call to action, and it is the LAST sentence. Prefer a low-friction ask where YOU do the work
   ("I've drafted it — reply YES and it goes live"). No multiple options, except numbered time slots for customer bookings.
7. Match the category voice and register. Never use any taboo word. Peer/colleague tone, never hype, no ALL CAPS, no "!!!".
8. Write in the requested language. For Hinglish: natural Roman-script code-mix, like a Delhi professional texting.
9. No URLs, no hashtags, no internal jargon (never say "trigger", "signal", "payload", "context", "CTR" is ok for merchants).
10. Concise: 2-5 short sentences, WhatsApp-readable. At most one emoji, only if it fits the category.
11. If the trigger is weak for this merchant (e.g. festival far away or not relevant), be honest and make it a light,
    useful heads-up rather than fake urgency.

OUTPUT: a JSON object with keys:
  "body": the WhatsApp message text,
  "cta": one of "binary_yes_no" | "binary_confirm_cancel" | "open_ended" | "multi_choice_slot" | "none",
  "rationale": 1-2 sentences: which fact you anchored on, why now, which engagement lever you used,
  "template_params": 2-4 short strings that would fill a pre-approved WhatsApp template for this message
                     (first = salutation/name, then the key fact(s)).
"""

CUSTOMER_ADDENDUM = """
THIS MESSAGE GOES TO THE MERCHANT'S CUSTOMER, sent from the merchant's number (not from Vera).
- Greet the customer by first name; speak as the merchant's team (e.g. "Dr. Meera's clinic here").
- Use the customer's language preference. Respect consent scope. No medical claims or advice.
- Warm, short, easy to reply to. Numbered slot choices are allowed for bookings.
"""


def _user_prompt(facts: dict, playbook: tuple[str, str, str], feedback: str | None) -> str:
    angle, levers, default_cta = playbook
    lang = (facts.get("customer") or {}).get("language_to_use") or facts["merchant"]["language_to_use"]
    parts = [
        "FACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str),
        f"\nTRIGGER PLAYBOOK ({facts['trigger']['kind']}): {angle}",
        f"Preferred engagement levers: {levers}. Suggested cta: {default_cta}.",
        f"Language: {lang}.",
    ]
    if feedback:
        parts.append(f"\nYOUR PREVIOUS DRAFT WAS REJECTED: {feedback} Rewrite it fixing exactly that.")
    parts.append("\nReturn only the JSON object.")
    return "\n".join(parts)


def validate(msg: dict, facts: dict) -> list[str]:
    problems = []
    body = (msg.get("body") or "").strip()
    if not body:
        return ["empty body"]
    if URL_RE.search(body):
        problems.append("it contains a URL; remove it.")
    low = body.lower()
    for t in facts["category_knowledge"].get("taboo_words") or []:
        word = t.split("(")[0].strip().lower()
        if word and word in low:
            problems.append(f"it uses the taboo phrase '{word}'.")
    bad = unknown_numbers(body, allowed_numbers(facts))
    if bad:
        problems.append(f"these numbers are not in FACTS: {', '.join(bad[:5])} — remove or replace with numbers from FACTS.")
    if re.search(r"\b\d{1,2}\s?% off\b", low):
        problems.append("it uses a generic '% off' offer; use service+price.")
    return problems


def _clean(msg: dict, default_cta: str) -> dict:
    body = URL_RE.sub("", (msg.get("body") or "")).strip()
    body = re.sub(r"[ \t]+", " ", body)
    cta = msg.get("cta") if msg.get("cta") in VALID_CTAS else default_cta
    params = msg.get("template_params")
    if not isinstance(params, list):
        params = []
    return {
        "body": body,
        "cta": cta,
        "rationale": (msg.get("rationale") or "").strip()[:500],
        "template_params": [str(p)[:120] for p in params][:4],
    }


async def compose_async(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
                        timeout: float = 10.0, allow_retry: bool = True) -> dict:
    facts = build_facts(category, merchant, trigger, customer)
    playbook = get_playbook(trigger.get("kind"))
    system = SYSTEM_PROMPT + (CUSTOMER_ADDENDUM if customer or trigger.get("scope") == "customer" else "")

    result = None
    feedback = None
    for attempt in range(2 if allow_retry else 1):
        raw = await llm.chat_json(system, _user_prompt(facts, playbook, feedback), timeout=timeout)
        if not raw:
            break
        cand = _clean(raw, playbook[2])
        problems = validate(cand, facts)
        if not problems:
            result = cand
            break
        feedback = " ".join(problems)
        # keep the draft if it's only a soft issue and we're out of retries
        if attempt == 1 and cand["body"] and not URL_RE.search(cand["body"]):
            result = cand
    if result is None:
        result = fallback_compose(category, merchant, trigger, customer)
        result["rationale"] = "[template fallback] " + result["rationale"]

    return _finalize(result, category, merchant, trigger, customer)


def _finalize(result: dict, category, merchant, trigger, customer) -> dict:
    is_customer = bool(customer) or trigger.get("scope") == "customer"
    kind = trigger.get("kind") or "generic"
    params = result.get("template_params") or [salutation(category, merchant)]
    return {
        "body": result["body"],
        "cta": result["cta"],
        "send_as": "merchant_on_behalf" if is_customer else "vera",
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": result.get("rationale") or f"{kind} trigger for {merchant.get('merchant_id')}",
        "template_name": f"{'merchant' if is_customer else 'vera'}_{kind}_v1",
        "template_params": params,
    }


# ---------------- deterministic fallback (no LLM) ----------------

def _pct(x) -> str:
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return ""


def fallback_compose(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    """Template composer used when the LLM is unavailable. Uses only context facts."""
    sal = salutation(category, merchant)
    kind = trigger.get("kind") or ""
    p = trigger.get("payload") or {}
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    offers = [o["title"] for o in merchant.get("offers") or [] if o.get("status") == "active" and o.get("title")]
    catalog = [o.get("title") for o in category.get("offer_catalog") or [] if o.get("title")]
    offer = offers[0] if offers else (catalog[0] if catalog else None)
    item = None
    wanted = p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")
    for d in category.get("digest") or []:
        if d.get("id") == wanted:
            item = d
    cta = "binary_yes_no"

    if customer:
        cname = (customer.get("identity") or {}).get("name") or "there"
        mname = (merchant.get("identity") or {}).get("name") or "your clinic"
        slots = p.get("available_slots") or p.get("next_session_options") or []
        if slots:
            opts = " ".join(f"Reply {i + 1} for {s.get('label')}." for i, s in enumerate(slots[:3]))
            body = f"Hi {cname}, {mname} here. It's time for your next visit — we've kept slots for you. {opts}"
            cta = "multi_choice_slot"
        elif kind == "chronic_refill_due":
            meds = ", ".join(p.get("molecule_list") or [])
            body = (f"Hi {cname}, {mname} here. Your refill for {meds} is due soon. "
                    f"Shall we keep it ready for you? Reply YES to confirm.")
        else:
            extra = f" {offer} is available for you." if offer else ""
            body = f"Hi {cname}, {mname} here. It's been a while since your last visit.{extra} Reply YES and we'll book a slot for you."
        return {"body": body, "cta": cta, "rationale": f"Customer-facing {kind}; used payload slots/offer only.",
                "template_params": [cname, mname]}

    if item:
        src = item.get("source", "")
        act = (item.get('actionable') or '').strip().rstrip('.')
        act = f"{act}. " if act else ""
        body = (f"{sal}, new this week — {item.get('title')} ({src}). "
                f"{act}Want me to prepare a short summary for you?")
        cta = "binary_yes_no"
    elif kind in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
        metric = p.get("metric", "views")
        direction = "up" if (p.get("delta_pct") or 0) > 0 else "down"
        body = (f"{sal}, your {metric} are {direction} {_pct(p.get('delta_pct'))} over the last {p.get('window', '7d')}. "
                f"Want me to draft a fresh Google post around {offer or 'your top service'} to act on this?")
    elif kind == "renewal_due":
        body = (f"{sal}, your {p.get('plan', '')} plan renews in {p.get('days_remaining')} days. "
                f"Last 30 days you got {perf.get('views')} views and {perf.get('calls')} calls. Reply YES to keep it running.")
    elif kind == "competitor_opened":
        body = (f"{sal}, {p.get('competitor_name')} opened {p.get('distance_km')} km away with {p.get('their_offer')}. "
                f"Want me to refresh your listing to highlight {offer or 'what sets you apart'}?")
    elif kind == "curious_ask_due":
        body = f"{sal}, quick one — which service are customers asking for most this week? I'll turn it into a Google post for you."
        cta = "open_ended"
    elif kind == "active_planning_intent":
        body = (f"{sal}, here's a first draft for {str(p.get('intent_topic', 'your plan')).replace('_', ' ')} — "
                f"built around {offer or 'your existing offers'}. Reply YES and I'll finalise and publish it.")
    elif kind == "gbp_unverified":
        body = (f"{sal}, your Google profile is still unverified — verified listings typically see about "
                f"{_pct(p.get('estimated_uplift_pct'))} more visibility. Want me to walk you through it? 5 minutes.")
    elif kind == "milestone_reached":
        body = (f"{sal}, you're at {p.get('value_now')} reviews — just {int(p.get('milestone_value', 0)) - int(p.get('value_now', 0))} "
                f"away from {p.get('milestone_value')}. Want me to draft a thank-you message asking happy customers for a review?")
    elif kind == "dormant_with_vera":
        ctr = perf.get("ctr")
        peer_ctr = peer.get("avg_ctr")
        anchor = f"your listing got {perf.get('views')} views in 30 days" if perf.get("views") else "I checked your listing"
        if ctr and peer_ctr:
            anchor += f" (CTR {ctr * 100:.1f}% vs {peer_ctr * 100:.1f}% peer avg)"
        body = f"{sal}, {anchor}. Want me to share 2 quick fixes?"
    else:
        anchor = f"{perf.get('views')} views and {perf.get('calls')} calls in the last 30 days" if perf.get("views") else "your listing"
        body = f"{sal}, quick update on {anchor}. Want me to draft a post around {offer or 'your top service'}?"
    return {"body": body, "cta": cta, "rationale": f"{kind}: anchored on payload/merchant numbers; single low-friction ask.",
            "template_params": [sal]}
