"""Backtest report tool.

Usage:
    py backtest.py SYMBOL                  # rank all strategies on a symbol by Sharpe
    py backtest.py SYMBOL MODE             # report one strategy mode on a symbol
    py backtest.py --all                   # report each symbol's live config strategy
    py backtest.py --list                  # list every registered strategy mode/label

SYMBOL uses internal names: SPY, QQQ, AAPL, ..., BTC/USD, ETH/USD.
MODE is a strategy mode, e.g. smc_fw, heikin_fw, boll_fw, donchian, rsi, ...
"""
import sys
import warnings

import pandas as pd

import brain

warnings.filterwarnings("ignore")


def fetch(symbol, period="4y"):
    return brain.yf.Ticker(brain.yf_symbol(symbol)).history(period=period)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = set(a for a in argv if a.startswith("--"))

    if "--list" in flags:
        for c in brain.candidates():
            print(f"  {c['mode']:<18} {c.get('label','')}")
        return 0

    if "--all" in flags:
        rows = []
        for sym in brain.ALL:
            df = fetch(sym)
            if df.empty:
                print(f"{sym}: no data, skipped"); continue
            cfg = brain.load_config(sym) or {"mode": "hold", "label": "hold / no config"}
            r = brain.backtest_report(df, dict(cfg, symbol=sym))
            rows.append(r)
        rows.sort(key=lambda r: r["sharpe"], reverse=True)
        print(f"\n{'SYMBOL':<14}{'MODE':<22}{'SHARPE':>7}{'TOTRET':>10}{'PF':>6}"
              f"{'MAXDD':>8}  STRATEGY")
        for r in rows:
            print(f"{r['symbol']:<14}{r['mode']:<22}{r['sharpe']:>7.2f}"
                  f"{r['total_return']*100:>9.1f}%{r['profit_factor']:>6.2f}"
                  f"{r['max_dd']*100:>7.1f}%  {r['label']}")
        return 0

    if len(args) == 1:
        sym = args[0]
        df = fetch(sym)
        if df.empty:
            print(f"No data for {sym}"); return 1
        cands = brain.candidates()
        rows = []
        for c in cands:
            r = brain.backtest_report(df, dict(c, symbol=sym))
            rows.append(r)
        rows.sort(key=lambda r: r["sharpe"], reverse=True)
        print(f"\n=== {sym}: all strategies ranked by Sharpe ===")
        print(f"{'SHARPE':>7}{'SORTINO':>8}{'TOTRET':>10}{'PF':>6}{'MAXDD':>8}"
              f"{'TR':>5}{'W%':>5}  STRATEGY (mode)")
        for r in rows[:25]:
            st = r["strategy"]
            tr = f"{st['trades']}" if st.get("expectancy_r") is not None else "-"
            w = f"{st['win_rate']*100:.0f}" if st.get("win_rate") is not None else "-"
            print(f"{r['sharpe']:>7.2f}{r['sortino']:>8.2f}{r['total_return']*100:>9.1f}%"
                  f"{r['profit_factor']:>6.2f}{r['max_dd']*100:>7.1f}%{tr:>5}{w:>5}  "
                  f"{r['label']} ({r['mode']})")
        return 0

    if len(args) == 2:
        sym, mode = args[0], args[1]
        df = fetch(sym)
        if df.empty:
            print(f"No data for {sym}"); return 1
        cfg = None
        for c in brain.candidates():
            if c["mode"] == mode:
                cfg = dict(c, symbol=sym)
                break
        if cfg is None:
            base = brain.load_config(sym) or {"mode": "hold"}
            cfg = dict(base, mode=mode)
        r = brain.backtest_report(df, cfg)
        brain.print_backtest_report(r)
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
