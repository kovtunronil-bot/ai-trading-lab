import base64
import csv
import glob
import io
import json
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import brain


def load_equity():
    try:
        df = brain.equity_history_df()
        if not df.empty:
            return df
    except Exception:
        pass
    try:
        return pd.read_csv("equity_history.csv")
    except FileNotFoundError:
        return pd.DataFrame(columns=["date", "equity", "peak", "drawdown_pct"])


def load_journal_tail(n=14):
    try:
        return brain.journal_tail(n)
    except Exception:
        try:
            with open(brain.JOURNAL_FILE, encoding="utf-8") as f:
                return f.read().strip().splitlines()[-n:]
        except FileNotFoundError:
            return ["no runs yet"]


def make_chart_png(df):
    if df.empty:
        return b""
    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=110)
    dates = pd.to_datetime(df["date"])
    ax.plot(dates, df["equity"], color="#22c55e", linewidth=2, marker="o", markersize=3)
    ax.axhline(100000, color="#64748b", linestyle="--", linewidth=1)
    ax.fill_between(dates, 100000, df["equity"],
                    where=df["equity"] >= 100000, color="#22c55e", alpha=0.15)
    ax.fill_between(dates, 100000, df["equity"],
                    where=df["equity"] < 100000, color="#ef4444", alpha=0.15)
    ax.set_title("Paper equity over time ($100k start)", color="#e2e8f0")
    ax.grid(alpha=0.25)
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.tick_params(colors="#94a3b8")
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def load_champions():
    rows = []
    for path in sorted(glob.glob("config_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
            rows.append((c.get("symbol", "?"), c.get("label", "?"),
                         c.get("test_score", "?"), c.get("updated", "?")[:10]))
        except (json.JSONDecodeError, OSError):
            pass
    return rows


def load_lessons_tail(n=6):
    try:
        with open(brain.LESSONS_FILE, encoding="utf-8") as f:
            return f.read().strip().splitlines()[-n:]
    except FileNotFoundError:
        return ["no evolution yet"]


def load_positions():
    try:
        with open("positions_snapshot.json", encoding="utf-8") as f:
            snap = json.load(f)
        return snap.get("positions", []), snap.get("generated", "?")
    except (FileNotFoundError, json.JSONDecodeError):
        return [], "?"


def load_incidents():
    try:
        stats = brain.incident_stats(7)
        return str(sum(n for _, n in stats)) if stats else "0 ✅"
    except Exception:
        return "0 ✅"


def load_slippage():
    try:
        conn = brain.init_db()
        row = conn.execute(
            "SELECT COUNT(*), AVG(slippage_bps) FROM trades WHERE slippage_bps IS NOT NULL"
        ).fetchone()
        conn.close()
        n, avg = row[0], row[1]
        if not n:
            return None
        return f"{avg:+.1f} bps ({n} trades)"
    except Exception:
        return None


def load_live_scores():
    try:
        conn = brain.init_db()
        rows = conn.execute("""SELECT strategy, COUNT(*), ROUND(AVG(pnl_pct),2) FROM trade_pnl
                               GROUP BY strategy ORDER BY AVG(pnl_pct) DESC""").fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def load_readiness():
    try:
        return brain.readiness_score()
    except Exception:
        return {"score": 0, "checks": []}


def build_html():
    eq = load_equity()
    png_b64 = base64.b64encode(make_chart_png(eq)).decode()
    champions = load_champions()
    journal = load_journal_tail()
    lessons = load_lessons_tail()
    positions, snap_time = load_positions()
    slippage = load_slippage()
    incidents = load_incidents()
    live_scores = load_live_scores()
    ready = load_readiness()
    ready_emoji = "🟢" if ready["score"] >= 80 else ("🟡" if ready["score"] >= 50 else "🔒")

    if eq.empty:
        equity_val = peak_val = dd = 100000.0, 100000.0, 0.0
        equity_val, peak_val, dd = 100000.0, 100000.0, 0.0
    else:
        equity_val = float(eq["equity"].iloc[-1])
        peak_val = float(eq["peak"].iloc[-1])
        dd = float(eq["drawdown_pct"].iloc[-1])
    pnl = equity_val - 100000

    champ_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(s, l, sc, u)
        for s, l, sc, u in champions)
    journal_rows = "".join("<tr><td>{}</td></tr>".format(r) for r in journal)
    lesson_rows = "".join("<li>{}</li>".format(r) for r in lessons)

    # Build these before the big f-string: nested f-strings with backslash
    # escapes (like class=\"...\") are a SyntaxError on Python 3.11.
    def _pos_td(v):
        return 'class="pos"' if v >= 0 else 'class="neg"'
    pos_rows = ""
    for p in positions:
        pos_rows += (
            "<tr><td>{}</td><td>{}</td><td>${:,.2f}</td>"
            "<td>${:,.0f}</td>"
            "<td {}>${:+,.0f}</td></tr>".format(
                p["symbol"], p["qty"], p["entry"], p["market_value"],
                _pos_td(p["unrealized_pl"]), p["unrealized_pl"]))
    if not pos_rows:
        pos_rows = "<tr><td colspan=5>all cash — waiting for signals</td></tr>"

    score_rows = ""
    for s, n, a in live_scores:
        verdict = "✅ trusted" if a >= 0 else "🔥 FIRED — brain refuses to re-elect"
        score_rows += ("<tr><td>{}</td><td>{}</td>"
                       "<td {}>{:+.2f}%</td><td>{}</td></tr>").format(
                           s, n, _pos_td(a), a, verdict)
    if not score_rows:
        score_rows = "<tr><td colspan=4>no closed trades yet — learning begins after first exits</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AI Trading Robot — Dashboard</title>
<meta http-equiv="refresh" content="300">
<style>
body {{ background:#0f172a; color:#e2e8f0; font-family:Consolas,monospace; margin:24px; }}
h1 {{ color:#38bdf8; }} h2 {{ color:#7dd3fc; border-bottom:1px solid #334155; padding-bottom:4px;}}
.cards {{ display:flex; gap:16px; flex-wrap:wrap; }}
.card {{ background:#1e293b; border-radius:12px; padding:16px 22px; min-width:170px; }}
.card .big {{ font-size:26px; font-weight:bold; }}
.pos {{ color:#22c55e; }} .neg {{ color:#ef4444; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
td,th {{ padding:5px 10px; border-bottom:1px solid #1e293b; text-align:left; }}
th {{ color:#94a3b8; }}
img {{ max-width:100%; border-radius:12px; margin-top:8px; }}
li {{ margin:4px 0; color:#cbd5e1; }}
.small {{ color:#64748b; font-size:12px; }}
</style></head><body>
<h1>🤖 AI Trading Robot — Dashboard</h1>
<p class="small">generated {datetime.now():%Y-%m-%d %H:%M:%S} · auto-reloads every 5 min · data: local files only</p>
<div class="cards">
<div class="card"><div class="small">EQUITY</div><div class="big">${equity_val:,.0f}</div></div>
<div class="card"><div class="small">PAPER P&L</div><div class="big {'pos' if pnl>=0 else 'neg'}">${pnl:+,.0f}</div></div>
<div class="card"><div class="small">PEAK</div><div class="big">${peak_val:,.0f}</div></div>
<div class="card"><div class="small">DRAWDOWN</div><div class="big {'pos' if dd>-5 else ('neg' if dd<-5 else '')}">{dd:.1f}%</div></div>
<div class="card"><div class="small">SAFETY LADDER</div><div class="big">{'🔒' if dd<=-20 else ('🛑' if dd<=-10 else ('⚠️' if dd<=-5 else '✅ CLEAR'))}</div></div>
<div class="card"><div class="small">OPEN POSITIONS</div><div class="big">{len(positions)}</div></div>
<div class="card"><div class="small">AVG SLIPPAGE</div><div class="big" style="font-size:20px">{slippage or '—'}</div></div>
<div class="card"><div class="small">INCIDENTS 7d</div><div class="big" style="font-size:20px">{incidents}</div></div>
<div class="card"><div class="small">REAL-MONEY READY</div><div class="big">{ready_emoji} {ready['score']}/100</div></div>
</div>
<h2>Open positions <span class="small">(snapshot {snap_time})</span></h2>
<table><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Value</th><th>P&L</th></tr>
{pos_rows}</table>
<h2>Equity curve</h2><img src="data:image/png;base64,{png_b64}">
<h2>Live strategy scoreboard</h2>
<table><tr><th>Strategy</th><th>Closed trades</th><th>Avg P&amp;L %</th><th>Verdict</th></tr>
{score_rows}</table>
<h2>Champion strategies (per market)</h2>
<table><tr><th>Market</th><th>Strategy</th><th>Test score</th><th>Checked</th></tr>{champ_rows}</table>
<h2>Recent activity (journal)</h2><table>{journal_rows}</table>
<h2>Evolution diary (latest)</h2><ul>{lesson_rows}</ul>
</body></html>"""


if __name__ == "__main__":
    html = build_html()
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)
    # Also publish to the Pages dir so the GitHub Pages site (the phone link)
    # serves the FULL dashboard (all markets, equity, positions, P&L) instead
    # of only the crypto summary.
    import os
    os.makedirs("_pages", exist_ok=True)
    with open(os.path.join("_pages", "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("dashboard.html written (+ _pages/index.html)")
