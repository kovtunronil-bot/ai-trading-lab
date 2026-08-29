import json
import csv
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "GLD",
           "AMD", "META", "GOOGL", "AMZN", "NFLX"]
CRYPTO = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}


def api_creds():
    """Return (API_KEY, SECRET_KEY). Prefer env vars (GitHub Actions cloud),
    fall back to keys.py (local dev)."""
    import os
    key = os.environ.get("APCA_API_KEY_ID", "") or os.environ.get("API_KEY", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "") or os.environ.get("SECRET_KEY", "")
    if key and secret:
        return key, secret
    try:
        from keys import API_KEY, SECRET_KEY
        return API_KEY, SECRET_KEY
    except Exception:
        return None, None


def alpaca_sym(symbol):
    return symbol.replace("/", "").replace("-", "")


def internal_sym(alpaca_symbol):
    for k in CRYPTO:
        if alpaca_sym(k) == alpaca_symbol.replace("/", "").replace("-", ""):
            return k
    return alpaca_symbol
ALL = SYMBOLS + list(CRYPTO.keys())
CAPITAL = 100000.0
JOURNAL_FILE = "journal.csv"
LESSONS_FILE = "lessons.txt"
STATE_FILE = "state.json"
DB_FILE = "lab.db"

CIRCUIT_BREAKERS = [(-0.05, "CAUTION"), (-0.10, "HALT"), (-0.20, "LOCKDOWN")]
POSITION_STOP_LOSS = 0.08
PORTFOLIO_HEAT_CAP = 0.85
TRAIL_ACTIVATE = 0.03
TRAIL_GIVEBACK = 0.05
MIN_CONVICTION = 0.35
# Above this conviction, trade at full size; below it, scale down proportionally.
FREE_SAIL_CONVICTION = 0.55
FLAT_THRESHOLD = 0.005
FLAT_MAX_DAYS = 15
SECTOR_CAP = 0.40
SECTORS = {
    "tech": ["NVDA", "AMD", "MSFT", "GOOGL", "META", "AMZN", "NFLX", "AAPL"],
    "broad": ["SPY", "QQQ"],
    "commodity": ["GLD"],
    "crypto": ["BTC/USD", "ETH/USD"],
}


def backup_db(keep=7):
    import shutil, os
    try:
        os.makedirs("backups", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        dest = f"backups/lab_{stamp}.db"
        conn = init_db()
        conn.close()
        shutil.copy2(DB_FILE, dest)
        old = sorted(os.listdir("backups"))
        while len(old) > keep:
            os.remove(os.path.join("backups", old.pop(0)))
        return dest
    except Exception as e:
        print(f"backup skipped: {e}")
        return None


def save_positions_snapshot(positions):
    try:
        snap = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "positions": [
                {"symbol": p.symbol, "qty": str(p.qty),
                 "market_value": float(p.market_value),
                 "unrealized_pl": float(p.unrealized_pl),
                 "entry": float(p.avg_entry_price)}
                for p in positions
            ],
        }
        with open("positions_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except Exception:
        pass


_DB_CONN = None

def _db():
    global _DB_CONN
    if _DB_CONN is not None:
        try:
            _DB_CONN.execute("SELECT 1")
            return _DB_CONN
        except Exception:
            _DB_CONN = None
    _DB_CONN = sqlite3.connect(DB_FILE, timeout=10)
    _DB_CONN.execute("PRAGMA journal_mode=WAL")
    return _DB_CONN


def init_db():
    conn = _db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS journal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, price REAL, action TEXT, detail TEXT, equity REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS equity_history(
        date TEXT PRIMARY KEY, equity REAL, peak REAL, drawdown_pct REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, text TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, side TEXT, notional REAL, qty REAL,
        limit_price REAL, fill_price REAL, status TEXT, algo TEXT,
        slippage_bps REAL)""")
    try:
        c.execute("ALTER TABLE trades ADD COLUMN slippage_bps REAL")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS position_peaks(
        symbol TEXT PRIMARY KEY, peak REAL, entry REAL)""")
    try:
        c.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE trades ADD COLUMN signal_price REAL")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS position_strategy(
        symbol TEXT PRIMARY KEY, label TEXT, opened TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trade_pnl(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, strategy TEXT, pnl_pct REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS execution_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, side TEXT, limit_pct REAL,
        time_to_fill REAL, result TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS loss_memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, strategy TEXT, regime TEXT,
        pnl_pct REAL, conditions TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS tf_agreement_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, daily_signal TEXT, h4_signal TEXT,
        agreement REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS regime_strategy_perf(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        regime TEXT, strategy TEXT, symbol TEXT,
        avg_pnl REAL, win_rate REAL, n_trades INTEGER, last_updated TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS kelly_data(
        symbol TEXT PRIMARY KEY, win_rate REAL, avg_win REAL,
        avg_loss REAL, n_trades INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pattern_memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, pattern_hash TEXT,
        conditions TEXT, outcome_pnl REAL, regime TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ai_news(
        symbol TEXT, ts TEXT, verdict TEXT, reason TEXT, source TEXT,
        PRIMARY KEY (symbol, ts))""")
    return conn


def set_position_strategy(symbol, label):
    conn = init_db()
    conn.execute("INSERT OR REPLACE INTO position_strategy(symbol,label,opened) VALUES(?,?,?)",
                 (symbol, label, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def get_position_strategy(symbol):
    conn = init_db()
    row = conn.execute("SELECT label FROM position_strategy WHERE symbol=?", (symbol,)).fetchone()
    conn.close()
    return row[0] if row else None


def pop_position_strategy(symbol):
    conn = init_db()
    row = conn.execute("SELECT label FROM position_strategy WHERE symbol=?", (symbol,)).fetchone()
    conn.execute("DELETE FROM position_strategy WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()
    return row[0] if row else None


def log_closed_trade(symbol, strategy, entry, exit_price):
    try:
        if not strategy:
            cfg = load_config(symbol)
            strategy = cfg.get("label", "?") if cfg else "unknown"
        if not entry or float(entry) <= 0:
            return
        pnl = round((float(exit_price) / float(entry) - 1) * 100, 3)
        conn = init_db()
        conn.execute("INSERT INTO trade_pnl(ts,symbol,strategy,pnl_pct) VALUES(?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), symbol, strategy, pnl))
        conn.commit()
        conn.close()
    except Exception:
        pass


def live_strategy_scores(min_trades=3):
    conn = init_db()
    rows = conn.execute("""SELECT strategy, COUNT(*), AVG(pnl_pct) FROM trade_pnl
                           GROUP BY strategy HAVING COUNT(*) >= ?""",
                        (min_trades,)).fetchall()
    conn.close()
    return {r[0]: {"n": r[1], "avg_pnl": r[2]} for r in rows}


def live_penalty(label):
    s = live_strategy_scores().get(label)
    if s and s["avg_pnl"] < 0:
        return -1.5
    return 0.0


def log_execution(symbol, side, limit_pct, time_seconds, result):
    try:
        conn = init_db()
        conn.execute("""INSERT INTO execution_log(ts,symbol,side,limit_pct,time_to_fill,result)
                        VALUES(?,?,?,?,?,?)""",
                     (datetime.now().isoformat(timespec="seconds"), symbol, side,
                      limit_pct, time_seconds, result))
        conn.commit()
        conn.close()
    except Exception:
        pass


def best_limit_pct(symbol, side, default_steps=None):
    if default_steps is None:
        default_steps = [1.001, 1.0025] if side == "buy" else [0.999, 0.9975]
    try:
        conn = init_db()
        rows = conn.execute("""SELECT limit_pct, result FROM execution_log
                               WHERE symbol=? AND side=? AND result='filled'
                               ORDER BY ts DESC LIMIT 10""", (symbol, side)).fetchall()
        conn.close()
        if len(rows) < 3:
            return default_steps
        fills = [r[0] for r in rows if r[1] == "filled"]
        if not fills:
            return default_steps
        avg_fill = sum(fills) / len(fills)
        best = round(avg_fill, 4)
        return sorted(set([best, best * 1.001, best * 1.003]))
    except Exception:
        return default_steps


def days_in_trade(position):
    try:
        conn = init_db()
        row = conn.execute("""SELECT opened FROM position_strategy
                              WHERE symbol=?""", (position.symbol,)).fetchone()
        if not row:
            return 0
        opened = datetime.fromisoformat(row[0])
        return (datetime.now() - opened).days
    except Exception:
        return 0


def is_flat_trade(position, threshold=FLAT_THRESHOLD, max_days=FLAT_MAX_DAYS):
    try:
        entry = float(position.avg_entry_price)
        cur = float(position.current_price)
        if entry <= 0:
            return False
        pct = abs(cur / entry - 1)
        days = days_in_trade(position)
        return pct < threshold and days >= max_days
    except Exception:
        return False


def log_loss_conditions(symbol, strategy, regime, pnl_pct, conditions=""):
    try:
        conn = init_db()
        conn.execute("""INSERT INTO loss_memory(ts,symbol,strategy,regime,pnl_pct,conditions)
                        VALUES(?,?,?,?,?,?)""",
                     (datetime.now().isoformat(timespec="seconds"), symbol, strategy,
                      regime, pnl_pct, str(conditions)[:200]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def should_avoid(symbol, current_regime):
    try:
        conn = init_db()
        rows = conn.execute("""SELECT COUNT(*) FROM loss_memory
                               WHERE symbol=? AND regime=?
                               AND pnl_pct < -3""", (symbol, current_regime)).fetchone()
        conn.close()
        return rows[0] >= 3
    except Exception:
        return False


def loss_count(symbol, current_regime, threshold=-3):
    try:
        conn = init_db()
        rows = conn.execute("""SELECT COUNT(*) FROM loss_memory
                               WHERE symbol=? AND regime=?
                               AND pnl_pct < ?""", (symbol, current_regime, threshold)).fetchone()
        conn.close()
        return rows[0] if rows else 0
    except Exception:
        return 0


REGIME_MULT = {
    "BULL_TREND": 1.15,
    "BULL_RANGE": 1.05,
    "QUIET_RANGE": 0.95,
    "BEAR_RANGE": 0.80,
    "BEAR_TREND": 0.65,
    "HIGH_VOL": 0.75,
}


def regime_multiplier(regime):
    return REGIME_MULT.get(regime, 0.90)


REGIME_HEAT_CAP = {
    "BULL_TREND": 0.90,
    "QUIET_RANGE": 0.80,
    "BEAR_TREND": 0.60,
    "HIGH_VOL": 0.55,
}


def dynamic_heat_cap(regime, drawdown=0.0):
    cap = REGIME_HEAT_CAP.get(regime, PORTFOLIO_HEAT_CAP)
    if drawdown < -0.05:
        cap *= 0.7
    elif drawdown < -0.03:
        cap *= 0.85
    return round(cap, 2)


def avoid_entry_window(symbol):
    if symbol in CRYPTO:
        return False, ""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_left = (close_time - now).total_seconds() / 60
        if 0 <= minutes_left <= 15:
            return True, f"last {minutes_left:.0f}min before close — high slippage"
    except Exception:
        pass
    return False, ""


def should_scale_in(holding, conviction, df, max_positions=14):
    try:
        entry = float(holding.avg_entry_price)
        current = float(holding.current_price)
        pnl_pct = (current / entry - 1) * 100
        if pnl_pct < 2:
            return False, "not enough profit to scale"
        if conviction < 0.80:
            return False, f"conviction {conviction:.0%} too low for scale-in"
        market_value = float(holding.market_value)
        current_equity = entry + market_value
        if market_value > 0.08 * current_equity:
            return False, "position already large enough"
        if len(df) < 20:
            return False, "insufficient data"
        close = df["Close"]
        fast = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
        slow = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
        if fast < slow:
            return False, "EMA bearish — not scaling into weakening trend"
        return True, f"scaling in: +{pnl_pct:.1f}% profit, conviction {conviction:.0%}"
    except Exception:
        return False, "error"


def get_sector(symbol):
    for sector, syms in SECTORS.items():
        if symbol in syms:
            return sector
    return "other"


def total_value(positions):
    return sum(abs(float(p.market_value)) for p in positions)


def sector_exposure(positions, equity=100000):
    exposure = {}
    for p in positions:
        sec = get_sector(p.symbol)
        exposure[sec] = exposure.get(sec, 0) + abs(float(p.market_value))
    denom = equity if equity > 0 else 1
    return {k: v / denom for k, v in exposure.items()}


def sector_blocked(positions, new_symbol, equity=100000):
    sec = get_sector(new_symbol)
    current = sector_exposure(positions, equity=equity)
    return current.get(sec, 0) > SECTOR_CAP


def sector_room(positions, new_symbol, equity=100000):
    sec = get_sector(new_symbol)
    current = sector_exposure(positions, equity=equity)
    used = current.get(sec, 0)
    room = max(0.0, min(1.0, (SECTOR_CAP - used) / max(SECTOR_CAP, 1e-6)))
    return room


def conviction_score(symbol, cfg, tf_agreement=1.0, news_verdict="NEUTRAL",
                     ensemble_agree=0.5, regime=None):
    score = 0.5
    test_score = cfg.get("test_score", 0)
    if test_score > 10:
        score += 0.20
    elif test_score > 3:
        score += 0.15
    elif test_score > 1:
        score += 0.05
    elif test_score < 0:
        score -= 0.20

    score += (tf_agreement - 0.5) * 0.3

    if news_verdict == "BULLISH":
        score += 0.08
    elif news_verdict == "BEARISH":
        score -= 0.25

    if tf_agreement < 0.5:
        score -= 0.15

    if ensemble_agree >= 0.9:
        score += 0.10
    elif ensemble_agree >= 0.67:
        score += 0.05
    elif ensemble_agree < 0.34:
        score -= 0.10

    if regime:
        rm = regime_multiplier(regime)
        score *= rm

    kelly = kelly_fraction(symbol)
    if kelly is not None:
        score = score * 0.7 + kelly * 0.3

    lc = loss_count(symbol, regime or "")
    if lc == 1:
        score -= 0.05
    elif lc == 2:
        score -= 0.10
    elif lc >= 3:
        score -= 0.25

    return max(0.0, min(1.0, score))


def recent_momentum(df, days=5):
    try:
        close = df["Close"]
        if len(close) < days + 1:
            return 0.0
        ret = close.iloc[-1] / close.iloc[-days - 1] - 1
        return round(ret, 4)
    except Exception:
        return 0.0


def multi_tf_signal(daily_df, h4_df, cfg):
    try:
        daily_close = daily_df["Close"]
        h4_close = h4_df["Close"]
        if len(daily_close) < 26 or len(h4_close) < 26:
            return "unknown", 0.5
        daily_ema9 = daily_close.ewm(span=9).mean().iloc[-1]
        daily_ema21 = daily_close.ewm(span=21).mean().iloc[-1]
        h4_ema9 = h4_close.ewm(span=9).mean().iloc[-1]
        h4_ema21 = h4_close.ewm(span=21).mean().iloc[-1]
        daily_bull = daily_ema9 > daily_ema21
        h4_bull = h4_ema9 > h4_ema21
        if daily_bull and h4_bull:
            return "BULL", 1.0
        elif not daily_bull and not h4_bull:
            return "BEAR", 1.0
        else:
            return "MIXED", 0.5
    except Exception:
        return "unknown", 0.5


def update_regime_strategy_perf(symbol, strategy, regime, pnl_pct):
    try:
        conn = init_db()
        row = conn.execute("""SELECT avg_pnl, win_rate, n_trades FROM regime_strategy_perf
                              WHERE regime=? AND strategy=? AND symbol=?""",
                           (regime, strategy, symbol)).fetchone()
        if row:
            old_avg, old_wr, old_n = row
            new_n = old_n + 1
            new_avg = (old_avg * old_n + pnl_pct) / new_n
            wins = old_wr * old_n + (1 if pnl_pct > 0 else 0)
            new_wr = wins / new_n
            conn.execute("""UPDATE regime_strategy_perf SET avg_pnl=?, win_rate=?,
                            n_trades=?, last_updated=? WHERE regime=? AND strategy=? AND symbol=?""",
                         (new_avg, new_wr, new_n, datetime.now().isoformat(timespec="seconds"),
                          regime, strategy, symbol))
        else:
            conn.execute("""INSERT INTO regime_strategy_perf(regime,strategy,symbol,
                            avg_pnl,win_rate,n_trades,last_updated) VALUES(?,?,?,?,?,?,?)""",
                         (regime, strategy, symbol, pnl_pct,
                          1.0 if pnl_pct > 0 else 0.0, 1,
                          datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
    except Exception:
        pass


def best_strategy_for_regime(regime, symbol=None):
    try:
        conn = init_db()
        if symbol:
            rows = conn.execute("""SELECT strategy, avg_pnl, win_rate, n_trades
                                   FROM regime_strategy_perf WHERE regime=? AND symbol=?
                                   AND n_trades >= 2 ORDER BY avg_pnl DESC""",
                                (regime, symbol)).fetchall()
        else:
            rows = conn.execute("""SELECT strategy, AVG(avg_pnl), AVG(win_rate), SUM(n_trades)
                                   FROM regime_strategy_perf WHERE regime=?
                                   AND n_trades >= 2 GROUP BY strategy ORDER BY AVG(avg_pnl) DESC""",
                                (regime,)).fetchall()
        conn.close()
        if rows:
            return rows[0][0], rows[0][1]
        return None, None
    except Exception:
        return None, None


def kelly_fraction(symbol):
    try:
        conn = init_db()
        row = conn.execute("SELECT win_rate, avg_win, avg_loss, n_trades FROM kelly_data WHERE symbol=?",
                           (symbol,)).fetchone()
        conn.close()
        if not row or row[3] < 5:
            return None
        wr, avg_win, avg_loss, n = row
        if avg_loss >= 0 or wr <= 0 or wr >= 1:
            return None
        b = abs(avg_win / avg_loss)
        q = 1 - wr
        kelly = (b * wr - q) / b
        return max(0.0, min(0.25, kelly * 0.5))
    except Exception:
        return None


def update_kelly_data(symbol, pnl_pct):
    try:
        conn = init_db()
        row = conn.execute("SELECT win_rate, avg_win, avg_loss, n_trades FROM kelly_data WHERE symbol=?",
                           (symbol,)).fetchone()
        if row:
            old_wr, old_aw, old_al, old_n = row
            new_n = old_n + 1
            new_wr = (old_wr * old_n + (1 if pnl_pct > 0 else 0)) / new_n
            if pnl_pct > 0:
                new_aw = (old_aw * old_n + pnl_pct) / new_n if old_aw else pnl_pct
                new_al = old_al
            else:
                new_aw = old_aw
                new_al = (old_al * old_n + abs(pnl_pct)) / new_n if old_al else abs(pnl_pct)
            conn.execute("UPDATE kelly_data SET win_rate=?, avg_win=?, avg_loss=?, n_trades=? WHERE symbol=?",
                         (new_wr, new_aw, new_al, new_n, symbol))
        else:
            conn.execute("INSERT INTO kelly_data(symbol,win_rate,avg_win,avg_loss,n_trades) VALUES(?,?,?,?,?)",
                         (symbol, 1.0 if pnl_pct > 0 else 0.0,
                          pnl_pct if pnl_pct > 0 else 0, abs(pnl_pct) if pnl_pct < 0 else 0, 1))
        conn.commit()
        conn.close()
    except Exception:
        pass


def analyze_trade_outcome(symbol, strategy, entry, exit_price, regime):
    try:
        if not entry or not exit_price or float(entry) <= 0:
            return None
        pnl_pct = (float(exit_price) / float(entry) - 1) * 100
        conn = init_db()
        win_count = conn.execute(
            "SELECT COUNT(*) FROM trade_pnl WHERE symbol=? AND strategy=? AND pnl_pct > 0",
            (symbol, strategy)).fetchone()[0]
        loss_count = conn.execute(
            "SELECT COUNT(*) FROM trade_pnl WHERE symbol=? AND strategy=? AND pnl_pct < 0",
            (symbol, strategy)).fetchone()[0]
        total = win_count + loss_count
        win_rate = win_count / total if total > 0 else 0
        lesson = ""
        if pnl_pct < -3:
            lesson = f"BIG LOSS: {symbol} {strategy} lost {pnl_pct:+.1f}% in {regime}"
        elif pnl_pct > 5:
            lesson = f"GOOD WIN: {symbol} {strategy} gained {pnl_pct:+.1f}% in {regime}"
        if total >= 3 and win_rate < 0.33:
            lesson += f" | STRATEGY FAILING: {win_rate:.0%} win rate over {total} trades"
        conn.execute("INSERT INTO lessons(ts,text) VALUES(?,?)",
                     (datetime.now().isoformat(timespec="seconds"), lesson))
        conn.commit()
        conn.close()
        try:
            tune_config(symbol, pnl_pct, regime=regime)
        except Exception:
            pass
        return {"pnl": pnl_pct, "win_rate": win_rate, "total": total, "lesson": lesson}
    except Exception:
        return None


def strategy_performance(symbol=None, min_trades=2):
    try:
        conn = init_db()
        if symbol:
            rows = conn.execute(
                """SELECT strategy, COUNT(*) as n, AVG(pnl_pct) as avg_pnl,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins
                   FROM trade_pnl WHERE symbol=? GROUP BY strategy HAVING COUNT(*) >= ?""",
                (symbol, min_trades)).fetchall()
        else:
            rows = conn.execute(
                """SELECT strategy, COUNT(*) as n, AVG(pnl_pct) as avg_pnl,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins
                   FROM trade_pnl GROUP BY strategy HAVING COUNT(*) >= ?""",
                (min_trades,)).fetchall()
        conn.close()
        results = {}
        for strat, n, avg_pnl, wins in rows:
            wr = wins / n if n > 0 else 0
            grade = "A" if wr >= 0.7 and avg_pnl > 1 else \
                    "B" if wr >= 0.5 and avg_pnl > 0 else \
                    "C" if avg_pnl > 0 else "D"
            results[strat] = {"n": n, "avg_pnl": round(avg_pnl, 2),
                              "win_rate": round(wr, 2), "grade": grade}
        return results
    except Exception:
        return {}


def tune_config(symbol, pnl_pct, regime=None):
    """Auto-tune a symbol's adaptive thresholds from its real trade outcome.

    After a closed trade: tighten entries after losses (more selective),
    relax modestly after wins. Bounded by TUNE_BOUNDS/TUNE_MAX_DELTA so the
    thresholds can never drift into absurd territory. Only nudges once enough
    trades exist so it doesn't chase single-trade noise.
    """
    try:
        cfg = load_config(symbol)
        if not cfg:
            return None
        if not isinstance(cfg.get("adaptive"), dict):
            return None
        # Require enough history before tuning (don't chase noise).
        perf = strategy_performance(symbol)
        cur_label = cfg.get("label", "")
        cur = (perf or {}).get(cur_label)
        n = (cur.get("n", 0) if cur else 0)
        need = 3
        tunable = []
        mode = cfg.get("mode", "")
        for key in cfg.get("adaptive", {}):
            if key not in TUNE_BOUNDS:
                continue
            # Only tune params that this strategy actually uses.
            if key == "rsi_buy" and mode not in ("rsi", "stoch"):
                continue
            if key == "rsi_sell" and mode not in ("rsi", "stoch"):
                continue
            if key == "ema_fast" and mode != "ema_cross":
                continue
            if key == "ema_slow" and mode != "ema_cross":
                continue
            if key == "atr_mult" and mode != "atr_breakout":
                continue
            tunable.append(key)
        if not tunable:
            return None
        # Direction: losses tighten entries, wins relax them.
        win = pnl_pct > 0
        gained = pnl_pct > 4
        lost_big = pnl_pct < -3
        # If we already have evidence the strategy is failing, nudge harder.
        sluggish = bool((n >= need) and cur and cur["win_rate"] < 0.40)
        step = 0
        if lost_big or sluggish:
            step = -2 if not sluggish else -3
        elif win and not gained:
            step = 0
        elif gained:
            step = 1
        else:
            step = 0
        if step == 0 and n < need:
            return None
        tuned = cfg["adaptive"].get("tuned", {})
        changes = []
        for key in tunable:
            if step == 0:
                continue
            # tighten after losses (lower rsi_buy = more selective dip),
            # relax only after a big win.
            delta = step  # same delta for all tunable params for this symbol
            old = tuned.get(key, 0) or 0
            new_delta = old + delta
            if key == "rsi_buy":
                new_delta = max(-12, min(6, new_delta))
            elif key == "rsi_sell":
                new_delta = max(-6, min(12, new_delta))
            elif key == "atr_mult":
                new_delta = max(-0.5, min(0.5, new_delta))
            else:  # ema params
                new_delta = max(-6, min(6, new_delta))
            if new_delta != old:
                tuned[key] = round(new_delta, 3)
                changes.append(f"{key}{new_delta:+.0f}")
        if not changes:
            return None
        cfg["adaptive"]["tuned"] = tuned
        cfg["updated"] = datetime.now().isoformat(timespec="seconds")
        save_config(cfg)
        lesson = (f"[{datetime.now():%Y-%m-%d %H:%M}] AUTO-TUNE {symbol} "
                  f"{cfg.get('label','?')} pnl={pnl_pct:+.1f}% (wr={(cur['win_rate']*100) if cur else 0:.0f}%, "
                  f"n={n}): {'tighten' if step<0 else 'relax'} {', '.join(changes)}")
        conn = init_db()
        conn.execute("INSERT INTO lessons(ts,text) VALUES(?,?)",
                     (datetime.now().isoformat(timespec="seconds"), lesson))
        conn.commit()
        conn.close()
        print(f"  AUTO-TUNE: {lesson}" if not regime else f"  AUTO-TUNE [{regime}]: {lesson}")
        return lesson
    except Exception as e:
        return None


# Losing-strategy safety exit: when a held symbol's strategy is statistically
# failing, cut losses earlier instead of trusting its weak exit signal.
FAILING_WR = 0.40
FAILING_MIN_TRADES = 5
FAILING_STOP = 0.04   # tighter stop for failing strategies
FAILING_FLAT_DAYS = 6  # exit a failing-strategy flat position sooner


def strategy_quality(symbol):
    """Return (grade, win_rate, n_trades) of the symbol's current strategy.

    Returns (None, None, 0) when there's not enough evidence yet."""
    try:
        cfg = load_config(symbol)
        if not cfg:
            return None, None, 0
        perf = strategy_performance(symbol)
        cur_label = cfg.get("label", "")
        info = (perf or {}).get(cur_label)
        if not info:
            return None, None, 0
        return info["grade"], info["win_rate"], info["n"]
    except Exception:
        return None, None, 0


def strategy_is_failing(symbol):
    """True when the held symbol's current strategy has proven losing odds.

    Either graded D/F outright, or a win-rate below FAILING_WR with enough
    sampled trades to be meaningful."""
    grade, wr, n = strategy_quality(symbol)
    if grade in ("D", "F"):
        return True
    if wr is not None and n >= FAILING_MIN_TRADES and wr < FAILING_WR:
        return True
    return False


def auto_adjust_strategy(symbol):
    try:
        perf = strategy_performance(symbol)
        if not perf:
            return None, "no data"
        cfg = load_config(symbol)
        if not cfg:
            return None, "no config"
        current = cfg.get("label", "")
        current_perf = perf.get(current)
        if not current_perf:
            return None, f"{current} has no trade history"
        # Never switch a strategy while we hold the symbol — swapping the exit logic
        # under an open position could strand or mis-exit the trade.
        try:
            from alpaca.trading.client import TradingClient
            _k, _s = api_creds()
            if not _k or not _s:
                return None, "no api creds"
            _client = TradingClient(_k, _s, paper=True)
            _hp = {p.symbol for p in _client.get_all_positions()}
            if alpaca_sym(symbol) in _hp or symbol in _hp:
                return None, f"{symbol} currently held — deferring strategy switch"
        except Exception:
            pass
        # Replace strategies that are genuinely bad: grade D, OR low win-rate
        # (lose more often than they win) once we have enough evidence.
        grade = current_perf["grade"]
        low_wr = current_perf["win_rate"] < 0.40 and current_perf["n"] >= 5
        trigger = grade in ("D", "F") or low_wr
        if trigger:
            candidates_list = candidates()
            best = None
            best_score = -99
            for c in candidates_list:
                c_label = c.get("label", "")
                if c_label == current:
                    continue
                c_perf = perf.get(c_label)
                if c_perf and c_perf["grade"] in ("A", "B"):
                    score = c_perf["avg_pnl"] * c_perf["win_rate"]
                    if score > best_score:
                        best_score = score
                        best = c
            if best:
                old_label = current
                why = "grade D" if grade in ("D", "F") else f"win-rate {current_perf['win_rate']:.0%}"
                cfg.update(best)
                cfg["symbol"] = symbol
                save_config(cfg)
                lesson = (f"[{datetime.now():%Y-%m-%d %H:%M}] AUTO-ADJUST {symbol}: "
                          f"{old_label} ({why}, {current_perf['avg_pnl']:+.1f}%) -> "
                          f"{best.get('label')} (grade {perf[best.get('label', '')].get('grade', '?')})")
                conn = init_db()
                conn.execute("INSERT INTO lessons(ts,text) VALUES(?,?)",
                             (datetime.now().isoformat(timespec="seconds"), lesson))
                conn.commit()
                conn.close()
                print(f"  AUTO-ADJUST: {lesson}")
                return best.get("label"), lesson
            return None, f"{current} is bad ({why}) but no better strategy found"
        return None, f"{current} is grade {grade} (wr {current_perf['win_rate']:.0%}) — ok"
    except Exception as e:
        return None, str(e)


def detect_divergence(df, lookback=14):
    try:
        close = df["Close"]
        if len(close) < lookback + 5:
            return "none"
        rsi = close.diff().clip(lower=0).ewm(span=lookback).mean() / \
              abs(close.diff()).ewm(span=lookback).mean() * 100
        price_higher = close.iloc[-1] > close.iloc[-lookback]
        rsi_higher = rsi.iloc[-1] > rsi.iloc[-lookback]
        if price_higher and not rsi_higher:
            return "bearish"
        if not price_higher and rsi_higher:
            return "bullish"
        return "none"
    except Exception:
        return "none"


def hash_pattern(df, regime):
    try:
        close = df["Close"]
        rsi = close.diff().clip(lower=0).ewm(span=14).mean() / \
              abs(close.diff()).ewm(span=14).mean() * 100
        vol = realized_vol(close)
        trend = "up" if close.iloc[-1] > close.iloc[-20] else "down"
        parts = [regime, trend, f"rsi{int(rsi.iloc[-1])}", f"vol{vol:.3f}"]
        return "|".join(parts)
    except Exception:
        return "unknown"


def check_pattern_memory(symbol, pattern_hash, min_trades=3):
    try:
        conn = init_db()
        row = conn.execute("""SELECT AVG(outcome_pnl), COUNT(*) FROM pattern_memory
                              WHERE symbol=? AND pattern_hash=?""",
                           (symbol, pattern_hash)).fetchone()
        conn.close()
        if not row or row[1] < min_trades:
            return None
        return row[0]
    except Exception:
        return None


def log_pattern(symbol, pattern_hash, outcome_pnl, regime):
    try:
        conn = init_db()
        conn.execute("""INSERT INTO pattern_memory(ts,symbol,pattern_hash,conditions,outcome_pnl,regime)
                        VALUES(?,?,?,?,?,?)""",
                     (datetime.now().isoformat(timespec="seconds"), symbol, pattern_hash,
                      "", outcome_pnl, regime))
        conn.commit()
        conn.close()
    except Exception:
        pass


def learn_on_exit(symbol, entry_price, exit_price, regime, pattern_hash=None):
    pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
    update_kelly_data(symbol, pnl_pct)
    if pattern_hash:
        log_pattern(symbol, pattern_hash, pnl_pct, regime)
    if pnl_pct < -3:
        _label = ""
        try:
            conn = init_db()
            row = conn.execute("SELECT strategy FROM trades WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                               (symbol,)).fetchone()
            conn.close()
            if row:
                _label = row[0] or ""
        except Exception:
            pass
        log_loss_conditions(symbol, _label, regime, pnl_pct, f"exit={exit_price:.2f}")


def rebalance_candidates(positions):
    if len(positions) < 2:
        return []
    scored = []
    for p in positions:
        try:
            entry = float(p.avg_entry_price)
            cur = float(p.current_price)
            if entry > 0:
                ret = (cur / entry - 1)
                scored.append((p, ret))
        except Exception:
            pass
    scored.sort(key=lambda x: x[1])
    worst = scored[:len(scored) // 3] if len(scored) >= 3 else []
    return [p for p, r in worst if r < -0.02]


def ensemble_signal(df, cfg, symbol):
    try:
        top = candidates()
        top = [c for c in top if c.get("mode") == cfg.get("mode")][:3]
        if len(top) < 2:
            return cfg.get("label"), 1.0, "single"
        votes = []
        for c in top:
            rets = _strat_rets(df, c)
            if len(rets) < 5:
                continue
            recent = rets.iloc[-5:]
            in_trade = bool(recent.iloc[-1] != 0)
            votes.append((c.get("label", "?"), in_trade))
        if not votes:
            return cfg.get("label"), 1.0, "no-votes"
        buy_votes = sum(1 for _, v in votes if v)
        total = len(votes)
        agreement = buy_votes / total if total > 0 else 0
        best = max(votes, key=lambda x: (x[1], 0))[0]
        consensus = "CONSENSUS" if agreement >= 0.67 else "SPLIT"
        return best, agreement, consensus
    except Exception:
        return cfg.get("label"), 1.0, "error"


def momentum_score(df):
    try:
        close = df["Close"]
        ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0
        ret_20d = (close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0
        vol = close.pct_change().rolling(20).std().iloc[-1] * (252 ** 0.5) if len(close) > 20 else 0.2
        vol_norm = max(vol, 0.01)
        raw = ret_5d * 0.6 + ret_20d * 0.4
        adjusted = raw / vol_norm
        return round(max(min(adjusted, 2.0), -2.0), 3)
    except Exception:
        return 0.0


def trailing_profit_targets(entry_price, current_price, side="long", atr=None):
    try:
        if side == "long":
            gain = current_price / entry_price - 1
        else:
            gain = 1 - current_price / entry_price
        if atr is None or atr <= 0:
            atr = entry_price * 0.015
        atr_stop_dist = atr * 2.0
        if gain < 0.01:
            return None, None
        elif gain < 0.03:
            stop = entry_price * 1.005
            target = None
        elif gain < 0.06:
            stop = current_price - atr_stop_dist * 0.5
            target = None
        elif gain < 0.12:
            stop = current_price - atr_stop_dist
            target = entry_price * 1.15
        elif gain < 0.25:
            stop = current_price - atr_stop_dist * 1.5
            target = entry_price * 1.30
        else:
            stop = current_price - atr_stop_dist * 2.0
            target = entry_price * 1.50
        stop = max(stop, entry_price * 1.005)
        return round(stop, 2), round(target, 2) if target else None
    except Exception:
        return None, None


def log_incident(kind, detail):
    try:
        conn = init_db()
        conn.execute("""CREATE TABLE IF NOT EXISTS incidents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, kind TEXT, detail TEXT)""")
        conn.execute("INSERT INTO incidents(ts,kind,detail) VALUES(?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), kind, str(detail)[:200]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def incident_stats(days=7):
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        conn = init_db()
        rows = conn.execute("""SELECT kind, COUNT(*) FROM incidents WHERE ts >= ?
                               GROUP BY kind ORDER BY COUNT(*) DESC""", (since,)).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def readiness_score():
    conn = init_db()
    checks = []

    first = conn.execute("SELECT MIN(date) FROM equity_history").fetchone()[0]
    try:
        days = (datetime.now() - datetime.fromisoformat(first)).days if first else 0
    except ValueError:
        days = 0
    checks.append(("Paper runtime", min(days / 42, 1.0) * 15,
                   f"{days} days of 42 needed"))

    n_closed = conn.execute("SELECT COUNT(*) FROM trade_pnl").fetchone()[0]
    checks.append(("Closed trades", min(n_closed / 25, 1.0) * 25,
                   f"{n_closed} of 25 needed"))

    last10 = conn.execute("""SELECT AVG(pnl_pct) FROM
        (SELECT pnl_pct FROM trade_pnl ORDER BY id DESC LIMIT 10)""").fetchone()[0]
    if last10 is None:
        checks.append(("Recent performance", 0.0, "no closed trades yet"))
    elif last10 > 0:
        checks.append(("Recent performance", 15.0, f"last 10 avg {last10:+.2f}%"))
    elif last10 > -2:
        checks.append(("Recent performance", 7.0, f"last 10 avg {last10:+.2f}% (weak)"))
    else:
        checks.append(("Recent performance", 0.0, f"last 10 avg {last10:+.2f}% (losing)"))

    row = conn.execute("""SELECT MIN(equity/peak), MAX(drawdown_pct)
                          FROM equity_history""").fetchone()
    worst_dd = row[0] - 1 if row and row[0] else 0.0
    cur_eq = conn.execute("SELECT equity FROM equity_history ORDER BY date DESC LIMIT 1").fetchone()
    peak_eq = conn.execute("SELECT MAX(peak) FROM equity_history").fetchone()[0]
    near_peak = bool(cur_eq and peak_eq and float(cur_eq[0]) >= peak_eq * 0.95)
    if worst_dd > -0.12:
        checks.append(("Drawdown resilience", 15.0, f"worst dip {worst_dd*100:.1f}%, handled"))
    elif near_peak:
        checks.append(("Drawdown resilience", 10.0, f"dipped {worst_dd*100:.1f}%, recovered"))
    else:
        checks.append(("Drawdown resilience", 0.0, f"currently {worst_dd*100:.1f}% down"))

    srow = conn.execute("""SELECT COUNT(*), ABS(AVG(slippage_bps)) FROM trades
                           WHERE slippage_bps IS NOT NULL""").fetchone()
    sn, savg = srow[0], srow[1] or 99
    if sn >= 10 and savg < 5:
        checks.append(("Execution quality", 15.0, f"{sn} fills, {savg:.1f} bps slip"))
    elif sn >= 10:
        checks.append(("Execution quality", 8.0, f"{sn} fills, {savg:.1f} bps slip (high)"))
    else:
        checks.append(("Execution quality", 0.0, f"{sn} of 10 measured fills"))

    diverse = conn.execute("SELECT COUNT(DISTINCT strategy) FROM trade_pnl").fetchone()[0]
    checks.append(("Strategy variety", min(diverse / 4, 1.0) * 15,
                   f"{diverse} of 4 strategies traded live"))
    conn.close()

    total = round(min(sum(c[1] for c in checks), 100), 1)
    return {"score": total, "checks": checks}


def update_position_peak(symbol, current_price, entry_price):
    conn = init_db()
    row = conn.execute("SELECT peak FROM position_peaks WHERE symbol=?", (symbol,)).fetchone()
    peak = max(current_price, row[0] if row else entry_price)
    conn.execute("INSERT OR REPLACE INTO position_peaks(symbol,peak,entry) VALUES(?,?,?)",
                 (symbol, peak, entry_price))
    conn.commit()
    conn.close()
    return peak


def clear_position_peak(symbol):
    conn = init_db()
    conn.execute("DELETE FROM position_peaks WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


def profit_lock_hit(entry, current, peak):
    if entry <= 0 or peak <= 0:
        return False
    return current >= entry * (1 + TRAIL_ACTIVATE) and current <= peak * (1 - TRAIL_GIVEBACK)
    c.execute("SELECT COUNT(*) FROM journal")
    if c.fetchone()[0] == 0:
        try:
            with open(JOURNAL_FILE, encoding="utf-8") as f:
                rows = list(csv.reader(f))[1:]
            for row in rows:
                if len(row) >= 5:
                    try:
                        c.execute("INSERT INTO journal(ts,symbol,price,action,detail,equity) VALUES(?,?,?,?,?,?)",
                                  (row[0], row[1], float(row[2] or 0), row[3], row[4],
                                   float(row[5]) if len(row) > 5 and row[5] else None))
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass
    c.execute("SELECT COUNT(*) FROM equity_history")
    if c.fetchone()[0] == 0:
        try:
            with open("equity_history.csv", encoding="utf-8") as f:
                rows = list(csv.reader(f))[1:]
            for row in rows:
                if len(row) >= 4:
                    c.execute("INSERT OR REPLACE INTO equity_history VALUES(?,?,?,?)",
                              (row[0], float(row[1]), float(row[2]), float(row[3])))
        except FileNotFoundError:
            pass
    conn.commit()
    return conn


def years(index):
    return max((index[-1] - index[0]).days / 365.25, 1e-9)


def _annualized(rets, mask=None):
    r = rets[mask] if mask is not None else rets
    r = r.dropna()
    if len(r) < 20:
        return 0.0, 0.0
    mu, sd = float(r.mean()) * 252, float(r.std()) * np.sqrt(252)
    return mu, sd


def sharpe_like(rets, mask=None):
    mu, sd = _annualized(rets, mask)
    return mu / sd if sd > 0 else 0.0


def sortino(rets, mask=None):
    r = rets[mask] if mask is not None else rets
    r = r.dropna()
    if len(r) < 20:
        return 0.0
    downside = r[r < 0]
    dd = float(downside.std()) * np.sqrt(252)
    mu = float(r.mean()) * 252
    return mu / dd if dd > 0 else (9.9 if mu > 0 else 0.0)


def profit_factor(rets, mask=None):
    r = rets[mask] if mask is not None else rets
    r = r.dropna()
    gains = float(r[r > 0].sum())
    losses = abs(float(r[r < 0].sum()))
    return round(gains / losses, 2) if losses > 0 else 9.9


def max_dd_stats(equity):
    dd_series = equity / equity.cummax() - 1
    mdd = float(dd_series.min())
    longest = 0
    cur = 0
    for v in dd_series:
        cur = cur + 1 if v < -1e-9 else 0
        longest = max(longest, cur)
    return mdd, longest


def smart_score(equity, strat_rets):
    if len(equity) < 30:
        return -99.0
    mdd, dd_days = max_dd_stats(equity)
    if mdd == 0:
        mdd = -1e-4
    cagr = ((float(equity.iloc[-1]) / CAPITAL) ** (1 / years(equity.index))) - 1
    calmar = cagr / abs(mdd)
    sr = sortino(strat_rets)
    pf = profit_factor(strat_rets)
    dd_penalty = min(dd_days / 180.0, 1.5)
    pf_gate = 0.0 if pf >= 1.15 else -2.0
    trade_count = int((strat_rets != 0).sum())
    total_days = len(strat_rets)
    trade_freq = trade_count / total_days if total_days > 0 else 0
    freq_penalty = 0.0
    if trade_freq < 0.02:
        freq_penalty = -10.0
    elif trade_freq < 0.05:
        freq_penalty = -3.0
    return round(calmar + 0.25 * min(sr, 8) + 0.4 * min(pf, 3) - dd_penalty + pf_gate + freq_penalty, 3)


def _vol10(close):
    return close.pct_change().rolling(10).std() * np.sqrt(252)


def _atr14(df):
    h, l, c = df["High"], df["Low"], df["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


REGIMES = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL", "QUIET_RANGE"]


def detect_regime_series(df):
    close = df["Close"]
    sma200 = close.rolling(200).mean()
    mid50 = close.rolling(50).mean()
    slope = mid50.diff(15)
    vol = close.pct_change().rolling(20).std() * np.sqrt(252)
    vol_rank = vol.rolling(min(252, max(len(vol) - 1, 5))).apply(lambda x: (x <= x[-1]).mean(), raw=True)

    up = (close > sma200) & (slope > 0)
    down = (close < sma200) & (slope < 0)
    wild = vol_rank.fillna(0.5) > 0.75

    reg = pd.Series("QUIET_RANGE", index=close.index)
    reg[wild] = "HIGH_VOL"
    reg[up & ~wild] = "BULL_TREND"
    reg[down & ~wild] = "BEAR_TREND"
    return reg


def current_regime(df):
    try:
        return str(detect_regime_series(df).iloc[-1])
    except Exception:
        return "UNKNOWN"


def _raw_position(df, cfg):
    mode = cfg["mode"]
    close = df["Close"]
    if mode == "hold":
        return pd.Series(1.0, index=close.index)

    s = pd.Series(np.nan, index=close.index)

    if mode == "donchian":
        upper = close.rolling(cfg["entry"]).max().shift(1)
        lower = close.rolling(max(int(cfg["exit"]), 2)).min().shift(1)
        s[close > upper] = 1
        s[close < lower] = 0

    elif mode == "rsi":
        d = close.diff()
        gain = d.clip(lower=0)
        loss = -d.clip(upper=0)
        ag = gain.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        al = loss.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        rsi = 100 - 100 / (1 + ag / al)
        s[rsi < cfg["buy_below"]] = 1
        s[rsi > cfg["sell_above"]] = 0
        if cfg.get("volume_confirm"):
            vok = df["Volume"] > 1.5 * df["Volume"].rolling(20).mean()
            s[(rsi < cfg["buy_below"]) & ~vok.fillna(False)] = np.nan

    elif mode == "bollinger":
        mid = close.rolling(20).mean()
        band = 2 * close.rolling(20).std()
        s[close < mid - band] = 1
        s[close > mid] = 0

    elif mode == "macd":
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        s[macd > signal] = 1
        s[macd < signal] = 0

    elif mode == "momentum":
        mom = close.pct_change(126)
        s[mom > 0] = 1
        s[mom < 0] = 0

    elif mode == "atr_breakout":
        atr = _atr14(df)
        upper = close.rolling(cfg["entry"]).max().shift(1) + cfg["mult"] * atr.shift(1)
        lower = close.rolling(max(int(cfg["exit"]), 2)).min().shift(1) - cfg["mult"] * atr.shift(1)
        s[close > upper] = 1
        s[close < lower] = 0

    elif mode == "ema_cross":
        fast = close.ewm(span=cfg["fast"], adjust=False).mean()
        slow = close.ewm(span=cfg["slow"], adjust=False).mean()
        s[fast > slow] = 1
        s[fast < slow] = 0

    elif mode == "stoch":
        lo = close.rolling(cfg["k"]).min()
        hi = close.rolling(cfg["k"]).max()
        k = 100 * (close - lo) / (hi - lo)
        s[k < cfg["buy_below"]] = 1
        s[k > cfg["sell_above"]] = 0

    elif mode == "adx":
        up = df["High"].diff()
        dn = -df["Low"].diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=close.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=close.index)
        tr = pd.concat([df["High"] - df["Low"],
                        (df["High"] - close.shift()).abs(),
                        (df["Low"] - close.shift()).abs()], axis=1).max(axis=1)
        atr_w = tr.ewm(alpha=1 / 14, min_periods=14).mean()
        pdi = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        mdi = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
        adx = dx.ewm(alpha=1 / 14, min_periods=14).mean()
        s[(adx > cfg["threshold"]) & (pdi > mdi)] = 1
        s[pdi < mdi] = 0

    if cfg.get("trend"):
        ok_trend = close > close.rolling(int(cfg["trend"])).mean()
        entries = s == 1
        s[entries & ~ok_trend.fillna(True)] = np.nan

    if cfg.get("vol_mult"):
        vol = _vol10(close)
        calm = vol <= vol.rolling(60).median().fillna(vol) * cfg["vol_mult"]
        entries = s == 1
        s[entries & ~calm.fillna(True)] = np.nan

    return s.ffill().fillna(0)


def position_for(df, cfg, shift=True):
    pos = _raw_position(df, cfg)
    return pos.shift(1).fillna(0) if shift else pos


def equity_curve(df, cfg):
    rets = df["Close"].pct_change().fillna(0)
    return CAPITAL * (1 + position_for(df, cfg, shift=True) * rets).cumprod()


def _strat_rets(df, cfg):
    rets = df["Close"].pct_change().fillna(0)
    return position_for(df, cfg, shift=True) * rets


def candidates():
    cands = [{"mode": "hold", "label": "BUY&HOLD"}]
    for e in [10, 15, 20, 30, 40, 55]:
        cands.append({"mode": "donchian", "entry": e, "exit": max(e // 2, 5),
                      "label": f"Donchian {e}/{max(e // 2, 5)}"})
    cands.append({"mode": "donchian", "entry": 20, "exit": 10, "trend": 200,
                  "label": "Donch20/10+trend200"})
    for p, b, sa in [(2, 10, 65), (2, 5, 60), (14, 25, 55), (3, 15, 60),
                     (2, 15, 70), (4, 20, 60), (5, 30, 65)]:
        cands.append({"mode": "rsi", "period": p, "buy_below": b, "sell_above": sa,
                      "label": f"RSI{p} <{b}/{sa}"})
    cands.append({"mode": "rsi", "period": 2, "buy_below": 5, "sell_above": 60,
                  "trend": 200, "label": "RSI2 <5/60 +trend200"})
    cands.append({"mode": "rsi", "period": 2, "buy_below": 5, "sell_above": 60,
                  "trend": 200, "vol_mult": 1.25, "label": "RSI2 <5/60 tr+vol"})
    cands.append({"mode": "rsi", "period": 3, "buy_below": 15, "sell_above": 60,
                  "trend": 100, "label": "RSI3 <15/60 +trend100"})
    cands.append({"mode": "rsi", "period": 14, "buy_below": 25, "sell_above": 55,
                  "volume_confirm": True, "label": "RSI14 <25/55 +volConfirm"})
    cands.append({"mode": "rsi", "period": 2, "buy_below": 10, "sell_above": 60,
                  "volume_confirm": True, "trend": 200,
                  "label": "RSI2 <10/60 vol+trend"})
    cands.append({"mode": "bollinger", "label": "Bollinger dip"})
    cands.append({"mode": "bollinger", "trend": 200, "label": "Bollinger dip +trend200"})
    cands.append({"mode": "macd", "label": "MACD cross"})
    cands.append({"mode": "macd", "trend": 200, "label": "MACD cross +trend200"})
    cands.append({"mode": "momentum", "label": "Momentum 6m"})
    cands.append({"mode": "momentum", "trend": 200, "vol_mult": 1.25,
                  "label": "Momentum 6m tr+vol"})
    for e, m in [(20, 0.25), (20, 0.5), (30, 0.5), (55, 1.0)]:
        cands.append({"mode": "atr_breakout", "entry": e, "exit": max(e // 2, 5), "mult": m,
                      "label": f"ATRbrk {e}d x{m}"})
    for f, sl in [(9, 21), (12, 26), (20, 50), (10, 30)]:
        cands.append({"mode": "ema_cross", "fast": f, "slow": sl,
                      "label": f"EMA{f}/{sl}"})
    cands.append({"mode": "ema_cross", "fast": 12, "slow": 26, "trend": 200,
                  "label": "EMA12/26 +trend200"})
    for k in [9, 14]:
        cands.append({"mode": "stoch", "k": k, "buy_below": 20, "sell_above": 80,
                      "label": f"Stoch{k} <20/>80"})
    cands.append({"mode": "stoch", "k": 14, "buy_below": 25, "sell_above": 75,
                  "trend": 100, "label": "Stoch14 <25/>75 +trend"})
    for th in [20, 25]:
        cands.append({"mode": "adx", "threshold": th,
                      "label": f"ADX>{th} trend-on"})
    cands.extend(load_ai_proposals())
    return cands


# Bounds for auto-tuning so thresholds can never drift into absurd territory.
TUNE_BOUNDS = {
    "rsi_buy": (5, 30),
    "rsi_sell": (50, 80),
    "ema_fast": (3, 25),
    "ema_slow": (8, 60),
    "atr_mult": (0.3, 3.0),
}
# How far from the regime baseline a per-symbol learned shift may go.
TUNE_MAX_DELTA = 6


def adaptive_params(df, cfg=None):
    try:
        close = df["Close"]
        vol = realized_vol(close)
        avg_vol = close.pct_change().std() * np.sqrt(252)
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
        params = {}
        if vol_ratio > 1.5:
            params["rsi_buy"] = 25
            params["rsi_sell"] = 65
            params["ema_fast"] = 12
            params["ema_slow"] = 26
            params["atr_mult"] = 1.5
            params["note"] = "high-vol: wider thresholds"
        elif vol_ratio < 0.7:
            params["rsi_buy"] = 20
            params["rsi_sell"] = 55
            params["ema_fast"] = 7
            params["ema_slow"] = 18
            params["atr_mult"] = 0.75
            params["note"] = "low-vol: tighter thresholds"
        else:
            params["rsi_buy"] = 24
            params["rsi_sell"] = 60
            params["ema_fast"] = 9
            params["ema_slow"] = 21
            params["atr_mult"] = 1.0
            params["note"] = "normal-vol: standard"
        # Apply any per-symbol learned shifts (auto-tuned) on top of the
        # regime baseline so what the bot learned actually takes effect.
        if cfg and cfg.get("adaptive") and cfg["adaptive"].get("tuned"):
            tuned = cfg["adaptive"]["tuned"]
            for key, delta in tuned.items():
                if key in params and isinstance(delta, (int, float)):
                    base = params[key]
                    lo, hi = TUNE_BOUNDS.get(key, (None, None))
                    target = base + delta
                    target = max(lo, min(hi, target)) if lo is not None else target
                    if abs(target - base) <= TUNE_MAX_DELTA:
                        params[key] = round(target, 3)
                        params["note"] = f"{params.get('note','')} | tuned {key}{delta:+.0f}"
        return params
    except Exception:
        return {"rsi_buy": 24, "rsi_sell": 60, "ema_fast": 9, "ema_slow": 21,
                "atr_mult": 1.0, "note": "fallback"}


def walk_forward_score(df, cfg, n_windows=5):
    try:
        close = df["Close"]
        window_size = len(df) // (n_windows + 1)
        if window_size < 30:
            return None
        test_scores = []
        for i in range(n_windows):
            start = window_size * (i + 1)
            end = min(start + window_size, len(df))
            if end - start < 20:
                break
            window_df = df.iloc[start:end]
            rets = _strat_rets(window_df, cfg)
            eq = CAPITAL * (1 + rets).cumprod()
            score = smart_score(eq, rets)
            test_scores.append(score)
        if not test_scores:
            return None
        avg = sum(test_scores) / len(test_scores)
        consistency = sum(1 for s in test_scores if s > 0) / len(test_scores)
        return avg * consistency
    except Exception:
        return None


def generate_experiments(symbol, existing_labels):
    experiments = []
    for rsi_b in [5, 8, 12, 15]:
        for rsi_s in [55, 60, 65]:
            label = f"RSI2 <{rsi_b}/{rsi_s}"
            if label not in existing_labels:
                experiments.append({"mode": "rsi2", "buy_below": rsi_b,
                                   "sell_above": rsi_s, "label": label})
    for f, s in [(5, 15), (7, 18), (10, 30), (15, 40)]:
        label = f"EMA{f}/{s}"
        if label not in existing_labels:
            experiments.append({"mode": "ema_cross", "fast": f, "slow": s,
                               "label": label})
    for e, m in [(15, 0.25), (25, 0.75), (40, 1.5)]:
        label = f"ATRbrk {e}d x{m}"
        if label not in existing_labels:
            experiments.append({"mode": "atr_breakout", "entry": e,
                               "exit": max(e // 2, 5), "mult": m, "label": label})
    import random
    random.shuffle(experiments)
    return experiments[:4]


def config_path(symbol):
    safe = symbol.replace("/", "-")
    return f"config_{safe}.json"


def evolve_symbol(symbol, force_print=True, df=None):
    if df is None:
        df = yf.Ticker(yf_symbol(symbol)).history(period="3y")
    close = df["Close"]
    regimes = detect_regime_series(df)
    live_regime = str(regimes.iloc[-1])

    cut = int(len(df) * 0.7)
    train_df, test_df = df.iloc[:cut], df.iloc[cut:]
    test_regimes = regimes.iloc[cut:]
    regime_mask = (test_regimes == live_regime).values

    scored = []
    for c in candidates():
        tr_rets = _strat_rets(train_df, c)
        scored.append((sharpe_like(tr_rets), c))
    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for tr_score, cfg in scored[:8]:
        te_rets = _strat_rets(test_df, cfg)
        te_eq = CAPITAL * (1 + te_rets[regime_mask]).cumprod()
        te_score = smart_score(te_eq, te_rets[regime_mask])
        wf_score = walk_forward_score(df, cfg)
        if wf_score is not None:
            te_score = te_score * 0.6 + wf_score * 0.4
        fired = live_penalty(cfg.get("label", ""))
        if fired:
            te_score += fired
            results.append((tr_score, te_score, cfg))
            print(f"  [LIVE RECORD] {cfg.get('label')} penalized {fired} "
                  f"(real trades lost money) — brain remembers.")
            continue
        results.append((tr_score, te_score, cfg))

    hold_rets = _strat_rets(test_df, {"mode": "hold"})
    hold_eq = CAPITAL * (1 + hold_rets[regime_mask]).cumprod()
    hold_te = smart_score(hold_eq, hold_rets[regime_mask])

    results.sort(key=lambda x: x[1], reverse=True)
    _, best_te, best_cfg = results[0]

    old = load_config(symbol)
    old_score = old.get("test_score", -99) if old else -99
    if old_score > best_te and old_score > 0:
        best_cfg = old
        best_te = old_score
        keep = True
    else:
        keep = False

    adapt = adaptive_params(df)
    best_cfg.update({"symbol": symbol, "regime": live_regime,
                     "test_score": best_te, "hold_test_score": hold_te,
                     "adaptive": adapt,
                     "updated": datetime.now().isoformat(timespec="seconds")})
    save_config(best_cfg)

    n_in_regime = int(regime_mask.sum())
    verdict = "BEATS hold in-regime" if best_te > hold_te else "best available in-regime"
    action = "kept (old was better)" if keep else ""
    lesson = (f"[{datetime.now():%Y-%m-%d %H:%M}] EVOLVE {symbol} [{live_regime}, {n_in_regime} test days]: "
              f"{old['label'] if old else 'none'} -> {best_cfg['label']} "
              f"(test {best_te} vs hold {hold_te}) {verdict} {action}")
    with open(LESSONS_FILE, "a", encoding="utf-8") as f:
        f.write(lesson + "\n")
    try:
        conn = init_db()
        conn.execute("INSERT INTO lessons(ts,text) VALUES(?,?)",
                     (datetime.now().isoformat(timespec="seconds"), lesson))
        conn.commit()
        conn.close()
    except Exception:
        pass
    if force_print:
        print("  " + lesson)
    return best_cfg


def evolve_all(symbols=None, force_print=True):
    import time as _t
    symbols = symbols if symbols else ALL
    cands = candidates()
    existing_labels = {c.get("label") for c in cands}
    for sym in symbols[:3]:
        experiments = generate_experiments(sym, existing_labels)
        if experiments:
            cands.extend(experiments)
            if force_print:
                print(f"  [EXPERIMENT] testing {len(experiments)} new combos for {sym}")
    t0 = _t.time()
    print(f"Brain: testing {len(cands)} strategy types x {len(symbols)} markets (regime-aware, Sortino/PF scoring)...")
    data = {}
    for sym in symbols:
        try:
            data[sym] = yf.Ticker(yf_symbol(sym)).history(period="3y")
        except Exception:
            pass
    print(f"  [cache] {len(data)}/{len(symbols)} markets downloaded once ({_t.time()-t0:.1f}s)")
    changes = []
    for sym in symbols:
        if sym not in data or data[sym].empty:
            continue
        old = load_config(sym)
        old_label = old["label"] if old else "none"
        new_cfg = evolve_symbol(sym, force_print, df=data[sym])
        if new_cfg["label"] != old_label:
            changes.append(f"{sym}: {old_label}->{new_cfg['label']}")
        else:
            changes.append(f"{sym}: kept {new_cfg['label']}")
    send_alert("🧠 BRAIN IMPROVED | שיפור מוח: " + "; ".join(changes))
    print(f"  evolution finished in {_t.time()-t0:.1f}s")


def yf_symbol(sym):
    return CRYPTO.get(sym, sym)


def load_config(symbol):
    try:
        with open(config_path(symbol), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_config(cfg):
    with open(config_path(cfg["symbol"]), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def config_is_stale(cfg, days=1):
    if cfg is None:
        return True
    return datetime.now() - datetime.fromisoformat(cfg["updated"]) > timedelta(days=days)


def current_state(df, cfg):
    close = df["Close"]
    price = float(close.iloc[-1])
    notes = []
    mode = cfg["mode"]
    ap = adaptive_params(df, cfg)

    if mode == "hold":
        return True, "always", "-", []

    if mode == "donchian":
        upper = float(close.rolling(cfg["entry"]).max().shift(1).iloc[-1])
        lower = float(close.rolling(max(int(cfg["exit"]), 2)).min().shift(1).iloc[-1])
        buy_txt, sell_txt = f"price ${price:.2f} > ${upper:.2f}", f"price < ${lower:.2f}"
        want = bool(price > upper)
        if want:
            notes.append(f"breakout vs {cfg['entry']}d high")

    elif mode == "rsi":
        d = close.diff()
        gain = d.clip(lower=0)
        loss = -d.clip(upper=0)
        ag = gain.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        al = loss.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        rsi = float((100 - 100 / (1 + ag / al)).iloc[-1])
        adj_buy = ap.get("rsi_buy", cfg["buy_below"])
        adj_sell = ap.get("rsi_sell", cfg["sell_above"])
        buy_txt = f"RSI {rsi:.0f} < {adj_buy}" + (f" (vol-adj)" if adj_buy != cfg["buy_below"] else "")
        sell_txt = f"RSI > {adj_sell}"
        want = bool(rsi < adj_buy)
        if want:
            notes.append(f"dip detected RSI={rsi:.0f}" + (f" vol-adj {ap['note']}" if adj_buy != cfg["buy_below"] else ""))
        if want and cfg.get("volume_confirm"):
            vok = bool(float(df["Volume"].iloc[-1]) > 1.5 * float(df["Volume"].rolling(20).mean().iloc[-1]))
            notes.append(f"volume {'confirming' if vok else 'weak -> blocked'}")
            want = want and vok

    elif mode == "bollinger":
        mid = float(close.rolling(20).mean().iloc[-1])
        low_band = mid - 2 * float(close.rolling(20).std().iloc[-1])
        buy_txt = f"price ${price:.2f} < lower band ${low_band:.2f}"
        sell_txt = f"price back above mid ${mid:.2f}"
        want = bool(price < low_band)
        if want:
            notes.append("band dip detected")

    elif mode == "macd":
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        m, sg = float(macd.iloc[-1]), float(signal.iloc[-1])
        buy_txt = f"MACD {m:.2f} above signal {sg:.2f}"
        sell_txt = "MACD below signal"
        want = bool(m > sg)
        notes.append(f"MACD {'bull' if want else 'bear'}")

    elif mode == "atr_breakout":
        atr = float(_atr14(df).iloc[-1])
        adj_mult = ap.get("atr_mult", cfg["mult"])
        upper = float(close.rolling(cfg["entry"]).max().shift(1).iloc[-1]) + adj_mult * atr
        lower = float(close.rolling(max(int(cfg["exit"]), 2)).min().shift(1).iloc[-1]) - adj_mult * atr
        buy_txt = f"price ${price:.2f} > ${upper:.2f} (incl {adj_mult}xATR buffer)" + (f" vol-adj" if adj_mult != cfg["mult"] else "")
        sell_txt = f"price < ${lower:.2f}"
        want = bool(price > upper)
        if want:
            notes.append(f"ATR-confirmed breakout" + (f" vol-adj" if adj_mult != cfg["mult"] else ""))

    elif mode == "ema_cross":
        fast_p = ap.get("ema_fast", cfg["fast"])
        slow_p = ap.get("ema_slow", cfg["slow"])
        fast = float(close.ewm(span=fast_p, adjust=False).mean().iloc[-1])
        slow = float(close.ewm(span=slow_p, adjust=False).mean().iloc[-1])
        buy_txt = f"EMA{fast_p} ${fast:.2f} above EMA{slow_p} ${slow:.2f}"
        sell_txt = f"EMA{fast_p} falls below EMA{slow_p}"
        want = bool(fast > slow)
        notes.append(f"EMA {'bull' if want else 'bear'} cross" + (f" (vol-adj)" if fast_p != cfg["fast"] else ""))

    elif mode == "stoch":
        lo = close.rolling(cfg["k"]).min()
        hi = close.rolling(cfg["k"]).max()
        k = float((100 * (close - lo) / (hi - lo)).iloc[-1])
        buy_txt = f"Stoch%K {k:.0f} < {cfg['buy_below']}"
        sell_txt = f"Stoch%K > {cfg['sell_above']}"
        want = bool(k < cfg["buy_below"])
        if want:
            notes.append(f"oversold stoch {k:.0f}")

    elif mode == "adx":
        up = df["High"].diff()
        dn = -df["Low"].diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=close.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=close.index)
        tr = pd.concat([df["High"] - df["Low"],
                        (df["High"] - close.shift()).abs(),
                        (df["Low"] - close.shift()).abs()], axis=1).max(axis=1)
        atr_w = tr.ewm(alpha=1 / 14, min_periods=14).mean()
        pdi_s = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        mdi_s = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        dx = 100 * (pdi_s - mdi_s).abs() / (pdi_s + mdi_s)
        adx_v = float(dx.ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1])
        pdi, mdi = float(pdi_s.iloc[-1]), float(mdi_s.iloc[-1])
        buy_txt = f"ADX {adx_v:.0f} > {cfg['threshold']} + DI+ {pdi:.0f} > DI-"
        sell_txt = "DI+ loses to DI-"
        want = bool(adx_v > cfg["threshold"] and pdi > mdi)
        notes.append(f"trend strength ADX={adx_v:.0f}")

    else:
        mom = float(close.pct_change(126).iloc[-1]) * 100
        buy_txt = f"6-month momentum {mom:+.0f}% > 0"
        sell_txt = "momentum turns negative"
        want = bool(mom > 0)
        notes.append(f"momentum {mom:+.0f}%")

    if want:
        if cfg.get("trend"):
            up = bool(price > float(close.rolling(int(cfg["trend"])).mean().iloc[-1]))
            notes.append(f"trend{cfg['trend']} {'UP ok' if up else 'DOWN -> blocked'}")
            want = want and up
        if want and cfg.get("vol_mult"):
            vol = _vol10(close)
            calm = bool(float(vol.iloc[-1]) <= float((vol.rolling(60).median().fillna(vol) * cfg["vol_mult"]).iloc[-1]))
            notes.append(f"vol {'calm ok' if calm else 'wild -> blocked'}")
            want = want and calm

    return want, buy_txt, sell_txt, notes


def is_exit_signal(df, cfg):
    close = df["Close"]
    price = float(close.iloc[-1])
    mode = cfg["mode"]
    ap = adaptive_params(df, cfg)

    if mode == "hold":
        return False

    if mode == "donchian":
        lower = float(close.rolling(max(int(cfg["exit"]), 2)).min().shift(1).iloc[-1])
        return price < lower

    if mode == "atr_breakout":
        lower = float(close.rolling(max(int(cfg["exit"]), 2)).min().shift(1).iloc[-1])
        atr_val = float(_atr14(df).iloc[-1])
        adj_mult = ap.get("atr_mult", cfg["mult"])
        return price < lower - adj_mult * atr_val

    if mode == "ema_cross":
        fast_p = ap.get("ema_fast", cfg["fast"])
        slow_p = ap.get("ema_slow", cfg["slow"])
        fast = float(close.ewm(span=fast_p, adjust=False).mean().iloc[-1])
        slow = float(close.ewm(span=slow_p, adjust=False).mean().iloc[-1])
        return fast < slow

    if mode == "macd":
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        return float(macd.iloc[-1]) < float(signal.iloc[-1])

    if mode == "stoch":
        lo = close.rolling(cfg["k"]).min()
        hi = close.rolling(cfg["k"]).max()
        k = float((100 * (close - lo) / (hi - lo)).iloc[-1])
        return k > cfg["sell_above"]

    if mode == "rsi":
        d = close.diff()
        gain = d.clip(lower=0)
        loss = -d.clip(upper=0)
        ag = gain.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        al = loss.ewm(alpha=1 / cfg["period"], min_periods=cfg["period"]).mean()
        rsi = float((100 - 100 / (1 + ag / al)).iloc[-1])
        adj_sell = ap.get("rsi_sell", cfg["sell_above"])
        return rsi > adj_sell

    if mode == "bollinger":
        mid = float(close.rolling(20).mean().iloc[-1])
        return price > mid

    if mode == "adx":
        up = df["High"].diff()
        dn = -df["Low"].diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=close.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=close.index)
        tr = pd.concat([df["High"] - df["Low"],
                        (df["High"] - close.shift()).abs(),
                        (df["Low"] - close.shift()).abs()], axis=1).max(axis=1)
        atr_w = tr.ewm(alpha=1 / 14, min_periods=14).mean()
        pdi_s = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        mdi_s = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / atr_w
        pdi, mdi = float(pdi_s.iloc[-1]), float(mdi_s.iloc[-1])
        return pdi < mdi

    if mode == "momentum":
        mom = float(close.pct_change(126).iloc[-1])
        return mom < 0

    return False


def log_journal(symbol, price, action, detail, equity):
    conn = init_db()
    try:
        p = float(price) if price not in (None, "") else 0.0
    except (TypeError, ValueError):
        p = 0.0
    conn.execute("INSERT INTO journal(ts,symbol,price,action,detail,equity) VALUES(?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), symbol,
                  round(p, 4), action, str(detail)[:200],
                  round(float(equity), 2) if equity else None))
    conn.commit()
    conn.close()


def log_trade(ts, symbol, side, notional, qty, limit_price, status, algo,
              fill_price=None, signal_price=None, strategy=None):
    conn = init_db()
    slippage_bps = None
    try:
        if fill_price and signal_price and float(signal_price) > 0:
            direction = 1 if str(side).upper() == "BUY" else -1
            slippage_bps = round((float(fill_price) / float(signal_price) - 1) * 10000 * direction, 1)
    except (TypeError, ValueError):
        pass
    conn.execute("""INSERT INTO trades(ts,symbol,side,notional,qty,limit_price,fill_price,status,algo,slippage_bps,strategy,signal_price)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (ts, symbol, side, notional, qty, limit_price,
                  round(float(fill_price), 4) if fill_price else None,
                  status, algo, slippage_bps, strategy,
                  round(float(signal_price), 4) if signal_price else None))
    conn.commit()
    conn.close()


def reconcile_trades(filled_orders):
    conn = init_db()
    open_rows = conn.execute("""SELECT id, symbol, side, signal_price FROM trades
                                WHERE fill_price IS NULL AND signal_price IS NOT NULL""").fetchall()
    updated = []
    for tid, tsym, tside, sig in open_rows:
        want = "buy" if str(tside).upper() == "BUY" else "sell"
        match = next((o for o in filled_orders
                      if alpaca_sym(o.symbol) == alpaca_sym(tsym)
                      and str(o.side).lower() == want
                      and getattr(o, "filled_avg_price", None)), None)
        if not match:
            continue
        fill = float(match.filled_avg_price)
        try:
            slip = round((fill / float(sig) - 1) * 10000 * (1 if want == "buy" else -1), 1)
        except (TypeError, ZeroDivisionError):
            slip = None
        conn.execute("UPDATE trades SET fill_price=?, status='filled', slippage_bps=? WHERE id=?",
                     (round(fill, 4), slip, tid))
        updated.append((tsym, tside.upper(), fill, slip))
    conn.commit()
    conn.close()
    return updated


def reconcile_with_positions(positions):
    conn = init_db()
    pos_map = {alpaca_sym(p.symbol): float(p.avg_entry_price) for p in positions}
    open_rows = conn.execute("""SELECT id, symbol, side, signal_price, ts FROM trades
                                WHERE fill_price IS NULL AND signal_price IS NOT NULL
                                AND side='BUY' ORDER BY ts DESC""").fetchall()
    seen = set()
    updated = []
    for tid, tsym, tside, sig, _ in open_rows:
        key = alpaca_sym(tsym)
        if key not in pos_map or key in seen:
            continue
        seen.add(key)
        fill = pos_map[key]
        try:
            slip = round((fill / float(sig) - 1) * 10000, 1)
        except (TypeError, ZeroDivisionError):
            slip = None
        conn.execute("UPDATE trades SET fill_price=?, status='filled', slippage_bps=? WHERE id=?",
                     (round(fill, 4), slip, tid))
        updated.append((tsym, "BUY", fill, slip))
    conn.commit()
    conn.close()
    return updated


def journal_tail(n=14):
    conn = init_db()
    rows = conn.execute("SELECT ts||' | '||symbol||' | '||action||' | '||detail FROM journal ORDER BY id DESC LIMIT ?",
                        (n,)).fetchall()
    conn.close()
    return [r[0] for r in rows][::-1]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"peak_equity": None, "halted": False, "lockdown": False}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def log_equity(equity, peak):
    conn = init_db()
    try:
        if peak is None or peak <= 0 or equity is None or equity <= 0:
            conn.close()
            return
        if equity > peak * 2 or peak > equity * 20:
            conn.close()
            return
        dd = (equity / peak - 1) * 100 if peak else 0.0
        conn.execute("INSERT OR REPLACE INTO equity_history(date,equity,peak,drawdown_pct) VALUES(?,?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d"), round(equity, 2),
                      round(peak, 2), round(dd, 2)))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def equity_history_df():
    try:
        conn = init_db()
        df = pd.read_sql("SELECT date, equity, peak, drawdown_pct FROM equity_history ORDER BY date", conn)
        conn.close()
        if not df.empty:
            return df
    except Exception:
        pass
    try:
        return pd.read_csv("equity_history.csv")
    except FileNotFoundError:
        return pd.DataFrame(columns=["date", "equity", "peak", "drawdown_pct"])


def realized_vol(close):
    v = close.pct_change().rolling(20).std().iloc[-1] * np.sqrt(252)
    return float(v) if pd.notna(v) and v > 0 else None


def vol_targeted_notional(vols, symbol, base=8500.0, floor=3000.0, cap=15000.0):
    target = vols.get(symbol)
    med = float(pd.Series([v for v in vols.values() if v]).median()) if any(vols.values()) else None
    if not target or not med:
        return base
    size = base * (med / target)
    return round(min(max(size, floor), cap), -2)


def correlation_gate(closes, symbol, held_symbols, threshold=0.70, max_alike=2):
    if symbol in held_symbols or not held_symbols or symbol not in closes:
        return {"blocked": False, "alike": 0, "avg": 0.0}
    base = closes[symbol].pct_change().dropna()
    corrs = []
    alike = 0
    for h in held_symbols:
        if h not in closes:
            continue
        other = closes[h].pct_change().dropna()
        j = pd.concat([base, other], axis=1).dropna()
        if len(j) < 30:
            continue
        c = float(j.corr().iloc[0, 1])
        corrs.append(c)
        if c > threshold:
            alike += 1
    avg = round(sum(corrs) / len(corrs), 2) if corrs else 0.0
    return {"blocked": alike >= max_alike, "alike": alike, "avg": avg}


def send_alert(message):
    import os
    try:
        from keys import NTFY_TOPIC
    except ImportError:
        return
    if not NTFY_TOPIC:
        return
    try:
        import requests
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), timeout=6)
    except Exception:
        pass


def _valid_proposal(p):
    if not isinstance(p, dict):
        return False
    mode = p.get("mode")
    try:
        if mode == "rsi":
            period = int(p["period"])
            bb, sa = float(p["buy_below"]), float(p["sell_above"])
            if not (1 <= period <= 30 and 0 < bb < sa <= 95):
                return False
            out = {"mode": "rsi", "period": period, "buy_below": bb, "sell_above": sa}
            core = f"RSI{period}<{bb}/{sa}"
        elif mode == "donchian":
            e, x = int(p["entry"]), int(p["exit"])
            if not (2 <= x < e <= 120):
                return False
            out = {"mode": "donchian", "entry": e, "exit": x}
            core = f"Donch{e}/{x}"
        elif mode == "atr_breakout":
            en, ex_ = int(p["entry"]), int(p["exit"])
            m = float(p["mult"])
            if not (2 <= ex_ < en <= 120 and 0.0 <= m <= 3.0):
                return False
            out = {"mode": "atr_breakout", "entry": en, "exit": ex_, "mult": round(m, 2)}
            core = f"ATRbrk{en}x{round(m, 2)}"
        else:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    extras = {}
    if p.get("trend"):
        t = int(p["trend"])
        if not (20 <= t <= 400):
            return False
        extras["trend"] = t
    if p.get("vol_mult"):
        vm = float(p["vol_mult"])
        if not (1.0 <= vm <= 3.0):
            return False
        extras["vol_mult"] = round(vm, 2)
    out.update(extras)
    tag = "".join(f"+{k}{v}" for k, v in extras.items())
    out["label"] = f"AI-idea {core} {tag}".strip()
    out["ai_proposed"] = True
    return out


_ai_proposals_printed = False

def load_ai_proposals():
    global _ai_proposals_printed
    try:
        import json as _json
        with open("proposals.json", encoding="utf-8") as f:
            raw = _json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return []
    valid = []
    for p in raw if isinstance(raw, list) else []:
        v = _valid_proposal(p)
        if v:
            valid.append(v)
    if valid and not _ai_proposals_printed:
        print(f"Brain: including {len(valid)} AI-proposed strategy ideas in today's trials")
        _ai_proposals_printed = True
    return valid
