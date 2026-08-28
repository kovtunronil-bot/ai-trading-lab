"""
upgrade.py — Self-improvement engine. Runs after each bot cycle.
Analyzes closed trades, learns from wins/losses, auto-adjusts failing strategies.
"""
import brain
import sqlite3
from datetime import datetime
import learn


def analyze_closed_trades():
    conn = brain.init_db()
    rows = conn.execute("""SELECT tp.ts, tp.symbol, tp.strategy, tp.pnl_pct
                           FROM trade_pnl tp
                           LEFT JOIN lessons l ON l.text LIKE '%' || tp.symbol || '%'
                           WHERE l.id IS NULL OR l.text NOT LIKE '%ANALYZED%'
                           ORDER BY tp.ts DESC LIMIT 20""").fetchall()
    conn.close()
    lessons = []
    for ts, symbol, strategy, pnl_pct in rows:
        result = brain.analyze_trade_outcome(symbol, strategy, None, None, "unknown")
        if result and result.get("lesson"):
            lessons.append(result["lesson"])
    return lessons


def auto_adjust_losing():
    adjustments = []
    conn = brain.init_db()
    rows = conn.execute("""SELECT symbol, strategy, COUNT(*) as n, AVG(pnl_pct) as avg_pnl,
                           SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins
                           FROM trade_pnl GROUP BY symbol, strategy
                           HAVING COUNT(*) >= 3""").fetchall()
    conn.close()
    for symbol, strategy, n, avg_pnl, wins in rows:
        wr = wins / n if n > 0 else 0
        if wr < 0.33 and avg_pnl < 0:
            new_label, reason = brain.auto_adjust_strategy(symbol)
            if new_label:
                adjustments.append(f"{symbol}: {strategy} -> {new_label} ({reason})")
    return adjustments


def strategy_report():
    perf = brain.strategy_performance()
    if not perf:
        return []
    report = []
    for strat, data in sorted(perf.items(), key=lambda x: x[1]["avg_pnl"], reverse=True):
        report.append(f"  {strat:30s} | {data['n']:2d} trades | {data['avg_pnl']:+.1f}% avg | {data['win_rate']:.0%} win | grade {data['grade']}")
    return report


def run_upgrade_cycle():
    print(f"\n{'='*50}")
    print(f"UPGRADE CYCLE — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*50}")

    learn.run_learn_cycle()

    lessons = analyze_closed_trades()
    adjustments = auto_adjust_losing()
    stale = [s for s in brain.ALL if brain.config_is_stale(brain.load_config(s))]
    if stale:
        print(f"  evolving {len(stale)} stale configs")
        brain.evolve_all(stale)

    report = []
    if lessons:
        report.append("TRADE ANALYSIS:")
        for l in lessons:
            report.append(f"  {l}")
    if adjustments:
        report.append("AUTO-ADJUSTMENTS:")
        for a in adjustments:
            report.append(f"  {a}")
    strat_report = strategy_report()
    if strat_report:
        report.append("STRATEGY SCOREBOARD:")
        report.extend(strat_report)
    if stale:
        report.append(f"EVOLVED: {len(stale)} stale configs")
    if not report:
        report.append("No upgrades needed — system healthy")

    summary = "\n".join(report)
    print(summary)
    brain.log_journal("ALL", 0.0, "UPGRADE", summary[:100], 0.0)

    if adjustments:
        brain.send_alert(f"BRAIN AUTO-ADJUSTED: {'; '.join(adjustments[:3])}")

    return summary


if __name__ == "__main__":
    run_upgrade_cycle()
