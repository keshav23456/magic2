"""End-to-end smoke test. Works against localhost or your Render URL.

    python smoke_test.py                                  # http://localhost:8080
    python smoke_test.py https://vera-bot.onrender.com    # deployed

Pushes the seed dataset, runs a tick, and exercises every reply path. Prints PASS/FAIL per check.
It wipes its own test data at the end via /v1/teardown (pass --keep to skip). Run it BEFORE the judge's test window, never during.
"""
import glob
import json
import sys
import time
import urllib.error
import urllib.request

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
BASE = (ARGS[0] if ARGS else "http://localhost:8080").rstrip("/")
results = []


def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t


def check(name, ok, info=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {info}" if info else ""))


def push(scope, cid, payload, v=1):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                        "delivered_at": "2026-04-26T10:00:00Z"})


s, b, _ = call("GET", "/v1/healthz")
check("healthz", s == 200 and b.get("status") == "ok", str(b))
s, b, _ = call("GET", "/v1/metadata")
check("metadata", s == 200 and b.get("team_name"), f"{b.get('team_name')} / {b.get('model')}")

for f in glob.glob("dataset/categories/*.json"):
    c = json.load(open(f)); push("category", c["slug"], c)
merchants = json.load(open("dataset/merchants_seed.json"))["merchants"]
for m in merchants:
    push("merchant", m["merchant_id"], m)
for c in json.load(open("dataset/customers_seed.json"))["customers"]:
    push("customer", c["customer_id"], c)
triggers = json.load(open("dataset/triggers_seed.json"))["triggers"]
for t in triggers:
    push("trigger", t["id"], t)
s, b, _ = call("GET", "/v1/healthz")
check("contexts loaded", b["contexts_loaded"]["merchant"] >= 10 and b["contexts_loaded"]["trigger"] >= 25, str(b["contexts_loaded"]))

m0 = merchants[0]
s, b, _ = push("merchant", m0["merchant_id"], m0, v=1)
check("idempotent re-push", s == 200 and b.get("accepted"))
push("merchant", m0["merchant_id"], m0, v=5)
s, b, _ = push("merchant", m0["merchant_id"], m0, v=4)
check("stale version -> 409", s == 409 and b.get("current_version") == 5)
s, b, _ = call("POST", "/v1/context", {"scope": "nope", "context_id": "x", "version": 1, "payload": {}})
check("bad scope -> 400", s == 400)

s, b, _ = call("GET", "/v1/debug/llm", timeout=40)
check("DeepInfra reachable from server", b.get("ok"), str(b))
print("\nWaiting 45s so background precompute can warm the cache...")
time.sleep(45)
s, b, dt = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"] for t in triggers]})
acts = b.get("actions", [])
req = {"conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale"}
check("tick returns actions", s == 200 and len(acts) > 0, f"{len(acts)} actions in {dt:.1f}s")
check("tick under 25s (judge limit 30s)", dt < 25, f"{dt:.1f}s")
check("action schema", all(req <= set(a) and a["body"] for a in acts))
fallbacks = sum(1 for a in acts if a["rationale"].startswith("[template fallback"))
check("LLM used (not template fallback)", fallbacks == 0, f"{fallbacks}/{len(acts)} were template fallback")
print("\n--- sample messages ---")
for a in acts[:8]:
    print(f"\n[{a['send_as']}] {a['trigger_id']}\n  {a['body']}\n  cta={a['cta']} | {a['rationale'][:120]}")
print()

conv = acts[0]["conversation_id"] if acts else "conv_x"
mid = acts[0]["merchant_id"] if acts else m0["merchant_id"]


def rep(cid, msg, turn=2, m=mid):
    return call("POST", "/v1/reply", {"conversation_id": cid, "merchant_id": m, "customer_id": None, "from_role": "merchant",
                                      "message": msg, "received_at": "2026-04-26T10:40:00Z", "turn_number": turn})


s, b, dt = rep(conv, "Interesting, what would the post say?")
check("engaged reply", b.get("action") == "send" and b.get("body"), f"{dt:.1f}s: {str(b.get('body'))[:100]}")
check("engaged reply written by LLM", not str(b.get("rationale", "")).startswith("[template fallback"), str(b.get("rationale"))[:80])
s, b, _ = rep(conv, "Ok lets do it. Whats next?", 3)
body = (b.get("body") or "").lower()
check("commit -> action mode", b.get("action") == "send" and not any(q in body for q in ["would you", "do you", "can you tell"]),
      str(b.get("body"))[:100])
s, b, _ = rep("conv_gst", "can you also help me file my GST?", 2, merchants[1]["merchant_id"])
check("off-topic redirect", b.get("action") == "send", str(b.get("body"))[:100])
m_ar = merchants[2]["merchant_id"]
acts_seen = [rep(f"conv_ar_{i}", "Thank you for contacting us! Our team will respond shortly.", i + 1, m_ar)[1].get("action") for i in range(4)]
check("auto-reply -> send, wait, end", acts_seen[:3] == ["send", "wait", "end"], str(acts_seen))
s, b, _ = rep("conv_hostile", "Stop messaging me. This is useless spam.", 2, merchants[3]["merchant_id"])
check("opt-out -> end", b.get("action") == "end")
s, b, _ = rep("conv_later", "busy right now, message me tomorrow", 2, merchants[4]["merchant_id"])
check("later -> wait", b.get("action") == "wait", str(b.get("wait_seconds")))

if "--keep" not in sys.argv:
    s, b, _ = call("POST", "/v1/teardown")
    check("teardown (clears smoke-test data)", s == 200 and b.get("status") == "wiped")
print(f"\n{sum(results)}/{len(results)} checks passed")
