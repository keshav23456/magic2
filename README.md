# Vera bot — magicpin AI Challenge (Keshav Soni)

## Approach
1. **Fact sheet.** Each message starts from a compact fact sheet built from the four contexts (category, merchant, trigger, customer). Digest items referenced by the trigger (`top_item_id`, `digest_item_id`, `alert_id`) are resolved in the category digest, so citations come straight from the data.
2. **Routing.** Each of about 30 trigger kinds has its own playbook: the angle to take, which engagement levers to use, and the default CTA. For example, `research_digest` cites the source, `seasonal_perf_dip` reassures before suggesting a move, and `active_planning_intent` delivers a draft instead of asking more questions.
3. **LLM.** Llama-3.3-70B-Instruct-Turbo on DeepInfra, run with temperature 0, a fixed seed and JSON output. The system prompt enforces the salutation, why-now, one verifiable anchor, service+price offers, and a single CTA placed last.
4. **Validator.** Every draft is checked for:
   - numbers that can't be traced back to the contexts (anti-fabrication)
   - taboo vocabulary
   - URLs
   - "% off" offers

   A draft that fails gets one corrective retry. If that also fails, or the LLM is down, a deterministic template composer takes over.
5. **Latency.** Compositions are precomputed in the background when context is pushed, and the cache is invalidated when versions change. `/v1/tick` therefore usually serves from cache. It also has a hard time budget with a template fallback, so it never times out.
6. **Replies.** A rule layer decides send, wait or end; the LLM only writes the text of a send.
   - **Auto-reply:** tracked per merchant. First time: one nudge. Second: wait 24h. Third: end.
   - **Opt-out:** end and suppress the merchant.
   - **Commitment:** switch to action mode. A guardrail rejects any qualifying question.
   - **Off-topic** (e.g. GST): polite redirect back to the pending item.
   - **"Later":** back off.
   - **Language:** detected on every turn.
7. **Tick policy.**
   - Suppression-key dedup, and expired triggers are skipped.
   - At most one action per merchant per tick, ordered by urgency.
   - Opted-out merchants are skipped.

## Tradeoffs
- State lives in memory, so there must be a single worker and no restart during the test. That keeps things simple and fast, but it is not durable.
- Rules handle the high-stakes reply decisions for predictability; nuance is left to the LLM.
- Regional-language mixes (Telugu, Kannada, Tamil) are kept light, English-based, to avoid low-quality output.

## What would have helped
Real merchant reply logs (to tune the auto-reply and intent patterns), slot availability for more merchants, and per-merchant post and photo history.

## Run locally
```bash
pip install -r requirements.txt
# put your key in .env → DEEPINFRA_API_KEY=...
uvicorn app.main:app --port 8080 --workers 1
python judge_simulator.py            # scenarios; set LLM_API_KEY in the file only if you want LLM scoring
python generate_submission.py        # writes submission.jsonl for the 30 test pairs
```

## Deploy on Render
1. Push this folder to a GitHub repo. `.env` is git-ignored, so the key stays local.
2. In Render, go to **New → Blueprint**, select the repo, and it will pick up `render.yaml`. Alternatively, create a Web Service with:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
3. Under **Environment**, set `DEEPINFRA_API_KEY`.
4. Check that `https://<your-app>.onrender.com/v1/healthz` returns `ok`. Submit that base URL, without `/v1`, on the portal.

**Free plan: how the two risks are handled**
- **Sleep after 15 minutes idle:** the bot pings its own `RENDER_EXTERNAL_URL/v1/healthz` every 10 minutes. Render sets that variable automatically, so there's nothing to configure. One always-on service fits in the 750 free hours per month.
- **Restart wipes memory:** state is snapshotted to `/tmp/vera_state.json` every 15 seconds and restored on startup. This survives a process restart, but not an instance replacement such as a redeploy, because Render's free disk is ephemeral. So don't redeploy during the test window. External storage isn't used because the challenge forbids sending merchant data to non-LLM services.
- **Clearing test data:** use `POST /v1/teardown`. A restart would reload the snapshot. `smoke_test.py` calls teardown automatically at the end.
