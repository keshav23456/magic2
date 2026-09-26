"""Builds submission.jsonl for the 30 canonical test pairs.

Usage:  python generate_submission.py
Needs DEEPINFRA_API_KEY in .env (without it you get template-fallback messages).
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from app.composer import compose_async  # noqa: E402

ROOT = Path(__file__).parent
EXP = ROOT / "expanded"


def load_dir(d: Path, key: str) -> dict:
    out = {}
    for f in sorted(d.glob("*.json")):
        obj = json.loads(f.read_text())
        out[obj.get(key, f.stem)] = obj
    return out


async def main():
    if not (EXP / "test_pairs.json").exists():
        subprocess.run([sys.executable, "generate_dataset.py", "--seed-dir", ".", "--out", str(EXP.resolve())],
                       cwd=ROOT / "dataset", check=True)
    cats = load_dir(EXP / "categories", "slug")
    merchants = load_dir(EXP / "merchants", "merchant_id")
    customers = load_dir(EXP / "customers", "customer_id")
    triggers = load_dir(EXP / "triggers", "id")
    tp = json.loads((EXP / "test_pairs.json").read_text())
    pairs = tp["pairs"] if isinstance(tp, dict) else tp

    sem = asyncio.Semaphore(6)

    async def one(p):
        trg = triggers[p["trigger_id"]]
        m = merchants[p.get("merchant_id") or trg["merchant_id"]]
        c = customers.get(p.get("customer_id") or trg.get("customer_id") or "")
        async with sem:
            msg = await compose_async(cats[m["category_slug"]], m, trg, c, timeout=25.0, allow_retry=True)
        print(f"{p['test_id']}: {msg['body'][:90]}...")
        return {"test_id": p["test_id"], **{k: msg[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}}

    rows = await asyncio.gather(*(one(p) for p in pairs))
    with open(ROOT / "submission.jsonl", "w", encoding="utf-8") as f:
        for r in sorted(rows, key=lambda r: r["test_id"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(rows)} lines to submission.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
