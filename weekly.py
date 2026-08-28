import sys
from datetime import datetime, timedelta

import brain


def week_stats(days=7):
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    conn = brain.init_db()
    closed = conn.execute(
        "SELECT COUNT(*), AVG(pnl_pct) FROM trade_pnl WHERE ts >= ?",
        (since,)).fetchone()
    evolutions = conn.execute(
        "SELECT COUNT(*) FROM lessons WHERE text LIKE '%EVOLVE%' AND ts >= ?",
        (since,)).fetchone()[0]
    slippage = conn.execute(
        "SELECT COUNT(*), AVG(slippage_bps) FROM trades WHERE slippage_bps IS NOT NULL AND ts >= ?",
        (since,)).fetchone()
    conn.close()

    eq_df = brain.equity_history_df()
    try:
        eq_now = float(eq_df.iloc[-1]["equity"])
        cutoff = datetime.now() - timedelta(days=days)
        old = eq_df[eq_df["date"].apply(
            lambda d: datetime.fromisoformat(str(d)[:19])) <= cutoff]
        eq_old = float(old.iloc[-1]["equity"]) if len(old) else None
    except Exception:
        eq_now = eq_old = None
    return {
        "closed_n": closed[0] or 0,
        "closed_avg": closed[1],
        "evolutions": evolutions,
        "slip_n": slippage[0] or 0,
        "slip_avg": slippage[1],
        "eq_now": eq_now,
        "eq_old": eq_old,
    }


def main():
    sys.stdout.reconfigure(errors="replace")
    s = week_stats(7)
    ready = brain.readiness_score()
    lines = [f"Weekly progress | דוח שבועי — {datetime.now():%Y-%m-%d}"]

    if s["eq_now"] is not None and s["eq_old"] is not None:
        delta = s["eq_now"] - s["eq_old"]
        pct = delta / s["eq_old"] * 100 if s["eq_old"] else 0
        emoji = "📈" if delta >= 0 else "📉"
        lines.append(f"{emoji} Equity ${s['eq_old']:,.0f} -> ${s['eq_now']:,.0f} ({pct:+.2f}%)")
    lines.append(f"🧠 Brain evolved {s['evolutions']}x this week")
    lines.append(f"📕 Closed trades: {s['closed_n']}"
                 + (f", avg {s['closed_avg']:+.2f}%" if s["closed_avg"] is not None else ""))
    if s["slip_n"]:
        lines.append(f"📏 Slippage: {s['slip_avg']:.1f} bps over {s['slip_n']} fills")

    incidents = brain.incident_stats(7)
    if incidents:
        total = sum(n for _, n in incidents)
        top3 = ", ".join(f"{k}: {n}" for k, n in incidents[:3])
        lines.append(f"🔧 Incidents (7d): {total} total — {top3}")
    else:
        lines.append("🔧 Incidents (7d): 0 — clean week ✅")

    lines.append("")
    lines.append(f"🎯 REAL-MONEY READINESS: {ready['score']}/100")
    for name, pts, detail in ready["checks"]:
        lines.append(f"   {'✅' if pts > 0 else '⬜'} {name}: {detail}")
    verdict = ("🟢 SAFE ZONE - discuss real money" if ready["score"] >= 80 else
               "🟡 getting there - keep paper trading" if ready["score"] >= 50 else
               "🔒 NOT ready - stay on paper")
    lines.append(verdict)
    report = "\n".join(lines)
    print(report)

    with open("progress.md", "a", encoding="utf-8") as f:
        f.write(report + "\n\n" + "-" * 50 + "\n\n")

    quiet = "quiet" in sys.argv
    if not quiet or ready["score"] >= 80:
        brain.send_alert(f"📊 Weekly: {ready['score']}/100 real-money readiness | "
                         f"{'🟢 SAFE ZONE' if ready['score'] >= 80 else 'still paper mode'}")


if __name__ == "__main__":
    main()
