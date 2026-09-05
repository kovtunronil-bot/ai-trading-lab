"""Daily strategy radar: re-verify every live config against the full
framework strategy library on 4y, and flag any alternative that wins with
acceptable risk. Alerts via ntfy so the human sees upgrades the same way the
bot reports trades. Read-only: never writes configs (cloud stays single-writer).

Promotion rule (same bar used for previous promotions):
  - alternative Sharpe > current Sharpe * 1.05 on 4y
  - drawdown not worse than -25%
  - at least 8 trades in the backtest
  - alternative still positive on 2y (recent regime sanity check)
"""
import warnings
warnings.filterwarnings("ignore")
import copy
import glob
import json
import os
import sys

import yfinance as yf

import brain

ALL_MODES = [
    "flag_fw", "orb_fw", "boll_fw", "vwap_fw", "smc_fw", "gap_fade_fw",
    "breakout_fw", "divergence_fw", "heikin_fw", "vwap_rev_fw", "fvg_fw",
]
ENTRIES = {
    "breakout_fw": [20, 55], "divergence_fw": [14, 20], "heikin_fw": [10, 20],
    "vwap_rev_fw": [10, 20], "vwap_fw": [10, 20], "smc_fw": [10, 20],
    "boll_fw": [20, 30], "orb_fw": [10, 20], "flag_fw": [30, 20],
    "gap_fade_fw": [10, 20], "fvg_fw": [10, 20],
}

DD_FLOOR = -25.0
MIN_TRADES = 8
BEAT_BY = 1.05


def _metrics(cfg, period):
    try:
        sym = cfg.get("symbol")
        df = yf.Ticker(brain.yf_symbol(sym)).history(period=period, auto_adjust=False)
        if df is None or df.empty or len(df) < 120:
            return None
        r = brain.backtest_report(df, cfg)
        n = (r.get("strategy") or {}).get("trades", 0) or 0
        return {
            "sharpe": float(r["sharpe"]),
            "dd": float(r["max_dd"]) * 100,
            "total": float(r["total_return"]) * 100,
            "trades": int(n),
        }
    except Exception:
        return None


def main():
    out = []
    for f in sorted(glob.glob("config_*.json")):
        cfg = json.load(open(f, encoding="utf-8"))
        sym = cfg.get("symbol")
        cur_mode = cfg.get("mode")
        cur = _metrics(cfg, "4y")
        if cur is None or cur["sharpe"] == 0.0:
            continue
        candidates = []
        for mode in ALL_MODES:
            if mode == cur_mode:
                continue
            for e in ENTRIES.get(mode, [20]):
                cc = copy.deepcopy(cfg)
                cc["mode"] = mode
                cc["entry"] = e
                m4 = _metrics(cc, "4y")
                if not m4 or m4["dd"] < DD_FLOOR or m4["trades"] < MIN_TRADES:
                    continue
                m2 = _metrics(cc, "2y")
                if not m2 or m2["sharpe"] <= 0.0:
                    continue
                candidates.append((mode, e, m4, m2))
        candidates.sort(key=lambda x: -x[2]["sharpe"])
        line = f"{sym:<8} {cur_mode:<12} S={cur['sharpe']:+.2f} DD={cur['dd']:+.1f}%"
        if candidates and candidates[0][2]["sharpe"] > cur["sharpe"] * BEAT_BY:
            mode, e, m4, m2 = candidates[0]
            line += (
                f"  -> UPGRADE {mode} e={e}: S4y={m4['sharpe']:+.2f} "
                f"DD4y={m4['dd']:+.1f}% S2y={m2['sharpe']:+.2f} n={m4['trades']}"
            )
        elif candidates:
            mode, e, m4, m2 = candidates[0]
            line += f"  (best alt {mode} e={e}: S4y={m4['sharpe']:+.2f} DD4y={m4['dd']:+.1f}% — below bar)"
        else:
            line += "  (no alt above bar)"
        print(line, flush=True)
        out.append(line)
    upgrades = [l for l in out if "-> UPGRADE" in l]
    if upgrades:
        msg = "STRATEGY-SCAN | upgrades found:\n" + "\n".join(upgrades)
    else:
        msg = "STRATEGY-SCAN | no upgrades (current lineup holds the bar)"
    brain.send_alert(msg)
    with open("strategy_scan_output.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())