"""Daily strategy radar: re-verify every live config against the full
framework strategy library on 4y, and flag any alternative that wins with
acceptable risk. Alerts via ntfy so the human sees upgrades the same way the
bot reports trades. Read-only: never writes configs (cloud stays single-writer).

Promotion rule (same bar used for previous promotions):
  - alternative Sharpe > current Sharpe * 1.05 on 4y
  - drawdown not worse than -25%
  - at least 8 trades in the backtest
  - alternative still positive on 2y (recent regime sanity check)

When PROMOTE=1 is set (strategy-scan.yml cron), a winner that clears an even
stricter gate is written back into its live config automatically:
  - the 4y beat bar above,
  - a third independent 3y window must also be positive (sharpe>0, DD>=-25%,
    n>=8),
  - the current config must already be a framework (_fw) strategy (frozen
    legacy/manual configs like ETH atr_breakout are never auto-touched),
  - only the measured fields change (mode, entry, label, test_score, updated);
    every other config field is left untouched.
The promotion is announced on ntfy exactly like the bot announces trades, so
every auto-change stays visible and auditable.

NEW-SYMBOL DISCOVERY: symbols added to the tracked universe (brain.SYMBOLS)
that have no config yet are tested against the full framework library. A new
symbol is admitted only when a framework mode clears a strict ABSOLUTE bar on
every window (4y Sharpe >= NEW_MIN_SHARPE, DD >= -25%, n >= 8; 3y positive;
2y positive). With PROMOTE=1 the winner is written as a fresh config (regime
deliberately omitted so the bot's regime-gate can never legacy-evolve it).
"""
import warnings
warnings.filterwarnings("ignore")
import copy
import glob
import json
import os
import sys
from datetime import datetime, timezone

import yfinance as yf

import brain

ALL_MODES = [
    "flag_fw", "orb_fw", "boll_fw", "vwap_fw", "smc_fw", "gap_fade_fw",
    "breakout_fw", "divergence_fw", "heikin_fw", "vwap_rev_fw", "fvg_fw",
]
ENTRIES = {
    "breakout_fw": [20, 55, 35], "divergence_fw": [14, 20, 17], "heikin_fw": [10, 20, 15],
    "vwap_rev_fw": [10, 20, 15], "vwap_fw": [10, 20, 15], "smc_fw": [10, 20, 15],
    "boll_fw": [20, 30, 25], "orb_fw": [10, 20, 15], "flag_fw": [30, 20, 25],
    "gap_fade_fw": [10, 20, 15], "fvg_fw": [10, 20, 15],
}

DD_FLOOR = -25.0
MIN_TRADES = 8
BEAT_BY = 1.05
NEW_MIN_SHARPE = 1.0   # absolute 4y admission bar for brand-new symbols


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


def _promote_cfg(path, cfg, cand, cur, sym):
    """Strict-gate auto-promotion. Returns True if the config was written."""
    mode, e, m4, m2 = cand
    cur_mode = cfg.get("mode", "")
    if not str(cur_mode).endswith("_fw"):
        return False  # frozen legacy/manual config: human-only
    if m4["sharpe"] <= cur["sharpe"] * BEAT_BY:
        return False
    m3 = _metrics({"symbol": sym, "mode": mode, "entry": e}, "3y")
    if not m3 or m3["sharpe"] <= 0.0 or m3["dd"] < DD_FLOOR or m3["trades"] < MIN_TRADES:
        return False
    old = {
        "mode": cur_mode,
        "sharpe": round(cur["sharpe"], 2),
        "dd": round(cur["dd"], 1),
    }
    cfg["mode"] = mode
    cfg["entry"] = e
    cfg["label"] = f"{mode} (auto-promotion)"
    cfg["test_score"] = round(m4["sharpe"], 2)
    cfg["promoted"] = {
        "from": cur_mode,
        "from_sharpe": old["sharpe"],
        "framework_sharpe": round(m4["sharpe"], 2),
        "dd": round(m4["dd"], 1),
        "sharpe_2y": round(m2["sharpe"], 2),
        "sharpe_3y": round(m3["sharpe"], 2),
        "updated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        "note": "auto-promotion: 4y/3y/2y validation + Sharpe>1.05x bar",
    }
    cfg["updated"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    print(f"    PROMOTE-> wrote {path} ({cur_mode} -> {mode}, S {old['sharpe']} -> {m4['sharpe']:.2f})", flush=True)
    return True


def _discover_sym(sym):
    """Best framework mode+entry for a symbol with no config yet.

    Returns (mode, e, m4, m2, m3) of the highest-4y-sharpe candidate that
    clears every window (4y: sharpe>=NEW_MIN_SHARPE, dd/trades floors;
    3y: sharpe>0, dd/trades floors; 2y: sharpe>0), or None.
    """
    best = None
    for mode in ALL_MODES:
        for e in ENTRIES.get(mode, [20]):
            cc = {"symbol": sym, "mode": mode, "entry": e}
            m4 = _metrics(cc, "4y")
            if not m4 or m4["sharpe"] < NEW_MIN_SHARPE or m4["dd"] < DD_FLOOR or m4["trades"] < MIN_TRADES:
                continue
            m2 = _metrics(cc, "2y")
            if not m2 or m2["sharpe"] <= 0.0:
                continue
            m3 = _metrics(cc, "3y")
            if not m3 or m3["sharpe"] <= 0.0 or m3["dd"] < DD_FLOOR or m3["trades"] < MIN_TRADES:
                continue
            cand = (mode, e, m4, m2, m3)
            if best is None or m4["sharpe"] > best[2]["sharpe"]:
                best = cand
    return best


def _admit_cfg(sym, best):
    """Write a brand-new config for a discovered symbol. Regime is deliberately
    omitted so the live bot's regime-gate can never auto-evolve it (it stays on
    the measured scan pipeline)."""
    mode, e, m4, m2, m3 = best
    cfg = {
        "mode": mode,
        "entry": e,
        "label": f"{mode} (scan-discovered)",
        "symbol": sym,
        "test_score": round(m4["sharpe"], 2),
        "promoted": {
            "from": "NEWSymbol",
            "framework_sharpe": round(m4["sharpe"], 2),
            "dd": round(m4["dd"], 1),
            "sharpe_2y": round(m2["sharpe"], 2),
            "sharpe_3y": round(m3["sharpe"], 2),
            "updated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
            "note": "scan-discovered: 4y/3y/2y validation + absolute Sharpe bar",
        },
        "updated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
    }
    with open(brain.config_path(sym), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    return cfg


def main():
    promote = os.environ.get("PROMOTE", "0") == "1"
    out = []
    promoted = []

    seen = set()
    for f in sorted(glob.glob(brain.config_path("*"))):
        cfg = json.load(open(f, encoding="utf-8"))
        sym = cfg.get("symbol")
        if not sym:
            continue
        seen.add(sym)
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
            if promote and _promote_cfg(f, cfg, candidates[0], cur, sym):
                promoted.append(line)
        elif candidates:
            mode, e, m4, m2 = candidates[0]
            line += f"  (best alt {mode} e={e}: S4y={m4['sharpe']:+.2f} DD4y={m4['dd']:+.1f}% — below bar)"
        else:
            line += "  (no alt above bar)"
        print(line, flush=True)
        out.append(line)

    # NEW-SYMBOL DISCOVERY: markets in the tracked universe with no config yet
    # (added to brain.SYMBOLS for the bot to scan, waiting on measured proof).
    # A symbol is admitted only when a framework mode clears every window at an
    # absolute bar (4y/3y/2y). With PROMOTE=1 a winner is written as a fresh
    # config; otherwise the radar reports it as an admit candidate.
    for sym in [s for s in brain.ALL if s not in seen]:
        best = _discover_sym(sym)
        if best is None:
            out.append(f"{sym:<8} NEW     (no framework mode above admit bar)")
            print(f"{sym:<8} NEW     (no framework mode above admit bar)", flush=True)
            continue
        mode, e, m4, m2, m3 = best
        line = (f"{sym:<8} ADMIT {mode} e={e}: S4y={m4['sharpe']:+.2f} "
                f"DD4y={m4['dd']:+.1f}% S2y={m2['sharpe']:+.2f} S3y={m3['sharpe']:+.2f} n={m4['trades']}")
        if promote:
            _admit_cfg(sym, best)
            line += "  -> WROTE config"
            promoted.append(line)
        out.append(line)
        print(line, flush=True)

    promotions = [l for l in promoted if "ADMIT" in l]
    upgrades = [l for l in out if "-> UPGRADE" in l]
    if promoted:
        parts = []
        if upgrades:
            parts.append("AUTO-PROMOTIONS applied:\n" + "\n".join(upgrades))
        if promotions:
            parts.append("NEW SYMBOLS admitted:\n" + "\n".join(promotions))
        msg = "STRATEGY-SCAN | " + "\n\n".join(parts)
    elif upgrades:
        msg = "STRATEGY-SCAN | upgrades found (auto-gate: 3y not met, alert only):\n" + "\n".join(upgrades)
    else:
        msg = "STRATEGY-SCAN | no upgrades (current lineup holds the bar)"
    brain.send_alert(msg)
    with open("strategy_scan_output.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())