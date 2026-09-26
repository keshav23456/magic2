"""Multi-turn reply handling.

A fast rule layer classifies the inbound message first (auto-reply, opt-out, commitment, later, off-topic,
slot pick). Rules decide the *action* (send / wait / end); the LLM only writes the text for 'send'.
That keeps the high-stakes decisions deterministic and within the 30s budget.
"""
import json
import re

from . import llm
from .facts import build_facts, customer_language, merchant_language, salutation

AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)",
    r"our team will (respond|get back|contact|reply)",
    r"will (get back|respond|revert) (to you )?(shortly|soon|asap)",
    r"we are (currently )?(closed|unavailable|away)",
    r"(business|working) hours",
    r"automated (assistant|message|reply|response)",
    r"this is an auto",
    r"aapki jaankari ke liye",
    r"hamari team tak",
    r"we have received your (message|query)",
    r"away from (the )?(phone|desk)",
]
OPT_OUT_PATTERNS = [
    r"\bstop\b", r"unsubscribe", r"not interested", r"no interest", r"don'?t (message|text|contact|send)",
    r"do not (message|text|contact|send)", r"\bspam\b", r"useless", r"bothering", r"leave me alone",
    r"band karo", r"mat bhejo", r"nahi chahiye", r"mat karo message", r"block (you|kar)",
    r"why are you (messaging|bothering)",
]
ABUSE_WORDS = [r"\bf+u+c*k", r"\bidiot\b", r"\bstupid\b", r"\bchutiya", r"\bbakwas\b", r"\bbc\b", r"\bmc\b", r"\bshut up\b"]
COMMIT_PATTERNS = [
    r"let'?s do (it|this)", r"lets do", r"go ahead", r"\bdo it\b", r"yes,? please", r"\bproceed\b", r"sign me up",
    r"i want to join", r"want to join", r"judna hai", r"judrna hai", r"join karna", r"kar do", r"kardo", r"karo\b",
    r"haan( ji)?\b", r"\bha+n?\b", r"sounds good", r"ok(ay)?,? (let'?s|do|go|send|start)", r"\bconfirm(ed)?\b",
    r"^\s*(yes|yep|yeah|sure|ok|okay|done|chalo|theek hai|thik hai)\s*[.!]*\s*$", r"what'?s next", r"\bstart it\b",
    r"send (it|me)", r"publish it", r"make it live",
]
LATER_PATTERNS = [r"\blater\b", r"\bbusy\b", r"baad mein", r"not now", r"\btomorrow\b", r"\bkal\b", r"in a meeting",
                  r"call (me )?later", r"after some time", r"next week"]
OFF_TOPIC_PATTERNS = [r"\bgst\b", r"income tax", r"\bitr\b", r"\bloan\b", r"insurance", r"electricity bill", r"\bvisa\b",
                      r"\bcrypto\b", r"stock market", r"\bcibil\b", r"\bpan card\b", r"\baadhaar\b"]
HINGLISH_MARKERS = r"\b(hai|hain|nahi|nahin|karo|kya|aap|mujhe|haan|ji|chahiye|kaise|kab|mera|meri|hum|bhej|kar|abhi|accha|acha|theek|thik|bhai)\b"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about", "could you tell", "kya aap", "aapke kitne"]


def _any(patterns, text) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def classify(message: str, prior_inbound: list[str], from_role: str) -> str:
    m = (message or "").strip()
    n = _norm(m)
    if not n:
        return "empty"
    if _any(AUTO_REPLY_PATTERNS, m) or (prior_inbound and n == _norm(prior_inbound[-1]) and len(n) > 25):
        return "auto_reply"
    if _any(OPT_OUT_PATTERNS, m):
        return "opt_out"
    if from_role == "customer" and re.match(r"^\s*[1-4]\s*[.)]?\s*$", m):
        return "slot_pick"
    if _any(OFF_TOPIC_PATTERNS, m):
        return "off_topic"
    if _any(ABUSE_WORDS, m):
        return "abuse"
    if _any(COMMIT_PATTERNS, m):
        return "commit"
    if _any(LATER_PATTERNS, m):
        return "later"
    return "engaged"


def reply_language(message: str, default: str) -> str:
    if re.search(r"[\u0900-\u097F]", message or ""):
        return "Hindi-English code-mix (Hinglish, Roman script)"
    if re.search(HINGLISH_MARKERS, message or "", re.I):
        return "Hindi-English code-mix (Hinglish, Roman script)"
    if len((message or "").split()) >= 4:
        return "English"
    return default


REPLY_SYSTEM = """You are Vera, magicpin's merchant-growth assistant, mid-conversation on WhatsApp.
Write the NEXT message only. Rules:
- Use only facts from FACTS and the conversation. Never invent numbers, names, prices, studies or results.
- Don't re-introduce yourself. No preamble. 1-4 short sentences. Exactly one ask, as the last sentence.
- Never repeat a message you already sent in this conversation.
- No URLs. Peer tone matching the category voice. Write in the language requested.
- Service+price offers only (from active offers / catalog), never "% off".
MODE-SPECIFIC INSTRUCTIONS are below and override everything else.
OUTPUT JSON: {"body": "...", "cta": "binary_yes_no|binary_confirm_cancel|open_ended|multi_choice_slot|none", "rationale": "..."}"""

MODE_INSTRUCTIONS = {
    "commit": ("The merchant has COMMITTED. Switch to ACTION mode immediately. Do NOT ask any qualifying or discovery "
               "question. Say what you are doing now (e.g. 'Done — drafting X now'), state the concrete deliverable and "
               "when it'll be ready, and end with a single confirmation ask like 'Reply CONFIRM to publish'. "
               "cta must be binary_confirm_cancel."),
    "engaged": ("The merchant engaged or asked something. Answer their question directly using FACTS (say honestly if "
                "the data isn't available), then move the conversation one step forward toward the original goal with "
                "one low-friction ask. If they reveal a preference, adapt to it."),
    "off_topic": ("The merchant asked about something outside your scope (e.g. GST/tax/loans). Politely say you can't help "
                  "with that one and suggest they check with their CA/the right person, then bring it back in one line to "
                  "the pending topic with a yes/no ask. Stay warm, no lecture."),
    "abuse": ("The merchant is frustrated/rude. Don't argue or mirror. One short, calm apology, and offer to stop "
              "messaging — or continue only if they want. cta none."),
    "auto_reply_nudge": ("The last message was an automatic WhatsApp Business reply, not the owner. Write ONE short line "
                         "addressed to the owner saying it looks like an auto-reply and that when they see this they can "
                         "just reply YES for the pending item. Don't repeat the full pitch."),
    "slot_pick": ("The customer picked a slot number. Confirm the booking for exactly that slot label from the trigger "
                  "payload (if available), mention what to expect in one line, and ask them to reply if they need to "
                  "reschedule. cta none. You are writing as the merchant's team."),
}


async def compose_reply(mode: str, conv: dict, message: str, contexts: dict, timeout: float = 10.0) -> dict:
    merchant = contexts.get("merchant") or {"merchant_id": conv.get("merchant_id"), "identity": {}}
    category = contexts.get("category") or {}
    trigger = contexts.get("trigger") or {"kind": conv.get("trigger_kind") or "conversation", "payload": {}}
    customer = contexts.get("customer")
    default_lang = customer_language(customer) if customer else merchant_language(merchant)
    lang = reply_language(message, default_lang)

    facts = build_facts(category, merchant, trigger, customer) if category else {
        "merchant": {"salutation": salutation(category, merchant)}}
    turns = conv.get("turns", [])[-8:]
    user = (
        "FACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str)
        + "\n\nCONVERSATION SO FAR (oldest first):\n"
        + "\n".join(f"[{t['from']}] {t['body']}" for t in turns)
        + f"\n\nLATEST INBOUND ({conv.get('role', 'merchant')}): {message}"
        + f"\n\nMODE: {mode}\n{MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS['engaged'])}"
        + f"\nLanguage: {lang}.\nReturn only the JSON object."
    )
    raw = await llm.chat_json(REPLY_SYSTEM, user, timeout=timeout, max_tokens=350)
    out = None
    if raw and (raw.get("body") or "").strip():
        body = re.sub(r"(https?://\S+|www\.\S+)", "", raw["body"]).strip()
        out = {"body": body, "cta": raw.get("cta") or "open_ended",
               "rationale": (raw.get("rationale") or f"{mode} reply").strip()[:400]}
        if mode == "commit" and any(q in body.lower() for q in QUALIFYING):
            out = None  # guardrail: never qualify after a commitment
    if out is None:
        out = fallback_reply(mode, conv, merchant, category, trigger, lang)
        out["rationale"] = "[template fallback] " + out["rationale"]
    if mode == "commit":
        out["cta"] = "binary_confirm_cancel"
    if mode in ("abuse", "slot_pick"):
        out["cta"] = "none"
    return out


def fallback_reply(mode: str, conv: dict, merchant: dict, category: dict, trigger: dict, lang: str) -> dict:
    sal = salutation(category, merchant) if merchant.get("identity") else ""
    hi = lang.startswith("Hindi")
    intent = (trigger.get("payload") or {}).get("intent_topic")
    topic = str(intent).replace("_", " ") if intent else "the next step"
    if mode == "commit":
        body = (f"Done — main abhi {topic} ka draft ready kar rahi hoon, 10 minute mein aapke paas hoga. Reply CONFIRM to publish it."
                if hi else f"Done — drafting {topic} for you now; it'll be ready in about 10 minutes. Reply CONFIRM and I'll publish it.")
        return {"body": body, "cta": "binary_confirm_cancel", "rationale": "Explicit commitment detected; switched to action mode (no further qualifying)."}
    if mode == "off_topic":
        body = ("Yeh mere scope se bahar hai — iske liye aapke CA best rahenge. Pending item pe aage badhein? Reply YES."
                if hi else "That one's outside what I can help with — your CA is the right person for it. Shall we continue with the pending item? Reply YES.")
        return {"body": body, "cta": "binary_yes_no", "rationale": "Off-topic request; polite redirect back to mission."}
    if mode == "abuse":
        body = "Sorry for the trouble — I won't push further. If you'd like help later, just reply 'Hi Vera'."
        return {"body": body, "cta": "none", "rationale": "Frustration detected; de-escalate and offer an exit."}
    if mode == "auto_reply_nudge":
        body = (f"{sal + ', ' if sal else ''}lagta hai yeh auto-reply hai 🙂 Jab aap dekhein, bas YES reply kar dijiye."
                if hi else f"{sal + ', ' if sal else ''}looks like an auto-reply 🙂 Whenever you see this, just reply YES and I'll take it from there.")
        return {"body": body, "cta": "binary_yes_no", "rationale": "Detected WhatsApp Business auto-reply; one owner-directed nudge."}
    if mode == "slot_pick":
        return {"body": "Booked — see you then! Reply here if you need to reschedule.", "cta": "none",
                "rationale": "Customer picked a slot; confirming."}
    body = ("Samajh gayi. Main aapke liye pehla draft bana deti hoon — reply YES to go ahead."
            if hi else "Got it. I'll put together a first draft for you — reply YES to go ahead.")
    return {"body": body, "cta": "binary_yes_no", "rationale": "Engaged reply; moving one step forward with a low-friction ask."}
