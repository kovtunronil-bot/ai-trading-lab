"""Weekly honest report for the AI Trading Robot.

Run whenever (e.g. every Sunday) to get a snapshot of equity, live trade
performance, integrity, readiness, and the first framework trades.
Read-only: it never modifies lab.db or configs.
"""
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime

import brain


def main():
    print("=== AI TRADING ROBOT — WEEKLY HONEST REPORT ===")
    print("generated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "\n")

    # 1) Equity today
    try:
        df = brain.equity_history_df()
        if not df.empty:
            eq = float(df["equity"].iloc[-1])
            dd = float(df["drawdown_pct"].iloc[-1])
            peak = float(df["peak"].iloc[-1])
            print(f"[1] EQUITY      ${eq:,.2f}")
            print(f"    PEAK        ${peak:,.2f}")
            print(f"    DRAWDOWN    {dd:+.1f}%")
        else:
            print("[1] EQUITY      no equity history yet")
    except Exception as e:
        print("[1] EQUITY      ERR", e)
    print()

    # 2) Closed-trade performance (all + per framework strategy)
    try:
        conn = brain.init_db()
        tot = conn.execute(
            "SELECT COUNT(*), ROUND(SUM(pnl_pct),2) FROM trade_pnl").fetchone()
        rows = conn.execute(
            "SELECT strategy, COUNT(*), ROUND(AVG(pnl_pct),2), "
            "ROUND(SUM(pnl_pct),2) FROM trade_pnl "
            "GROUP BY strategy ORDER BY SUM(pnl_pct) DESC").fetchall()
        conn.close()
        print(f"[2] CLOSED TRADES  {tot[0]}   cum P&L {tot[1]:+.2f}%")
        print("    per strategy:")
        for s, n, a, ssum in rows:
            flag = "" if (a or 0) >= 0 else "   <-- losing"
            print(f"      {s:<26} n={n:>4}  avg={a:+.2f}%  sum={ssum:+.2f}%{flag}")
    except Exception as e:
        print("[2] TRADES      ERR", e)
    print()

    # 3) Integrity: compile + configs
    import glob
    import py_compile
    bad = []
    for f in glob.glob("*.py"):
        try:
            py_compile.compile(f, doraise=True)
        except Exception as ex:
            bad.append(f"{f}: {ex}")
    print(f"[3] INTEGRITY  compile: {'OK' if not bad else bad}")

    import json
    cfgs = []
    for f in glob.glob("config_*.json"):
        try:
            json.load(open(f, encoding="utf-8"))
            cfgs.append(f)
        except Exception as ex:
            cfgs.append(f"{f}: {ex}")
    print(f"    configs loaded: {sum(1 for c in cfgs if ':' not in c)}/{len(cfgs)}")
    print()

    # 4) Readiness
    try:
        r = brain.readiness_score()
        print(f"[4] READINESS  {r['score']}/100")
    except Exception as e:
        print("[4] READINESS  ERR", e)
    print()

    # 5) Framework first closed trades (flag_fw / orb_fw)
    try:
        conn = brain.init_db()
        rows = conn.execute(
            "SELECT symbol, strategy, ROUND(pnl_pct,2), ts FROM trade_pnl "
            "WHERE strategy LIKE '%flag%' OR strategy LIKE '%orb%' "
            "ORDER BY ts ASC LIMIT 12").fetchall()
        conn.close()
        if rows:
            print("[5] FRAMEWORK FIRST CLOSED TRADES")
            for sym, strat, pnl, ts in rows:
                print(f"      {sym:<9} {strat:<22} {pnl:+.2f}%  ({ts})")
        else:
            print("[5] FRAMEWORK      no framework closed trades yet — waiting for first exits")
    except Exception as e:
        print("[5] FRAMEWORK   ERR", e)


if __name__ == "__main__":
    main()