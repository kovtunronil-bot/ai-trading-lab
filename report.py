import subprocess
import sys

import brain

import brain

journal_lines = brain.journal_tail(40)
try:
    with open(brain.LESSONS_FILE, encoding="utf-8") as f:
        lessons = f.read().strip()
except FileNotFoundError:
    lessons = "no lessons yet"

stats = (
    f"Strategy: multi-market portfolio (14 markets)\n"
    f"Recent journal entries:\n" + "\n".join(journal_lines) + "\n\n"
    f"Evolution lessons:\n{lessons}"
)

prompt = (
    "ROLE: You are a brutal, elite quantitative portfolio reviewer at a top hedge fund. "
    "You have never complimented anyone in your career. Your job is to find weaknesses.\n\n"
    "Below is the trade journal and evolution history of a retail paper-trading bot (daily bars, "
    "long-only, 14 markets incl BTC/ETH). Analyze it with zero mercy:\n\n"
    "MANDATORY CHECKS:\n"
    "1. BETA vs ALPHA: Is this bot just riding market beta? If buy&hold beat it, say exactly that and quantify.\n"
    "2. LOGICAL FALLACIES in its strategy choices: survivorship of lucky parameters, regime-mismatched champions, "
    "overfitting signs (too many trades? too few?), risk-blind sizing.\n"
    "3. BEHAVIORAL PATHOLOGIES: revenge-style re-entries, holding losers too long, missing exits, whipsaw churn.\n"
    "4. REGIME AWARENESS: which regimes does it clearly fail in?\n\n"
    "OUTPUT FORMAT (max 220 words):\n"
    "VERDICT: <one brutal sentence>\n"
    "FALLACIES FOUND: <numbered list, cite specific evidence from journal>\n"
    "WHAT WOULD GET IT FIRED: <the single worst behavior>\n"
    "ONE MATHEMATICALLY RIGOROUS FIX: <concrete rule with numbers>\n\n"
    "Do not encourage. Do not soften. Treat it as a junior whose job depends on this review.\n\n"
    + stats
)

model = None
for m in ["llama3.1", "llama3.2"]:
    r = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if m.split(":")[0] in r.stdout.lower() or m in r.stdout:
        model = m
        break
if model is None:
    print("Ollama model not found. Run: ollama pull llama3.1")
    sys.exit(1)

print(f"Asking {model} to analyze the robot's diary...\n" + "=" * 52)
r = subprocess.run(["ollama", "run", model, prompt], capture_output=True, text=True, encoding="utf-8")
if r.returncode != 0:
    print("Ollama error:", r.stderr[:300])
    sys.exit(1)
print(r.stdout.strip())

propose_prompt = (
    "You are a quantitative researcher hunting for alpha in daily-bar, long-only strategies. "
    "Here is what has been tried recently and how it scored:\n"
    + "\n".join(l for l in lessons.splitlines()[-10:])
    + "\n\nInvent 5 NEW parameter combinations that address the weaknesses above "
    "(different regime filters, tighter/looser thresholds, volume confirmation). "
    "Reply ONLY with JSON lines, each like one of these examples:\n"
    '{"mode": "rsi", "period": 2, "buy_below": 8, "sell_above": 62}\n'
    '{"mode": "rsi", "period": 5, "buy_below": 20, "sell_above": 65, "trend": 150}\n'
    '{"mode": "donchian", "entry": 25, "exit": 12, "vol_mult": 1.3}\n'
    '{"mode": "atr_breakout", "entry": 25, "exit": 12, "mult": 0.75}\n'
    "No other text."
)
r2 = subprocess.run(["ollama", "run", model, propose_prompt], capture_output=True, text=True, encoding="utf-8")
import json as _json
proposals = []
for line in (r2.stdout or "").splitlines():
    line = line.strip().strip("`")
    if not line.startswith("{"):
        continue
    try:
        proposals.append(_json.loads(line))
    except _json.JSONDecodeError:
        pass
with open("proposals.json", "w", encoding="utf-8") as f:
    _json.dump(proposals[:5], f, indent=2)
print("\n" + "=" * 52)
print(f"AI proposed {len(proposals[:5])} new strategy ideas -> brain will test them at next evolution")

with open("report_latest.md", "w", encoding="utf-8") as f:
    f.write(r.stdout.strip())

import brain
brain.send_alert("Sunday AI report: " + r.stdout.strip().replace("\n", " ")[:150])

from datetime import datetime
with open("reports_history.md", "a", encoding="utf-8") as f:
    f.write(f"\n\n---\n# AI Report {datetime.now():%Y-%m-%d %H:%M}\n\n" + r.stdout.strip())
print("\n" + "=" * 52)
print("Saved to report_latest.md + reports_history.md | headline sent to phone")
