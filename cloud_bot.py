"""
cloud_bot.py — Lightweight bot for GitHub Actions (no ollama, no vision).
Runs the same strategies, same risk management, same self-learning.
"""
import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY = os.environ.get("APCA_API_KEY_ID", "")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY", "")
if not API_KEY:
    from keys import API_KEY, SECRET_KEY

client = TradingClient(API_KEY, SECRET_KEY, paper=True)


def _tif(symbol):
    if symbol in brain.CRYPTO:
        return TimeInForce.GTC
    return TimeInForce.DAY


def smart_buy(symbol, notional, ref_price):
    tif = _tif(symbol)
    steps = brain.best_limit_pct(symbol, "buy")
    for lim_pct in steps:
        limit_price = round(ref_price * lim_pct, 2)
        qty = round(float(notional) / max(limit_price, 1e-9), 4)
        if qty <= 0:
            return None, "rejected", None
        try:
            o = client.submit_order(LimitOrderRequest(
                symbol=symbol, side=OrderSide.BUY, time_in_force=tif,
                qty=qty, limit_price=limit_price))
            time.sleep(5)
            o = client.get_order(o.id)
            st = str(o.status)
            if st in ("filled",):
                fill = float(o.filled_avg_price)
                return o.id, "filled", fill
            elif st in ("accepted", "new", "pending_new"):
                return o.id, "queued", None
            else:
                try:
                    client.cancel_order_by_id(o.id)
                except Exception:
                    pass
        except Exception as e:
            print(f"    buy failed: {e}")
            continue
    try:
        o = client.submit_order(MarketOrderRequest(
            symbol=symbol, side=OrderSide.BUY, time_in_force=tif,
            qty=round(float(notional) / max(ref_price, 1e-9), 4)))
        time.sleep(5)
        o = client.get_order(o.id)
        return o.id, str(o.status), float(o.filled_avg_price) if o.filled_avg_price else ref_price
    except Exception as e:
        return None, "error", None


def smart_sell(symbol, qty, ref_price):
    tif = _tif(symbol)
    try:
        o = client.submit_order(MarketOrderRequest(
            symbol=symbol, side=OrderSide.SELL, time_in_force=tif, qty=float(qty)))
        time.sleep(5)
        o = client.get_order(o.id)
        return o.id, str(o.status), float(o.filled_avg_price) if o.filled_avg_price else ref_price
    except Exception as e:
        return None, "error", None


def run_cloud():
    print(f"CLOUD BOT — {datetime.now():%Y-%m-%d %H:%M}")

    all_data = {}
    for sym in brain.ALL:
        try:
            all_data[sym] = yf.Ticker(brain.yf_symbol(sym)).history(period="3mo")
        except Exception:
            pass

    try:
        import news as news_mod
    except Exception:
        news_mod = None

    try:
        clock = client.get_clock()
        market_open = bool(clock.is_open)
    except Exception:
        market_open = False

    account = client.get_account()
    equity = float(account.equity)
    positions = list(client.get_all_positions())
    held_symbols = [p.symbol for p in positions]

    state = brain.load_state()
    if state["peak_equity"] is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity
    brain.save_state(state)
    peak = float(state["peak_equity"])
    drawdown = equity / peak - 1

    breaker_level = None
    for threshold, name in brain.CIRCUIT_BREAKERS:
        if drawdown <= threshold:
            breaker_level = name

    allow_entries = breaker_level != "CAUTION"

    try:
        all_open_orders = list(client.get_orders())
        pending_symbols = {brain.internal_sym(o.symbol) for o in all_open_orders}
    except Exception:
        pending_symbols = set()

    vols = {}
    closes = {}
    for sym in brain.ALL:
        if sym in all_data and not all_data[sym].empty:
            try:
                vols[sym] = brain.realized_vol(all_data[sym]["Close"])
                closes[sym] = all_data[sym]["Close"]
            except Exception:
                pass

    deployed = sum(float(p.market_value) for p in positions)

    print(f"Equity: ${equity:,.2f} | Drawdown: {drawdown*100:+.1f}% | Positions: {len(positions)} | Cash: ${float(account.cash):,.2f}")

    actions = {}
    for symbol in brain.ALL:
        cfg = brain.load_config(symbol)
        if symbol not in all_data or all_data[symbol].empty:
            continue
        df = all_data[symbol]
        close = df["Close"]
        price = float(close.iloc[-1])

        live_regime = brain.current_regime(df)
        if not brain.config_is_stale(cfg) and cfg.get("regime") and cfg.get("regime") != live_regime:
            brain.evolve_symbol(symbol, force_print=False, df=df)
            cfg = brain.load_config(symbol)

        want_in, buy_txt, sell_txt, notes = brain.current_state(df, cfg)
        holding = next((p for p in positions if brain.alpaca_sym(p.symbol) == brain.alpaca_sym(symbol)), None)
        planned_notional = brain.vol_targeted_notional(vols, symbol)
        conviction = brain.conviction_score(symbol, cfg, regime=live_regime)
        recent_mom = brain.recent_momentum(df)
        sized_notional = planned_notional * conviction

        if recent_mom < -0.03:
            sized_notional *= 0.5
        elif recent_mom > 0.05:
            sized_notional *= 1.1

        action, detail = "WAIT", ""
        try:
            if want_in and holding is None and allow_entries:
                if recent_mom < -0.03:
                    action, detail = "MOMENTUM-BLOCKED", f"price dropped {recent_mom*100:+.1f}%"
                elif symbol in pending_symbols:
                    action, detail = "PENDING", "order working"
                elif deployed + sized_notional > brain.dynamic_heat_cap(live_regime, drawdown) * equity:
                    hc = brain.dynamic_heat_cap(live_regime, drawdown)
                    action, detail = "HEAT-BLOCKED", f"would breach {hc*100:.0f}% ({live_regime})"
                elif conviction < brain.MIN_CONVICTION:
                    action, detail = "LOW-CONVICTION", f"{conviction:.0%} < {brain.MIN_CONVICTION:.0%}"
                else:
                    try:
                        verdict, reason = news_mod.sentiment(symbol) if news_mod else ("NEUTRAL", "no news module")
                        if verdict == "BEARISH":
                            action, detail = "NEWS-BLOCKED", reason[:60]
                    except Exception:
                        pass

                    if action == "WAIT":
                        oid, st, fill_price = smart_buy(symbol, sized_notional, price)
                        action, detail = "BUY", f"${sized_notional:,.0f} conv={conviction:.0%}"
                        brain.set_position_strategy(symbol, cfg.get("label", "?"))
                        brain.log_trade(datetime.now().isoformat(timespec="seconds"), symbol, "BUY",
                                        sized_notional, None, None, st, "smart-limit",
                                        fill_price=fill_price, signal_price=price,
                                        strategy=cfg.get("label", "?"))
                        deployed += sized_notional

            elif holding is not None and brain.is_exit_signal(df, cfg) and cfg.get("mode") != "hold":
                qty = holding.qty
                oid, st, fill_price = smart_sell(symbol, qty, price)
                brain.clear_position_peak(symbol)
                action, detail = "SELL", f"exit signal: {qty} x {symbol}"
                _label = brain.pop_position_strategy(symbol)
                brain.log_trade(datetime.now().isoformat(timespec="seconds"), symbol, "SELL",
                                None, float(qty), None, st, "smart-limit",
                                fill_price=fill_price, signal_price=price,
                                strategy=_label)
                brain.log_closed_trade(symbol, _label, float(holding.avg_entry_price), fill_price or price)
                brain.analyze_trade_outcome(symbol, _label, float(holding.avg_entry_price),
                                            fill_price or price, live_regime)
                try:
                    brain.learn_on_exit(symbol, float(holding.avg_entry_price),
                                        fill_price or price, live_regime,
                                        brain.hash_pattern(df, live_regime) if not df.empty else None)
                except Exception:
                    pass
                brain.send_alert(f"SELL {symbol} ({_label})")

            elif holding is not None:
                action, detail = "HOLD", f"{holding.qty} x {symbol}"

        except Exception as e:
            action, detail = "ERROR", str(e)[:80]

        brain.log_journal(symbol, price, action, detail, equity)
        actions[symbol] = (action, detail)
        print(f"  {symbol:8s} | {action:16s} | {detail[:60]}")

    brain.log_equity(equity, peak)

    print("\n[LEARN]")
    try:
        import learn
        learn.learn_from_open_trades()
        learn.track_regime_performance()
        learn.update_kelly_from_trades()
        print("  learning complete")
    except Exception as e:
        print(f"  learn error: {e}")

    print(f"\nFinal equity: ${equity:,.2f}")


if __name__ == "__main__":
    run_cloud()
