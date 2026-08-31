#!/usr/bin/env python3
"""
crypto_dashboard.py  (upgraded / design-deep version)
Generates a polished, dark-theme crypto dashboard with big charts,
long history, stats cards, a trades table and AI verdicts.
Run:  py crypto_dashboard.py   ->  opens crypto_dashboard.html
"""
import sqlite3
import json
import html
import subprocess
import sys
import os
import pandas as pd
import yfinance as yf
import brain

import os

OUT = "crypto_dashboard.html"
PAGES_DIR = "_pages"
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]
YF = {s: brain.yf_symbol(s) for s in CRYPTO_SYMBOLS}

# ---------- data ----------
def get_conn():
    return sqlite3.connect("lab.db")

price_hist = {}
live_price = {}
for internal in CRYPTO_SYMBOLS:
    y = YF[internal]
    try:
        df = yf.Ticker(y).history(period="6mo")
        df = df.reset_index()
        rows = [{"d": r.Date.strftime("%Y-%m-%d"), "close": round(float(r.Close), 2)}
                for r in df.itertuples(index=False)]
        price_hist[internal] = rows[-180:]
        live_price[internal] = rows[-1]["close"] if rows else None
    except Exception:
        price_hist[internal] = []
        live_price[internal] = None

conn = get_conn()
try:
    tdf = pd.read_sql("SELECT symbol, pnl_pct, ts, strategy FROM trade_pnl", conn)
except Exception:
    tdf = pd.DataFrame(columns=["symbol", "pnl_pct", "ts", "strategy"])

crypto_trades = tdf[tdf["symbol"].isin(CRYPTO_SYMBOLS)].copy()
trade_summary = {}
trade_rows = {}
for s in CRYPTO_SYMBOLS:
    sub = crypto_trades[crypto_trades["symbol"] == s].copy()
    n = len(sub)
    trade_summary[s] = {
        "n": n,
        "avg": round(float(sub["pnl_pct"].mean()), 2) if n else 0.0,
        "winrate": round(float((sub["pnl_pct"] > 0).mean() * 100), 1) if n else 0.0,
        "sum": round(float(sub["pnl_pct"].sum()), 2) if n else 0.0,
    }
    sub = sub.sort_values("ts", ascending=False).head(8)
    trade_rows[s] = [
        {"ts": str(r.ts)[:16], "pnl": round(float(r.pnl_pct), 2), "strategy": str(r.strategy or "")}
        for r in sub.itertuples(index=False)
    ]

ai_news = {}
try:
    adf = pd.read_sql("SELECT symbol, verdict, reason, ts FROM ai_news", conn)
    for s in CRYPTO_SYMBOLS:
        sub = adf[adf["symbol"] == s]
        if len(sub):
            r = sub.iloc[-1]
            ai_news[s] = {"verdict": str(r.verdict), "ts": str(r.ts)[:16], "reason": str(r.reason or "")}
        else:
            ai_news[s] = {"verdict": "n/a", "ts": "", "reason": ""}
except Exception:
    pass

positions = {}
try:
    k, sec = brain.api_creds()
    from alpaca.trading.client import TradingClient
    c = TradingClient(k, sec, paper=True)
    for p in c.get_all_positions():
        internal = brain.internal_sym(p.symbol)
        if internal in CRYPTO_SYMBOLS:
            positions[internal] = {
                "qty": float(p.qty),
                "avg": float(p.avg_entry_price),
                "cur": float(p.current_price),
                "pnl_pct": round(float(p.unrealized_plpc) * 100, 2),
            }
except Exception:
    pass

conn.close()

# ---------- html ----------
def verdict_color(v):
    v = str(v).upper()
    if v == "BULLISH":
        return "#0c6"
    if v == "BEARISH":
        return "#f33"
    return "#888"

def chart_json(s):
    return json.dumps(price_hist.get(s, []))

cards = ""
for s in CRYPTO_SYMBOLS:
    ts = trade_summary[s]
    lp = live_price.get(s)
    pos = positions.get(s)
    ai = ai_news.get(s, {})
    vc = verdict_color(ai.get("verdict"))
    pos_html = ""
    if pos:
        pc = "#0c6" if pos["pnl_pct"] >= 0 else "#f33"
        pos_html = (f"<div class='posheader'>Position OPEN — qty {pos['qty']} @ ${pos['avg']:,.2f} "
                    f"<br>now ${pos['cur']:,.2f} &nbsp; <b style='color:{pc}'>{pos['pnl_pct']:+.2f}%</b></div>")
    # trades table
    rows_html = "".join(
        f"<tr><td class='dim'>{html.escape(r['ts'])}</td><td>{html.escape(r['strategy'][:16])}</td>"
        f"<td style='color:{'#0c6' if r['pnl']>=0 else '#f33'}'>{r['pnl']:+.2f}%</td></tr>"
        for r in trade_rows[s]
    ) or "<tr><td colspan='3' class='dim'>אין טריידים</td></tr>"

    cards += f"""
  <div class="card">
    <div class="cardhead">
      <div>
        <div class="sym">{html.escape(s)}</div>
        <div class="aiverd"><span class="dot" style="background:{vc}"></span>AI: <b style="color:{vc}">{html.escape(str(ai.get('verdict')))}</b>
        <span class="dim"> {html.escape(str(ai.get('ts')))}</span></div>
      </div>
      <div class="live">${lp:,.2f}</div>
    </div>
    {pos_html}
    <div class="stats">
      <div class="stat"><span class="k">טריידים</span><span class="v">{ts['n']}</span></div>
      <div class="stat"><span class="k">ממוצע</span><span class="v" style="color:{'#0c6' if ts['avg']>=0 else '#f33'}">{ts['avg']:+.2f}%</span></div>
      <div class="stat"><span class="k">אחוז זכייה</span><span class="v">{ts['winrate']}%</span></div>
      <div class="stat"><span class="k">סה״כ</span><span class="v" style="color:{'#0c6' if ts['sum']>=0 else '#f33'}">{ts['sum']:+.2f}%</span></div>
    </div>
    <canvas id="ch-{s.replace('/','_')}" height="220"></canvas>
    <div class="tradeshead">טריידים אחרונים</div>
    <table class="trades"><tr><th>תאריך</th><th>אסטרטגיה</th><th>רווח%</th></tr>{rows_html}</table>
  </div>"""

chart_map = {"ch-" + s.replace("/", "_"): chart_json(s) for s in CRYPTO_SYMBOLS}

# overall totals card
total_trades = sum(trade_summary[s]["n"] for s in CRYPTO_SYMBOLS)
total_avg = (sum(trade_summary[s]["avg"] * trade_summary[s]["n"] for s in CRYPTO_SYMBOLS)
             / total_trades) if total_trades else 0
allsum = sum(trade_summary[s]["sum"] for s in CRYPTO_SYMBOLS)
summary_card = f"""
  <div class="card summary">
    <div class="sym" style="color:#c9f">סיכום קריפטו</div>
    <div class="stats">
      <div class="stat"><span class="k">נכסים פעילים</span><span class="v">{len(CRYPTO_SYMBOLS)}</span></div>
      <div class="stat"><span class="k">סה״כ טריידים</span><span class="v">{total_trades}</span></div>
      <div class="stat"><span class="k">ממוצע משוקלל</span><span class="v" style="color:{'#0c6' if total_avg>=0 else '#f33'}">{total_avg:+.2f}%</span></div>
      <div class="stat"><span class="k">סה״כ רווח%</span><span class="v" style="color:{'#0c6' if allsum>=0 else '#f33'}">{allsum:+.2f}%</span></div>
    </div>
  </div>"""

htmldoc = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>דשבורד קריפטו — בוט מסחר</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
 :root {{
   --bg:#0b101a; --card:#151d2c; --line:#26344e; --txt:#e8ecf4; --dim:#8a93a6;
   --acc:#3ea6ff; --green:#0c6; --red:#f3564d;
 }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; background:var(--bg); color:var(--txt);
        font-family:'Segoe UI',Arial,sans-serif; padding:22px; }}
 h1 {{ color:var(--acc); font-size:22px; margin:0 0 18px;
       display:flex; align-items:center; gap:10px; }}
 .sub {{ color:var(--dim); font-size:13px; margin:0 0 20px; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:18px; }}
 .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
         padding:18px; box-shadow:0 4px 18px rgba(0,0,0,.35); }}
 .cardhead {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }}
 .sym {{ font-size:22px; font-weight:bold; color:#fff; }}
 .live {{ font-size:24px; font-weight:bold; color:#7f7; }}
 .aiverd {{ font-size:13px; margin-top:4px; }}
 .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%;
        margin-left:4px; }}
 .posheader {{ background:#22304a; border:1px solid #33456b; border-radius:8px;
              padding:8px 10px; font-size:13px; margin-bottom:12px; color:#ffe9a8; }}
 .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:12px 0; }}
 .stat {{ background:#0f1726; border:1px solid var(--line); border-radius:10px;
         padding:10px 8px; text-align:center; }}
 .stat .k {{ display:block; font-size:11px; color:var(--dim); margin-bottom:4px; }}
 .stat .v {{ font-size:16px; font-weight:bold; }}
 .tradeshead {{ margin:14px 0 6px; font-size:13px; color:var(--dim); }}
 table.trades {{ width:100%; border-collapse:collapse; font-size:13px; }}
 table.trades th, table.trades td {{ text-align:right; padding:5px 6px;
        border-bottom:1px solid var(--line); }}
 table.trades th {{ color:var(--dim); font-weight:normal; }}
 .dim {{ color:var(--dim); font-size:12px; }}
 @media (max-width:640px) {{ .stats {{ grid-template-columns:repeat(2,1fr); }}
                              .grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<h1>📊 דשבורד קריפטו <span class="sub">מבוסס על נתוני הבוט • {len(CRYPTO_SYMBOLS)} נכסים</span></h1>
<div class="grid">
{summary_card}
{cards}
</div>
<script>
const data = {json.dumps(chart_map, ensure_ascii=False)};
Object.keys(data).forEach(function(id){{
  const el = document.getElementById(id);
  if(!el) return;
  const rows = data[id];
  new Chart(el.getContext('2d'), {{
    type:'line',
    data:{{ labels:rows.map(r=>r.d),
            datasets:[{{ label:'מחיר', data:rows.map(r=>r.close),
                        borderColor:'#3ea6ff', backgroundColor:'rgba(62,166,255,.12)',
                        fill:true, tension:.25, pointRadius:0 }}] }},
    options:{{ responsive:true,
               plugins:{{ legend:{{display:false}} }},
               scales:{{ x:{{ ticks:{{ color:'#8a93a6', maxTicksLimit:8, maxRotation:0 }} }},
                         y:{{ ticks:{{ color:'#8a93a6' }} }} }}
    }}
  }});
}});
</script>
</body>
</html>"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(htmldoc)

# Also write into a dedicated pages dir so the Pages deploy only exposes the
# dashboard (index.html) and not the whole repo.
os.makedirs(PAGES_DIR, exist_ok=True)
with open(os.path.join(PAGES_DIR, "index.html"), "w", encoding="utf-8") as f:
    f.write(htmldoc)

print("Wrote", OUT)
print("live:", {s: live_price.get(s) for s in CRYPTO_SYMBOLS})
print("positions:", positions)

# auto-open
try:
    subprocess.Popen(["cmd", "/c", "start", "", OUT], shell=True)
except Exception:
    pass
