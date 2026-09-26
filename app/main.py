"""Vera bot — HTTP server for the magicpin AI Challenge.

Endpoints: POST /v1/context, POST /v1/tick, POST /v1/reply, GET /v1/healthz, GET /v1/metadata (+ POST /v1/teardown).
State is in memory; run with a single worker.
"""
import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import json  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from . import llm  # noqa: E402
from .composer import compose_async, fallback_compose  # noqa: E402
from .replies import classify, compose_reply  # noqa: E402

START = time.time()
STATE_FILE = os.getenv("STATE_FILE", "/tmp/vera_state.json")
SNAPSHOT_EVERY_S = float(os.getenv("SNAPSHOT_EVERY_S", "15"))
KEEPALIVE_URL = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
KEEPALIVE_EVERY_S = float(os.getenv("KEEPALIVE_EVERY_S", "600"))
VALID_SCOPES = ("category", "merchant", "customer", "trigger")

TICK_BUDGET_S = float(os.getenv("TICK_BUDGET_S", "11"))
REPLY_BUDGET_S = float(os.getenv("REPLY_BUDGET_S", "11"))
MAX_ACTIONS_PER_TICK = int(os.getenv("MAX_ACTIONS_PER_TICK", "20"))
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "8"))

# ---------------- state ----------------
contexts: dict[tuple[str, str], dict] = {}      # (scope, id) -> {"version", "payload"}
conversations: dict[str, dict] = {}             # conversation_id -> state
sent_suppression_keys: set[str] = set()
suppressed_merchants: dict[str, str] = {}       # merchant_id -> reason (opt-out / auto-reply exit)
merchant_auto_reply_count: dict[str, int] = {}  # consecutive auto-replies per merchant
precomputed: dict[str, dict] = {}               # trigger_id -> {"sig", "msg"}
_bg_tasks: set[asyncio.Task] = set()
_sem = asyncio.Semaphore(LLM_CONCURRENCY)
_dirty = {"v": False}


def mark_dirty() -> None:
    _dirty["v"] = True


# ---------------- persistence (best-effort local snapshot) ----------------
def save_state() -> None:
    data = {
        "contexts": [[s, c, v] for (s, c), v in contexts.items()],
        "conversations": {k: {**v, "sent_bodies": sorted(v.get("sent_bodies", []))} for k, v in conversations.items()},
        "sent_suppression_keys": sorted(sent_suppression_keys),
        "suppressed_merchants": suppressed_merchants,
        "merchant_auto_reply_count": merchant_auto_reply_count,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)  # atomic


def load_state() -> None:
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for s_, c_, v_ in data.get("contexts", []):
            contexts[(s_, c_)] = v_
        for k, v in data.get("conversations", {}).items():
            v["sent_bodies"] = set(v.get("sent_bodies", []))
            conversations[k] = v
        sent_suppression_keys.update(data.get("sent_suppression_keys", []))
        suppressed_merchants.update(data.get("suppressed_merchants", {}))
        merchant_auto_reply_count.update(data.get("merchant_auto_reply_count", {}))
        print(f"[state] restored {len(contexts)} contexts, {len(conversations)} conversations from {STATE_FILE}")
    except Exception as e:
        print(f"[state] could not restore snapshot: {e}")


async def _snapshot_loop() -> None:
    while True:
        await asyncio.sleep(SNAPSHOT_EVERY_S)
        if _dirty["v"]:
            _dirty["v"] = False
            try:
                save_state()
            except Exception as e:
                print(f"[state] snapshot failed: {e}")


async def _keepalive_loop() -> None:
    """Render free instances sleep after 15 min without inbound traffic; ping our own public URL."""
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await asyncio.sleep(KEEPALIVE_EVERY_S)
            try:
                r = await client.get(f"{KEEPALIVE_URL}/v1/healthz")
                print(f"[keepalive] {r.status_code}")
            except Exception as e:
                print(f"[keepalive] failed: {e}")


@asynccontextmanager
async def lifespan(_app):
    load_state()
    loops = [asyncio.create_task(_snapshot_loop())]
    if KEEPALIVE_URL:
        loops.append(asyncio.create_task(_keepalive_loop()))
        print(f"[keepalive] pinging {KEEPALIVE_URL}/v1/healthz every {KEEPALIVE_EVERY_S:.0f}s")
    yield
    for t in loops:
        t.cancel()
    if _dirty["v"]:
        try:
            save_state()
        except Exception:
            pass


app = FastAPI(title="Vera bot", lifespan=lifespan)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get(scope: str, cid: str | None) -> dict | None:
    if not cid:
        return None
    c = contexts.get((scope, cid))
    return c["payload"] if c else None


def _ver(scope: str, cid: str | None) -> int:
    c = contexts.get((scope, cid)) if cid else None
    return c["version"] if c else 0


def _bundle(trigger: dict) -> tuple[dict | None, dict | None, dict | None]:
    merchant = get("merchant", trigger.get("merchant_id"))
    category = get("category", (merchant or {}).get("category_slug")) if merchant else None
    customer = get("customer", trigger.get("customer_id"))
    return category, merchant, customer


def _signature(trigger_id: str, trigger: dict) -> str:
    merchant = get("merchant", trigger.get("merchant_id")) or {}
    return "|".join(str(x) for x in (
        _ver("trigger", trigger_id), _ver("merchant", trigger.get("merchant_id")),
        _ver("category", merchant.get("category_slug")), _ver("customer", trigger.get("customer_id"))))


# ---------------- background precompute ----------------
async def _precompute(trigger_id: str) -> None:
    trigger = get("trigger", trigger_id)
    if not trigger:
        return
    category, merchant, customer = _bundle(trigger)
    if not (category and merchant):
        return
    sig = _signature(trigger_id, trigger)
    if precomputed.get(trigger_id, {}).get("sig") == sig:
        return
    async with _sem:
        msg = await compose_async(category, merchant, trigger, customer, timeout=20.0, allow_retry=True)
    if _signature(trigger_id, trigger) == sig:  # contexts didn't change while we were composing
        precomputed[trigger_id] = {"sig": sig, "msg": msg}


def _schedule(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _triggers_touching(scope: str, cid: str) -> list[str]:
    out = []
    for (s, tid), c in contexts.items():
        if s != "trigger":
            continue
        p = c["payload"]
        if scope == "merchant" and p.get("merchant_id") == cid:
            out.append(tid)
        elif scope == "customer" and p.get("customer_id") == cid:
            out.append(tid)
        elif scope == "category":
            m = get("merchant", p.get("merchant_id"))
            if m and m.get("category_slug") == cid:
                out.append(tid)
    return out


# ---------------- endpoints ----------------
@app.get("/")
async def root():
    return {"service": "vera-bot", "status": "ok"}


@app.get("/v1/healthz")
async def healthz():
    counts = {s: 0 for s in VALID_SCOPES}
    for (scope, _cid) in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Keshav Soni"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Keshav Soni").split(",")],
        "model": llm.MODEL,
        "approach": ("Trigger-routed composer: context -> compact fact sheet -> per-trigger-kind playbook -> "
                     "Llama-3.3-70B (temp 0, JSON) -> validator (fabricated numbers, taboos, URLs, CTA) with one "
                     "corrective retry -> deterministic template fallback. Compositions precomputed on context push. "
                     "Rule-based reply router (auto-reply, opt-out, commitment, off-topic, later) decides send/wait/end; "
                     "LLM writes the text."),
        "contact_email": os.getenv("CONTACT_EMAIL", "keshav.ug23@nsut.ac.in"),
        "version": "1.0.0",
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-26T00:00:00Z"),
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_json", "details": "body is not JSON"})
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": f"scope={scope!r}"})
    if not cid or not isinstance(payload, dict) or not isinstance(version, int):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_body",
                                                      "details": "context_id, integer version and object payload are required"})
    key = (scope, cid)
    cur = contexts.get(key)
    if cur and cur["version"] > version:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]})
    if cur and cur["version"] == version:  # idempotent re-post
        return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": cur["stored_at"]}
    stored_at = _now_iso()
    contexts[key] = {"version": version, "payload": payload, "stored_at": stored_at}
    mark_dirty()

    # Warm the composition cache for affected triggers (only when the LLM is configured).
    if llm.enabled():
        tids = [cid] if scope == "trigger" else _triggers_touching(scope, cid)
        for tid in tids:
            _schedule(_precompute(tid))
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": stored_at}


def _expired(trigger: dict, now: datetime) -> bool:
    exp = trigger.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) < now
    except ValueError:
        return False


def _parse_now(s: str | None) -> datetime:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _conv_id(merchant_id: str, customer_id: str | None, trigger_id: str) -> str:
    base = f"conv_{(customer_id or merchant_id)}_{trigger_id}"
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", base)[:120]
    cid, n = base, 1
    while cid in conversations:
        n += 1
        cid = f"{base}_{n}"
    return cid


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = _parse_now(body.get("now"))
    trigger_ids = body.get("available_triggers") or []

    # 1. select candidates
    candidates = []
    for tid in trigger_ids:
        trg = get("trigger", tid)
        if not trg:
            continue
        category, merchant, customer = _bundle(trg)
        if not (category and merchant):
            continue
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        skey = trg.get("suppression_key") or f"{trg.get('kind')}:{mid}:{trg.get('customer_id')}"
        if skey in sent_suppression_keys or _expired(trg, now):
            continue
        if mid in suppressed_merchants and trg.get("scope") != "customer":
            continue
        if trg.get("customer_id") and not customer:
            continue
        candidates.append((tid, trg, category, merchant, customer, skey))

    # 2. prioritise: urgency desc; one merchant-facing action per merchant per tick
    candidates.sort(key=lambda c: -(c[1].get("urgency") or 0))
    chosen, seen = [], set()
    for c in candidates:
        tid, trg, _cat, merchant, customer, _skey = c
        k = (merchant.get("merchant_id"), trg.get("customer_id"))
        if k in seen:
            continue
        seen.add(k)
        chosen.append(c)
        if len(chosen) >= MAX_ACTIONS_PER_TICK:
            break

    # 3. compose (cache hit or live call, bounded by the tick budget)
    async def build(c):
        tid, trg, category, merchant, customer, _ = c
        cached = precomputed.get(tid)
        if cached and cached["sig"] == _signature(tid, trg):
            return cached["msg"]
        async with _sem:
            return await compose_async(category, merchant, trg, customer, timeout=TICK_BUDGET_S - 1, allow_retry=False)

    tasks = [asyncio.create_task(build(c)) for c in chosen]
    if tasks:
        await asyncio.wait(tasks, timeout=TICK_BUDGET_S)

    actions = []
    for c, t in zip(chosen, tasks):
        tid, trg, category, merchant, customer, skey = c
        if t.done() and not t.exception():
            msg = t.result()
        else:
            t.cancel()
            fb = fallback_compose(category, merchant, trg, customer)
            from .composer import _finalize
            msg = _finalize({**fb, "rationale": "[template fallback: time budget] " + fb["rationale"]},
                            category, merchant, trg, customer)
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        cust_id = trg.get("customer_id")
        conv_id = _conv_id(mid, cust_id, tid)
        conversations[conv_id] = {
            "merchant_id": mid, "customer_id": cust_id, "trigger_id": tid, "trigger_kind": trg.get("kind"),
            "role": "customer" if cust_id else "merchant",
            "topic": str((trg.get("payload") or {}).get("intent_topic") or trg.get("kind") or ""),
            "turns": [{"from": "vera" if not cust_id else "merchant_team", "body": msg["body"]}],
            "sent_bodies": {msg["body"]}, "auto_count": 0, "status": "open",
        }
        sent_suppression_keys.add(skey)
        actions.append({
            "conversation_id": conv_id, "merchant_id": mid, "customer_id": cust_id,
            "send_as": msg["send_as"], "trigger_id": tid,
            "template_name": msg["template_name"], "template_params": msg["template_params"],
            "body": msg["body"], "cta": msg["cta"], "suppression_key": msg["suppression_key"] or skey,
            "rationale": msg["rationale"],
        })
    if actions:
        mark_dirty()
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    conv_id = body.get("conversation_id") or f"conv_adhoc_{int(time.time())}"
    mid = body.get("merchant_id")
    cust_id = body.get("customer_id")
    role = body.get("from_role") or "merchant"
    message = body.get("message") or ""

    conv = conversations.get(conv_id)
    if conv is None:  # conversation we didn't start (or after restart) — adopt it
        conv = conversations[conv_id] = {
            "merchant_id": mid, "customer_id": cust_id, "trigger_id": None, "trigger_kind": None, "role": role,
            "topic": "", "turns": [], "sent_bodies": set(), "auto_count": 0, "status": "open"}
    mid = conv.get("merchant_id") or mid

    if conv["status"] == "ended":
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    prior_inbound = [t["body"] for t in conv["turns"] if t["from"] in ("merchant", "customer")]
    conv["turns"].append({"from": role, "body": message})
    mark_dirty()
    kind = classify(message, prior_inbound, role)

    # --- auto-reply handling: tracked per merchant (auto-replies can arrive across conversations) ---
    if kind == "auto_reply":
        key = mid or conv_id
        merchant_auto_reply_count[key] = merchant_auto_reply_count.get(key, 0) + 1
        n = merchant_auto_reply_count[key]
        if n >= 3:
            conv["status"] = "ended"
            suppressed_merchants[key] = "auto_reply_loop"
            return {"action": "end", "rationale": f"Auto-reply received {n}x with no human response; closing to avoid wasting turns."}
        if n == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same WhatsApp Business auto-reply again — owner isn't at the phone. Backing off 24h."}
        mode = "auto_reply_nudge"
    else:
        merchant_auto_reply_count[mid or conv_id] = 0
        mode = kind

    if kind == "opt_out":
        conv["status"] = "ended"
        if mid:
            suppressed_merchants[mid] = "opt_out"
        return {"action": "end", "rationale": "Merchant asked to stop / not interested; exiting gracefully and suppressing further outreach."}
    if kind == "later":
        wait = 86400 if re.search(r"tomorrow|\bkal\b|next week", message, re.I) else 3600
        return {"action": "wait", "wait_seconds": wait, "rationale": f"Merchant asked for time; backing off {wait // 3600}h."}
    if kind == "empty":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Empty inbound; waiting."}

    # Too many unanswered/low-value turns -> exit gracefully
    vera_turns = sum(1 for t in conv["turns"] if t["from"] in ("vera", "merchant_team"))
    if vera_turns >= 6:
        conv["status"] = "ended"
        return {"action": "end", "rationale": "Conversation has run its course; closing politely rather than over-messaging."}

    ctx = {
        "merchant": get("merchant", mid),
        "category": get("category", (get("merchant", mid) or {}).get("category_slug")),
        "trigger": get("trigger", conv.get("trigger_id")),
        "customer": get("customer", conv.get("customer_id") or cust_id),
    }
    try:
        out = await asyncio.wait_for(compose_reply(mode, conv, message, ctx, timeout=REPLY_BUDGET_S - 1),
                                     timeout=REPLY_BUDGET_S)
    except Exception:
        from .replies import fallback_reply
        out = fallback_reply(mode, conv, ctx["merchant"] or {}, ctx["category"] or {}, ctx["trigger"] or {}, "English")

    # anti-repetition
    if out["body"] in conv["sent_bodies"]:
        out["body"] = out["body"].rstrip(".") + " — whenever suits you."
    conv["sent_bodies"].add(out["body"])
    conv["turns"].append({"from": "vera" if role == "merchant" else "merchant_team", "body": out["body"]})
    if mode == "abuse":
        conv["status"] = "ended"
    return {"action": "send", "body": out["body"], "cta": out["cta"], "rationale": out["rationale"]}


@app.post("/v1/teardown")
async def teardown():
    for t in list(_bg_tasks):
        t.cancel()
    contexts.clear(); conversations.clear(); sent_suppression_keys.clear()
    suppressed_merchants.clear(); merchant_auto_reply_count.clear(); precomputed.clear()
    _dirty["v"] = False
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    return {"status": "wiped"}
