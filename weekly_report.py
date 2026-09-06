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
    print()

    # 6) October verdict gate (see VERDICT.md). Measured go/no-go, honest.
    try:
        gates = {}
        conn = brain.init_db()

        # G1 STABILITY: no full-day outage in the last 14 days.
        # equity_history holds one row per day, so a gap > 60h = missed days.
        days = conn.execute(
            "SELECT date FROM equity_history ORDER BY date DESC LIMIT 14").fetchall()
        prev = None
        gaps = []
        for (d,) in days:
            try:
                t = datetime.fromisoformat(d)
            except Exception:
                continue
            if prev is not None:
                gaps.append((prev - t).total_seconds() / 3600)
            prev = t
        stable = bool(gaps) and max(gaps) <= 60
        gates["Stability (no outage 14d)"] = stable

        # G2 DRAWDOWN: current drawdown within design bounds.
        bound = float(df["drawdown_pct"].iloc[-1]) >= -12 if not df.empty else False
        gates["Drawdown >= -12%"] = bound

        # G3 FRAMEWORK: first framework trades prove positive expectancy.
        fw = conn.execute(
            "SELECT COUNT(*), AVG(pnl_pct) FROM trade_pnl "
            "WHERE strategy LIKE '%flag%' OR strategy LIKE '%orb%'").fetchone()
        fw_n, fw_avg = (fw or (0, None))
        fw_ok = bool(fw_n and fw_n >= 15 and (fw_avg or 0) >= 0.25)
        gates["Framework trades n>=15 avg>=+0.25%"] = fw_ok

        # G4 READINESS.
        ready = float(r["score"]) >= 85
        gates["Readiness >= 85/100"] = ready
        conn.close()

        print("[6] OCTOBER VERDICT GATE")
        for name, ok in gates.items():
            print(f"      [{'PASS' if ok else 'PEND'}]  {name}")
        if all(gates.values()):
            print("      VERDICT: READY — measured evidence supports real money")
        elif not fw_ok:
            print("      VERDICT: PENDING — waiting for first framework exits (honest gate)")
        else:
            print("      VERDICT: NOT READY yet — see failing gates above")
    except Exception as e:
        print("[6] VERDICT    ERR", e)


if __name__ == "__main__":
    main()