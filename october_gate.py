#!/usr/bin/env python3
"""october_gate.py - strictly READ-ONLY daily October verdict watcher.

Mirrors the [6] gate in weekly_report.py exactly (brain.framework_stats
"clean" cohort + brain.readiness_score). Deliberately does NOT import brain:
brain performs day-rollover WRITES on open (state.json / journal), which would
corrupt the committed lab.db. All logic here is replicated from brain inline
and every connection is opened with PRAGMA query_only=ON, so this script can
never mutate the database.

Exit 0 when the verdict is READY (so a daily workflow can notify only on the
flip), 1 otherwise.
"""
import datetime
import sqlite3
import sys

DB_FILE = "lab.db"

FRAMEWORK_STRATEGIES = (
    "Momentum Flag Pullback",
    "Flag Pattern Fade (Flag FW)",
    "Bank of America (BAC Flag FW)",
    "Johnson & Johnson (JNJ Flag FW)",
)


def conn_ro():
    c = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=10)
    c.execute("PRAGMA query_only=ON")
    return c


def is_framework_strategy(name):
    n = (name or "").upper()
    if not n:
        return False
    if name in FRAMEWORK_STRATEGIES:
        return True
    return "FW" in n or "FLAG" in n or "ORB FW" in n


def framework_clean_stats():
    conn = conn_ro()
    rows = conn.execute("SELECT strategy, tags, pnl_pct FROM trade_pnl").fetchall()
    conn.close()
    clean = []
    for strategy, tags, pnl in rows:
        if not is_framework_strategy(strategy):
            continue
        tokens = [t for t in (tags or "").split(",") if t]
        if not tokens:
            clean.append((strategy, tags, pnl))
    pnls = [float(p) for _, _, p in clean if p is not None]
    return {"n": len(clean), "avg": round(sum(pnls) / len(pnls), 3) if pnls else 0.0}


def readiness_score():
    """Replicates brain.readiness_score() read-only (queries only)."""
    conn = conn_ro()
    checks = []
    first = conn.execute("SELECT MIN(date) FROM equity_history").fetchone()[0]
    try:
        days = (datetime.datetime.now() - datetime.datetime.fromisoformat(first)).days if first else 0
    except ValueError:
        days = 0
    checks.append(("Paper runtime", min(days / 42, 1.0) * 15))

    n_closed = conn.execute("SELECT COUNT(*) FROM trade_pnl").fetchone()[0]
    checks.append(("Closed trades", min(n_closed / 25, 1.0) * 25))

    last10 = conn.execute("""SELECT AVG(pnl_pct) FROM
        (SELECT pnl_pct FROM trade_pnl ORDER BY id DESC LIMIT 10)""").fetchone()[0]
    if last10 is None:
        checks.append(("Recent performance", 0.0))
    elif last10 > 0:
        checks.append(("Recent performance", 15.0))
    elif last10 > -2:
        checks.append(("Recent performance", 7.0))
    else:
        checks.append(("Recent performance", 0.0))

    row = conn.execute("""SELECT MIN(equity/peak), MAX(drawdown_pct)
                          FROM equity_history""").fetchone()
    worst_dd = row[0] - 1 if row and row[0] else 0.0
    cur_eq = conn.execute("SELECT equity FROM equity_history ORDER BY date DESC LIMIT 1").fetchone()
    peak_eq = conn.execute("SELECT MAX(peak) FROM equity_history").fetchone()[0]
    near_peak = bool(cur_eq and peak_eq and float(cur_eq[0]) >= peak_eq * 0.95)
    if worst_dd > -0.12:
        checks.append(("Drawdown resilience", 15.0))
    elif near_peak:
        checks.append(("Drawdown resilience", 10.0))
    else:
        checks.append(("Drawdown resilience", 0.0))

    srow = conn.execute("""SELECT COUNT(*), ABS(AVG(slippage_bps)) FROM trades
                           WHERE slippage_bps IS NOT NULL""").fetchone()
    sn, savg = srow[0], srow[1] or 99
    if sn >= 10 and savg < 5:
        checks.append(("Execution quality", 15.0))
    elif sn >= 10:
        checks.append(("Execution quality", 8.0))
    else:
        checks.append(("Execution quality", 0.0))

    diverse = conn.execute("SELECT COUNT(DISTINCT strategy) FROM trade_pnl").fetchone()[0]
    checks.append(("Strategy variety", min(diverse / 4, 1.0) * 15))
    conn.close()
    return round(min(sum(c[1] for c in checks), 100), 1)


def main():
    conn = conn_ro()
    gates = {}

    days = [r[0] for r in conn.execute(
        "SELECT date FROM equity_history ORDER BY date DESC LIMIT 14").fetchall()]
    prev = None
    gaps = []
    for d in days:
        try:
            t = datetime.datetime.fromisoformat(d)
        except Exception:
            continue
        if prev is not None:
            gaps.append((prev - t).total_seconds() / 3600)
        prev = t
    gates["Stability (no outage 14d)"] = bool(gaps) and max(gaps) <= 60

    row = conn.execute("SELECT drawdown_pct FROM equity_history ORDER BY date DESC LIMIT 1").fetchone()
    cur_dd = float(row[0]) if row and row[0] is not None else None
    gates["Drawdown >= -12%"] = cur_dd is not None and cur_dd >= -12
    last_date = days[0] if days else "?"

    fw = framework_clean_stats()
    fw_n, fw_avg = fw["n"], fw["avg"]
    gates["Framework clean n>=15 avg>=+0.25%"] = bool(fw_n >= 15 and fw_avg >= 0.25)

    score = readiness_score()
    gates["Readiness >= 85/100"] = score >= 85

    print("[OCTOBER GATE] generated", datetime.datetime.now().isoformat(timespec="seconds"))
    for name, ok in gates.items():
        print(f"      [{'PASS' if ok else 'PEND'}]  {name}")
    print(f"      framework: n={fw_n} avg={fw_avg:+.2f}%  readiness={score}/100  "
          f"dd={cur_dd if cur_dd is None else cur_dd:+.1f}%  last_eq={last_date}")
    if all(gates.values()):
        print("      VERDICT: READY")
        return 0
    print("      VERDICT: PENDING")
    return 1


if __name__ == "__main__":
    sys.exit(main())