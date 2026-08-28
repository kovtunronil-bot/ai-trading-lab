"""
seed_memory.py — Backfill the self-learning memory tables from historical data.

Walks each market's live config strategy through ~3 years of daily prices,
generating simulated round-trip trades. Each simulated exit feeds the SAME
learning functions the live bot uses on real exits:
  - brain.update_kelly_data()        -> kelly_data
  - brain.log_pattern()              -> pattern_memory
  - brain.log_loss_conditions()      -> loss_memory
plus trade_pnl so learn.py can build lessons and auto-switch losing strategies.

Run:  py seed_memory.py
"""
import datetime
import brain

MARKETS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "GLD", "AMD",
           "META", "GOOGL", "AMZN", "NFLX", "BTC/USD", "ETH/USD"]
START = "2022-01-01"


def min_trade_pnl(entry, exit):
    return (exit / entry - 1) * 100 if entry > 0 else 0.0


def run(symbol):
    cfg = brain.load_config(symbol)
    if not cfg:
        print(f"  {symbol}: no config, skipping")
        return 0

    import yfinance as yf
    df = yf.Ticker(brain.yf_symbol(symbol)).history(start=START)
    if df is None or df.empty or len(df) < 100:
        print(f"  {symbol}: no data, skipping")
        return 0

    close = df["Close"]
    regimes = brain.detect_regime_series(df)
    pos = brain.position_for(df, cfg, shift=True)

    trades = 0
    entry_i = None
    entry_price = None
    pnl_rows = []

    for i in range(len(df)):
        signal = float(pos.iloc[i])
        price = float(close.iloc[i])
        # enter when we flip from flat to in (and not yet holding)
        if signal > 0.5 and entry_i is None:
            entry_i = i
            entry_price = price
        # exit when we flip to flat while holding
        elif signal < 0.5 and entry_i is not None:
            exit_price = price
            pnl = min_trade_pnl(entry_price, exit_price)
            regime = str(regimes.iloc[i])
            # pattern key captured at the exit bar (rich context)
            pattern = brain.hash_pattern(df.iloc[: i + 1], regime)

            brain.update_kelly_data(symbol, pnl)
            brain.log_pattern(symbol, pattern, pnl, regime)
            if pnl < -3:
                brain.log_loss_conditions(symbol, cfg.get("label", "?"), regime, pnl,
                                          f"backfill exit={exit_price:.2f}")

            # collect for trade_pnl insert (dedup on date so re-runs don't balloon)
            key = (df.index[i].date().isoformat(), symbol)
            if key not in pnl_rows:
                pnl_rows.append((df.index[i].isoformat(timespec="seconds"), symbol,
                                 cfg.get("label", "?"), round(pnl, 4)))

            trades += 1
            entry_i = None

    # Insert trade_pnl on a dedicated connection (init_db is a closed singleton here)
    import sqlite3
    conn = sqlite3.connect(brain.DB_FILE, timeout=10)
    for ts, sym, label, pnl in pnl_rows:
        exists = conn.execute("SELECT COUNT(*) FROM trade_pnl WHERE ts=? AND symbol=?",
                              (ts, sym)).fetchone()[0]
        if exists == 0:
            conn.execute("INSERT INTO trade_pnl(ts,symbol,strategy,pnl_pct) VALUES(?,?,?,?)",
                         (ts, sym, label, pnl))
    conn.commit()
    conn.close()
    print(f"  {symbol}: {trades} simulated trades -> kelly/pattern/loss_memory seeded")
    return trades


def main():
    import sqlite3
    print("=" * 60)
    print(f"SEED MEMORY — {datetime.datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 60)

    conn = sqlite3.connect(brain.DB_FILE, timeout=10)
    n_pnl = conn.execute("SELECT COUNT(*) FROM trade_pnl").fetchone()[0]
    n_pattern = conn.execute("SELECT COUNT(*) FROM pattern_memory").fetchone()[0]
    conn.close()

    # Bootstrap only: if learning already exists (from a prior seed or live trades),
    # do NOT wipe it. Wiping every run would erase real self-improvement on each cycle.
    if n_pattern > 0 and n_pnl > 0:
        print(f"Skipping (already seeded: {n_pattern} patterns, {n_pnl} pnl rows). "
              "Preserving live learning.")
        return

    print("Seeding self-learning memory from historical walk...")

    total = 0
    for sym in MARKETS:
        try:
            total += run(sym)
        except Exception as e:
            print(f"  {sym}: ERROR {e}")
    print(f"\nDone. Seeded {total} simulated trades across {len(MARKETS)} markets.")

    conn = sqlite3.connect(brain.DB_FILE, timeout=10)
    for t in ["pattern_memory", "loss_memory", "kelly_data", "trade_pnl"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n} rows")
    conn.close()


if __name__ == "__main__":
    main()
