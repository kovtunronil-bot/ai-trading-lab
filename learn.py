"""
learn.py — The self-learning engine.
Analyzes every trade, records outcomes, tracks strategy performance,
and makes the bot smarter after every cycle.
"""
import brain
import sqlite3
from datetime import datetime


def learn_from_open_trades():
    """Record current open trades so we have strategy labels for when they close."""
    from alpaca.trading.client import TradingClient
    API_KEY, SECRET_KEY = brain.api_creds() or (None, None)
    try:
        client = TradingClient(API_KEY, SECRET_KEY, paper=True)
        positions = list(client.get_all_positions())
        updated = 0
        for p in positions:
            sym = p.symbol
            internal = brain.internal_sym(sym)
            label = brain.get_position_strategy(internal) or brain.get_position_strategy(sym)
            if not label:
                cfg = brain.load_config(internal)
                label = cfg.get("label", "unknown") if cfg else "unknown"
                brain.set_position_strategy(internal, label)
            updated += 1
        return updated
    except Exception as e:
        print(f"  learn_from_open: {e}")
        return 0


def learn_from_closed_trades():
    """Analyze every closed trade and record the lesson."""
    conn = brain.init_db()
    rows = conn.execute("""
        SELECT tp.ts, tp.symbol, tp.strategy, tp.pnl_pct
        FROM trade_pnl tp
        ORDER BY tp.ts DESC
    """).fetchall()
    conn.close()

    if not rows:
        return []

    lessons = []
    for ts, symbol, strategy, pnl_pct in rows:
        if pnl_pct > 3:
            lessons.append(f"WIN: {symbol} {strategy} gained {pnl_pct:+.1f}% — repeat this pattern")
        elif pnl_pct < -3:
            lessons.append(f"LOSS: {symbol} {strategy} lost {pnl_pct:+.1f}% — avoid this setup")

    conn = brain.init_db()
    for l in lessons:
        existing = conn.execute("SELECT COUNT(*) FROM lessons WHERE text=?", (l,)).fetchone()[0]
        if existing == 0:
            conn.execute("INSERT INTO lessons(ts,text) VALUES(?,?)",
                        (datetime.now().isoformat(timespec="seconds"), l))
    conn.commit()
    conn.close()
    return lessons


def track_regime_performance():
    """Record which strategies work in which regimes."""
    import yfinance as yf

    conn = brain.init_db()
    rows = conn.execute("SELECT symbol, strategy, pnl_pct FROM trade_pnl").fetchall()
    conn.close()
    if not rows:
        return 0

    updated = 0
    for symbol, strategy, pnl_pct in rows:
        try:
            df = yf.Ticker(brain.yf_symbol(symbol)).history(period="6mo")
            if df.empty:
                continue
            regime = brain.current_regime(df)
            conn2 = brain.init_db()
            existing = conn2.execute(
                "SELECT avg_pnl, n_trades FROM regime_strategy_perf WHERE symbol=? AND strategy=? AND regime=?",
                (symbol, strategy, regime)).fetchone()
            if existing:
                old_avg, old_n = existing
                new_n = old_n + 1
                new_avg = (old_avg * old_n + pnl_pct) / new_n
                conn2.execute("UPDATE regime_strategy_perf SET avg_pnl=?, n_trades=? WHERE symbol=? AND strategy=? AND regime=?",
                           (new_avg, new_n, symbol, strategy, regime))
            else:
                conn2.execute("INSERT INTO regime_strategy_perf(symbol,regime,strategy,avg_pnl,n_trades) VALUES(?,?,?,?,?)",
                           (symbol, regime, strategy, pnl_pct, 1))
            conn2.commit()
            conn2.close()
            updated += 1
        except Exception:
            pass

    return updated


def auto_switch_losers():
    """If a strategy has 3+ losing trades, switch to a better one."""
    conn = brain.init_db()
    rows = conn.execute("""
        SELECT symbol, strategy, COUNT(*) as n, AVG(pnl_pct) as avg_pnl,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins
        FROM trade_pnl GROUP BY symbol, strategy
        HAVING COUNT(*) >= 3
    """).fetchall()
    conn.close()

    switches = []
    for symbol, strategy, n, avg_pnl, wins in rows:
        win_rate = wins / n if n > 0 else 0
        if win_rate < 0.33 and avg_pnl < 0:
            new_label, reason = brain.auto_adjust_strategy(symbol)
            if new_label:
                switches.append(f"{symbol}: {strategy} ({win_rate:.0%} wr, {avg_pnl:+.1f}%) -> {new_label}")
    return switches


def update_kelly_from_trades():
    """Rebuild Kelly data from scratch from trade_pnl so it is idempotent
    (running every cycle does NOT keep double-counting accumulated trades)."""
    conn = brain.init_db()
    rows = conn.execute("SELECT symbol, pnl_pct FROM trade_pnl ORDER BY ts").fetchall()
    # rebuild deterministically from the full history
    conn.execute("DELETE FROM kelly_data")
    conn.commit()
    conn.close()
    for symbol, pnl_pct in rows:
        brain.update_kelly_data(symbol, pnl_pct)
    return len(rows)


def run_learn_cycle():
    print(f"\n{'='*50}")
    print(f"LEARN CYCLE — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*50}")

    open_count = learn_from_open_trades()
    print(f"  Tracked {open_count} open positions")

    lessons = learn_from_closed_trades()
    if lessons:
        print(f"  Lessons from trades:")
        for l in lessons:
            print(f"    {l}")

    regime_count = track_regime_performance()
    print(f"  Updated {regime_count} regime-strategy records")

    kelly_count = update_kelly_from_trades()
    print(f"  Updated Kelly data from {kelly_count} trades")

    switches = auto_switch_losers()
    if switches:
        print(f"  AUTO-SWITCH:")
        for s in switches:
            print(f"    {s}")
        brain.send_alert(f"BRAIN LEARNED: {'; '.join(switches[:3])}")

    print(f"\n  Strategy scoreboard:")
    perf = brain.strategy_performance()
    for strat, data in sorted(perf.items(), key=lambda x: x[1]["avg_pnl"], reverse=True):
        emoji = "WIN" if data["avg_pnl"] > 0 else "LOSE"
        print(f"    {emoji} {strat:25s} | {data['n']} trades | {data['avg_pnl']:+.1f}% avg | {data['win_rate']:.0%} win | grade {data['grade']}")

    return True


if __name__ == "__main__":
    run_learn_cycle()
