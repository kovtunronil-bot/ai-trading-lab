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
    tried_ids = []
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
                # Give the band one more short window to fill before we
                # abandon it. Choppy markets let the price come back to us.
                time.sleep(8)
                o = client.get_order(o.id)
                st = str(o.status)
                if st in ("filled",):
                    fill = float(o.filled_avg_price)
                    return o.id, "filled", fill
                # Still open: cancel and let the escalation below wind up.
                tried_ids.append(o.id)
                brain.log_execution(symbol, "buy", lim_pct,
                                    (time.time() - _RUN_T0) if "_RUN_T0" in globals() else 0,
                                    "timeout")
                try:
                    client.cancel_order_by_id(o.id)
                except Exception:
                    pass
            else:
                try:
                    client.cancel_order_by_id(o.id)
                except Exception:
                    pass
        except Exception as e:
            print(f"    buy failed: {e}")
            continue
    # Escalation: the market ran past our limits. Take the fill at market
    # rather than miss the entry entirely (the bot is here to trade, not to
    # save 10 bps). Big limit windows that stayed unfilled are cancelled
    # above, so this is a single market order, not a duplicate.
    try:
        qty = round(float(notional) / max(ref_price, 1e-9), 4)
        if qty <= 0:
            return None, "rejected", None
        o = client.submit_order(MarketOrderRequest(
            symbol=symbol, side=OrderSide.BUY, time_in_force=tif,
            qty=qty))
        time.sleep(5)
        o = client.get_order(o.id)
        return o.id, str(o.status), float(o.filled_avg_price) if o.filled_avg_price else None
    except Exception as e:
        return None, "error", None


def smart_sell(symbol, qty, ref_price):
    tif = _tif(symbol)
    try:
        o = client.submit_order(MarketOrderRequest(
            symbol=symbol, side=OrderSide.SELL, time_in_force=tif, qty=float(qty)))
        time.sleep(5)
        o = client.get_order(o.id)
        return o.id, str(o.status), float(o.filled_avg_price) if o.filled_avg_price else None
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

    # SELF-IMPROVEMENT: re-select the best strategy for any market whose
    # config is stale (>1 day old). This is the bot "thinking" — it re-runs
    # its backtests on the latest data and keeps only what beats buy-and-hold
    # in the current regime. Only stale symbols run, so this stays light, and
    # each symbol re-evolves roughly once a day.
    try:
        stale = []
        for s in brain.ALL:
            _cfg = brain.load_config(s)
            if _cfg and brain.config_is_stale(_cfg) and str(_cfg.get("mode", "")).endswith("_fw"):
                stale.append(s)
        if stale:
            print(f"[SELF-LEARN] re-evolving {len(stale)} stale markets: {stale}")
            brain.evolve_all(stale, force_print=False)
    except Exception as e:
        print(f"[SELF-LEARN] skipped ({e})")

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
    held_symbols = [brain.internal_sym(p.symbol) for p in positions]

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

    # INDEPENDENT RISK MANAGER: stop-loss, profit-lock, flat-exit, lockdown.
    # (Parity with bot.py — the cloud bot must not hold losers forever.)
    def _close_and_learn(p, reason, cur_price):
        internal = brain.internal_sym(p.symbol)
        entry = float(p.avg_entry_price)
        try:
            client.close_position(p.symbol)
        except Exception as e:
            print(f"    close {p.symbol} failed: {e}")
            return
        brain.clear_position_peak(internal)
        _lbl = brain.pop_position_strategy(internal)
        brain.log_closed_trade(internal, _lbl, entry, cur_price)
        brain.analyze_trade_outcome(internal, _lbl, entry, cur_price,
                                    brain.current_regime(all_data.get(internal, pd.DataFrame())))
        try:
            _df = all_data.get(internal, pd.DataFrame())
            _regime = brain.current_regime(_df)
            _pattern = brain.hash_pattern(_df, _regime) if not _df.empty else None
            brain.learn_on_exit(internal, entry, cur_price, _regime, _pattern)
        except Exception:
            pass
        msg = f"{p.symbol} {reason} ({(cur_price/entry-1)*100:+.1f}%)"
        print(f"  {reason}: {msg}")
        brain.log_journal(internal, cur_price, reason[:20], msg[:100], equity)
        brain.send_alert(f"{reason} | {msg}")
        try:
            positions.remove(p)
        except ValueError:
            pass

    for p in list(positions):
        internal = brain.internal_sym(p.symbol)
        entry = float(p.avg_entry_price)
        cur = float(p.current_price)
        if entry <= 0:
            continue
        pnl_pct = cur / entry - 1

        # EMERGENCY PROTECTION: hard exit. If a single position is down >12%
        # from entry, close it immediately regardless of other conditions.
        # This is the backstop for gap-throughs, flash crashes, or any
        # situation where the normal stop-loss (8%) didn't trigger in time.
        if pnl_pct < -0.12:
            if internal not in brain.CRYPTO and not market_open:
                print(f"  {p.symbol} HARD-EXIT ({pnl_pct*100:+.1f}%) but market closed — next open run")
                continue
            _close_and_learn(p, f"HARD-EXIT({pnl_pct*100:+.1f}%)", cur)
            continue
        # Adaptive safety exit: a held symbol on a statistically failing
        # strategy gets a tighter stop so we don't ride losers via its weak
        # sell logic (e.g. MSFT EMA9/21 at 32% win-rate).
        failing = brain.strategy_is_failing(internal)
        stop_fraction = brain.FAILING_STOP if failing else brain.POSITION_STOP_LOSS
        # Framework strategies: use the strategy-specific SL (saved in config
        # at entry time) instead of the generic 8% stop.  This respects the
        # strategy's own risk framework (SL at sweep extreme, retest low, etc.).
        fw_sl = None
        fw_tp = None
        try:
            _cfg = brain.load_config(internal)
            fw_sl = _cfg.get("fw_sl") if _cfg else None
            fw_tp = _cfg.get("fw_tp") if _cfg else None
            if fw_sl and float(fw_sl) > 0:
                stop_fraction = max(1 - float(fw_sl) / entry, 0.02)  # min 2%
                # Cap framework stop width at the generic 8% stop. Framework
                # SLs measured at 11-20% are risk-unbalanced: with 1.5% per
                # trade risk they force either oversized bets or oversized
                # losses. Capping keeps every losing trade bounded.
                stop_fraction = min(stop_fraction, brain.POSITION_STOP_LOSS)
        except Exception:
            pass
        # 1) hard stop-loss (adaptive to strategy quality)
        if cur < entry * (1 - stop_fraction):
            if internal not in brain.CRYPTO and not market_open:
                print(f"  STOPLOSS HIT {p.symbol} but market closed — next open run")
                continue
            _close_and_learn(p, f"STOPLOSS{'[FAILING]' if failing else ''}", cur)
        else:
            # 1b) framework take-profit: mirrors the SL/TP walk-forward the
            #     measured edge came from (a trade that reaches the strategy
            #     TP exits there, locking the win). Without this, live trades
            #     ride past TP and give back profit the backtest assumed banked.
            if fw_tp and float(fw_tp) > 0 and cur >= float(fw_tp):
                if internal not in brain.CRYPTO and not market_open:
                    print(f"  {p.symbol} TP-HIT but market closed — next open run")
                    continue
                _close_and_learn(p, "TP-HIT", cur)
                continue
            # 2) profit-lock / trailing stop on winners
            pos_peak = brain.update_position_peak(internal, cur, entry)
            _live_reg = brain.current_regime(all_data.get(internal, pd.DataFrame()))
            if brain.profit_lock_hit(entry, cur, pos_peak, regime=_live_reg):
                if internal not in brain.CRYPTO and not market_open:
                    print(f"  {p.symbol} profit-lock hit but market closed — next open run")
                    continue
                _close_and_learn(p, "PROFIT-LOCK", cur)
                continue
            # 2b) adaptive trailing stop on winners (ATR-scaled, regime-aware,
            #     uses brain's trailing_profit_targets ladder). Locks in gains
            #     faster than profit-lock once the trade has run up a lot, and
            #     adapts stop width to the market regime (wider in trends, tighter
            #     in ranges) so we ride winners in trending markets and bank
            #     profits quickly in choppy/ranging ones.
            trail_stop, trail_target = brain.trailing_profit_targets(
                entry, cur, side="long",
                atr=brain.avg_atr(all_data.get(internal, pd.DataFrame())),
                regime=_live_reg)
            if trail_stop is not None and cur <= trail_stop:
                if internal not in brain.CRYPTO and not market_open:
                    print(f"  {p.symbol} TRAIL-HIT but market closed — next open run")
                    continue
                _close_and_learn(p, "TRAIL-HIT", cur)
                continue
            # 3) AI-driven exit: if the night-runner AI flagged the news
            #    BEARISH and we are holding a PROFIT, close early to lock it
            #    in before the bad news drags the price down — instead of
            #    waiting for the stop-loss. Only fires when in profit, so it
            #    never locks a loss on opinion.
            if cur > entry:
                _ai_bear = brain.ai_exit_bearish(internal)
                if _ai_bear:
                    if internal not in brain.CRYPTO and not market_open:
                        print(f"  {p.symbol} AI-BEARISH exit but market closed — next open run")
                    else:
                        _close_and_learn(p, "AI-BEARISH", cur)
                    continue
            # 4) flat (stale) trade exit (sooner for failing strategies)
            flat_days = brain.FAILING_FLAT_DAYS if failing else None
            flat_ok = (brain.is_flat_trade(p, max_days=flat_days)
                       if flat_days is not None else brain.is_flat_trade(p))
            if flat_ok:
                if internal not in brain.CRYPTO and not market_open:
                    print(f"  {p.symbol} flat {brain.days_in_trade(p)}d — next open run")
                    continue
                _close_and_learn(p, "FLAT-EXIT", cur)

    if breaker_level == "LOCKDOWN":
        print("!!! LOCKDOWN — selling everything !!!")
        for p in list(positions):
            try:
                client.close_position(p.symbol)
                brain.log_journal(brain.internal_sym(p.symbol), 0.0, "LOCKDOWN-SELL",
                                  f"closed {p.qty} x {p.symbol}", equity)
            except Exception as e:
                print(f"  close {p.symbol} failed: {e}")
        state["halted"] = True
        state["lockdown"] = True

    allow_entries = breaker_level not in ("CAUTION", "HALT", "LOCKDOWN")

    # EMERGENCY PROTECTION: daily loss limit. If equity dropped >3% from the
    # previous day's close, stop all new entries for today. This prevents
    # compounding losses during a multi-day selloff (e.g. tariff shock, flash
    # crash + aftershocks). Checked by comparing current equity to yesterday's
    # last recorded equity in equity_history.
    try:
        yesterday_eq = brain.latest_equity_before(datetime.now().date(), days_back=2)
        if yesterday_eq and yesterday_eq > 0:
            daily_loss = equity / yesterday_eq - 1
            if daily_loss < -0.03:
                allow_entries = False
                print(f">>> DAILY LOSS LIMIT: {daily_loss*100:+.1f}% from yesterday — entries halted for today")
    except Exception:
        pass

    # EMERGENCY PROTECTION: intraday max-daily-loss kill-switch (guide Part 4).
    # Measures today's P&L against where the day STARTED (not yesterday's
    # close) and halts ALL new entries for the rest of the day once today's
    # loss exceeds the threshold (2.5%).  Positions already held are still
    # managed/closed by the risk manager, but no new risk is taken.
    try:
        _kill, _kill_loss, _kill_msg = brain.daily_kill_switch(equity, threshold=0.025)
        if _kill:
            allow_entries = False
            print(f">>> DAILY KILL-SWITCH: {_kill_loss*100:+.1f}% today — no new entries rest of day")
            brain.send_alert(_kill_msg)
    except Exception as e:
        print(f"  daily kill-switch skipped ({e})")

    # EMERGENCY PROTECTION: HALT-level trim. At -10% drawdown, forcibly
    # trim the 2 largest positions by 30% each to reduce risk while still
    # keeping some exposure (not a full liquidation like LOCKDOWN).
    if breaker_level == "HALT":
        try:
            sized = sorted(positions, key=lambda p: float(p.market_value), reverse=True)
            for p in sized[:2]:
                mv = float(p.market_value)
                cur = float(p.current_price)
                if cur <= 0:
                    continue
                trim_qty = round(p.qty * 0.30, 4)
                if trim_qty <= 0:
                    continue
                oid, st, fill_price = smart_sell(p.symbol, trim_qty, cur)
                if st == "filled" and fill_price:
                    brain.log_journal(brain.internal_sym(p.symbol), cur, "HALT-TRIM",
                                      f"-30% {p.symbol} (${mv:,.0f})", equity)
                    brain.send_alert(f"HALT-TRIM {p.symbol}: trimming 30% at -10% drawdown")
                    print(f"  HALT-TRIM {p.symbol}: sold 30% ({trim_qty} shares)")
        except Exception as e:
            print(f"  halt-trim error: {e}")

    # ── TradingView signal execution ──────────────────────────────────────
    # Check the dedicated ntfy topic for TradingView alerts. These are
    # executed BEFORE the bot's own logic so they act as an overlay — the
    # bot still does its normal analysis, but TradingView signals get
    # priority execution. Only fires when entries are allowed (no
    # CAUTION/HALT/LOCKDOWN). Sell signals close existing positions
    # immediately regardless of the normal exit logic.
    try:
        tv_signals = brain.check_tradingview_signals()
        if tv_signals:
            print(f"\n[TRADINGVIEW] {len(tv_signals)} signal(s) received:")
            held_symbols = {brain.internal_sym(p.symbol) for p in positions}
            for sig in tv_signals:
                sym = sig["symbol"]
                action = sig["action"]
                reason = sig["reason"]
                print(f"  TV {action.upper()} {sym}: {reason}")
                if action == "buy" and allow_entries:
                    # Check if already holding
                    if sym in held_symbols or brain.alpaca_sym(sym) in [p.symbol for p in positions]:
                        print(f"    -> already holding {sym}, skip")
                        continue
                    # Check if it's a known market symbol
                    cfg = brain.load_config(sym)
                    if not cfg and sym not in brain.ALL:
                        print(f"    -> {sym} not in tracked markets, skip")
                        continue
                    # Execute buy via Alpaca
                    notional = min(brain.vol_targeted_notional(vols, sym), 10000)
                    if sym in brain.CRYPTO:
                        notional *= 1.8
                    df = all_data.get(sym, pd.DataFrame())
                    price = float(df["Close"].iloc[-1]) if not df.empty else None
                    if price and price > 0:
                        oid, st, fill_price = smart_buy(sym, notional, price)
                        if st == "filled" and fill_price:
                            brain.set_position_strategy(sym, f"TradingView: {reason[:30]}")
                            brain.log_journal(sym, fill_price, "TV-BUY",
                                              f"{sym} ${notional:,.0f} reason: {reason[:60]}", equity)
                            brain.send_alert(f"TradingView BUY {sym} ${notional:,.0f} ({reason})")
                            print(f"    -> BOUGHT {sym} ${notional:,.0f} @ ${fill_price}")
                        else:
                            print(f"    -> buy {st or 'error'}")
                elif action == "sell":
                    # Close existing position if holding
                    holding = next((p for p in positions if brain.internal_sym(p.symbol) == sym or p.symbol == sym), None)
                    if holding:
                        cur_price = float(holding.current_price)
                        qty = holding.qty
                        oid, st, fill_price = smart_sell(holding.symbol, qty, cur_price)
                        if st == "filled" and fill_price:
                            brain.log_journal(sym, fill_price, "TV-SELL",
                                              f"closed {qty} x {sym} reason: {reason[:60]}", equity)
                            brain.send_alert(f"TradingView SELL {sym} ({reason})")
                            print(f"    -> SOLD {sym} @ ${fill_price}")
                        else:
                            print(f"    -> sell {st or 'error'}")
                    else:
                        print(f"    -> not holding {sym}, skip sell")
    except Exception as e:
        print(f"  TradingView signal error: {e}")

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

    # CONCENTRATION TRIM: a position that has grown past its intended cap
    # (vol-target cap, or price appreciation) is trimmed back to the cap so
    # no single bet can swamp the portfolio. Stocks use the vol cap; crypto
    # respects the 1.8x boost cap.
    #   - over cap x 1.25: trim no matter what (hard risk discipline; a
    #     lopsided losing bet is the MOST dangerous kind)
    #   - over cap x 1.1 but under 1.25: trim winners only (never lock a loss
    #     on a mild overage)
    try:
        for p in list(positions):
            internal = brain.internal_sym(p.symbol)
            entry = float(p.avg_entry_price)
            cur = float(p.current_price)
            mv = float(p.market_value)
            if entry <= 0 or cur <= 0:
                continue
            cap_sym = 27000.0 if internal in brain.CRYPTO else 15000.0
            if mv <= cap_sym * 1.1:
                continue
            hard_trim = mv > cap_sym * 1.25
            if not hard_trim and cur <= entry:  # mild overage on a loser: wait
                continue
            sell_mv = mv - cap_sym
            qty = sell_mv / cur
            if qty <= 0:
                continue
            oid, st, fill_price = smart_sell(p.symbol, qty, cur)
            if st == "filled" and fill_price:
                reason = "HARD-TRIM" if hard_trim else "TRIM"
                brain.log_journal(internal, cur, reason, f"{p.symbol} trim ${sell_mv:,.0f} back to cap", equity)
                brain.send_alert(f"{reason} {p.symbol}: oversized ${mv:,.0f} -> trimmed ${sell_mv:,.0f}")
                print(f"  {reason} {p.symbol}: trimmed ${sell_mv:,.0f} (${mv:,.0f} -> cap)")
            else:
                print(f"  TRIM {p.symbol}: sell {st or 'err'}, will retry")
    except Exception as e:
        print(f"  trim pass failed: {e}")

    # CORRELATION DE-RISK: live multi-asset risk management. If several held
    # positions start moving in lockstep (e.g. a sector-wide selloff), they are
    # really one single bet, not several diverse bets. Detect a correlated
    # cluster of 3+ holdings all moving together and trim the redundant names
    # (keeping the strongest-mover) so one macro shock can't drag the whole
    # book. This is the "dynamic correlation matrix" risk guard.
    try:
        held_internal = [brain.internal_sym(p.symbol) for p in positions]
        corr_trims = brain.correlation_de_risk(closes, held_internal)
        for sym, frac in corr_trims:
            p = next((p for p in positions if brain.internal_sym(p.symbol) == sym), None)
            if not p:
                continue
            cur = float(p.current_price)
            if cur <= 0:
                continue
            qty = round(float(p.qty) * frac, 4)
            if qty <= 0:
                continue
            mv = float(p.market_value)
            oid, st, fill_price = smart_sell(p.symbol, qty, cur)
            if st == "filled" and fill_price:
                reason = "CORR-TRIM"
                brain.log_journal(sym, cur, reason,
                                  f"{p.symbol} trim {frac*100:.0f}% correlated bet (${mv:,.0f})", equity)
                brain.send_alert(f"{reason} {p.symbol}: trimming redundant correlated position")
                print(f"  CORR-TRIM {p.symbol}: trimmed {frac*100:.0f}% for correlation de-risk")
    except Exception as e:
        print(f"  correlation de-risk failed: {e}")

    # MONTE CARLO RISK ADVISOR: resample our real trading history to project
    # the most likely worst-case drawdown. If the projected 95th-percentile
    # drawdown approaches our HALT/LOCKDOWN limits, this returns a scale
    # factor (<1) applied to every new entry so the bot sizes down BEFORE a
    # catastrophic drawdown, not after. Print the advisory each run for
    # transparency.
    try:
        mc = brain.monte_carlo_drawdown()
        risk_mult = mc["risk_mult"]
        print(f">>> MONTE-CARLO: p95 worst-case DD {mc['p95_dd']}% (median {mc['median_dd']}%) "
              f"-> sizing {risk_mult*100:.0f}%")
        if risk_mult < 1.0:
            brain.send_alert(f"MONTE-CARLO: projected p95 DD {mc['p95_dd']}% -> scaling entries to {risk_mult*100:.0f}%")
    except Exception as e:
        risk_mult = 1.0
        print(f"  monte-carlo risk advisor skipped ({e})")

    actions = {}
    framework_levels = {}
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
        planned_notional *= risk_mult   # Monte-Carlo-derived risk sizing
        conviction = brain.conviction_score(symbol, cfg, regime=live_regime)
        recent_mom = brain.recent_momentum(df)

        # CRYPTO BOOST: crypto (BTC/ETH) trades have won ~6x the average of
        # stocks (+15.7% vs +2.4% per trade) and trade 24/7, so we allocate a
        # larger position to them. Boosting planned_notional means every later
        # sizing recalculation inherits it; heat/sector caps still protect us.
        if symbol in brain.CRYPTO:
            planned_notional *= 1.8
            print(f">>> CRYPTO-BOOST {symbol}: planned ${planned_notional:,.0f} (1.8x)")

        sized_notional = planned_notional * conviction

        # Framework strategies: OVERRIDE notional with risk-based sizing.
        # Position size = (Equity * 1.5%) / |Entry - SL|.  The SL/TP are
        # stored in framework_levels so the risk_manager can use them.
        # fw_size is preserved and re-applied right before order placement so
        # the volume-based conviction/momentum sizing can't clobber it.
        fw_mode = cfg.get("mode") in ("smc_fw", "breakout_fw", "divergence_fw",
                                      "vwap_fw", "heikin_fw", "boll_fw",
                                      "orb_fw", "vwap_rev_fw", "flag_fw", "gap_fade_fw")
        fw_size = None
        if fw_mode:
            rl = brain.compute_risk_levels(df, cfg)
            fw_entry = float(rl["entry"].iloc[-1])
            fw_sl    = float(rl["sl"].iloc[-1])
            fw_tp    = float(rl["tp"].iloc[-1])
            fw_sig   = int(rl["signal"].iloc[-1])
            if fw_sig and fw_sl > 0 and fw_entry > 0:
                units = brain.risk_position_size(equity, brain.RISK_PER_TRADE_PCT, fw_entry, fw_sl)
                sized_notional = units * fw_entry
                fw_size = sized_notional
                framework_levels[symbol] = {"sl": fw_sl, "tp": fw_tp, "entry": fw_entry}
                print(f">>> FRAMEWORK {symbol} {cfg.get('mode')}: "
                      f"SL={fw_sl:.2f} TP={fw_tp:.2f} -> "
                      f"{units:.2f} units = ${sized_notional:,.0f} (1.5% risk)")
            else:
                sized_notional = 0
                fw_size = None
                framework_levels.pop(symbol, None)
                print(f">>> FRAMEWORK {symbol}: no signal this bar")

        if not fw_mode:
            if recent_mom < -0.03:
                sized_notional *= 0.5
            elif recent_mom > 0.05:
                sized_notional *= 1.1

        action, detail = "WAIT", ""
        # Framework configs must enter ONLY on a live framework signal
        # (fw_size set). current_state() has no _fw branch, so want_in would
        # fall back to the momentum strategy — a momentum buy under a
        # framework config was trading the wrong (unmeasured) strategy.
        fw_ready = fw_mode and fw_size is not None
        try:
            if (fw_ready or (not fw_mode and want_in)) and holding is None and allow_entries:
                if brain.strategy_is_failing(symbol):
                    action, detail = "FAILING-STRATEGY-BLOCKED", "trained loser — skip entry"
                    print(f"  {symbol}: blocked — strategy {cfg.get('label')} has proven losing odds")
                elif not fw_mode and recent_mom < -0.03:
                    action, detail = "MOMENTUM-BLOCKED", f"price dropped {recent_mom*100:+.1f}%"
                else:
                    # Entry-quality gates: fundamental health, volume, liquidity.
                    f_blocked, f_detail = brain.fundamental_gate(symbol, df)
                    v_blocked, _vd = brain.volume_confirm_gate(df)
                    l_blocked, _ld = brain.liquidity_gate(symbol, df)
                    if f_blocked:
                        action, detail = "FUNDAMENTAL-BLOCKED", f_detail
                    elif v_blocked:
                        action, detail = "VOLUME-BLOCKED", "no volume confirmation"
                    elif l_blocked:
                        action, detail = "LIQUIDITY-BLOCKED", "insufficient liquidity"
                    elif symbol in pending_symbols:
                        action, detail = "PENDING", "order working"
                    else:
                        size_mult = 1.0
                        hc = brain.dynamic_heat_cap(live_regime, drawdown)
                        if deployed + sized_notional > hc * equity:
                            _heat_room = (hc * equity) - deployed
                            if _heat_room <= equity * 0.005:
                                # Heat budget fully consumed: entering even the
                                # $200 floor would bust the portfolio heat cap.
                                action, detail = "HEAT-BLOCKED", f"{hc*100:.0f}% cap consumed"
                                print(f">>> HEAT-BLOCKED {symbol}: no room under {hc*100:.0f}% cap ({live_regime})")
                            else:
                                size_mult = min(size_mult, max(0.0, _heat_room / max(sized_notional, 1)))
                                print(f">>> HEAT: sizing down {size_mult:.0%} to respect {hc*100:.0f}% cap ({live_regime})")
                        if conviction < brain.FREE_SAIL_CONVICTION:
                            if conviction < brain.MIN_CONVICTION:
                                action, detail = "LOW-CONVICTION", f"{conviction:.0%} < {brain.MIN_CONVICTION:.0%}"
                            else:
                                size_mult = min(size_mult, 0.6)
                                print(f">>> Conviction {conviction:.0%}: scaling down to 60%")
                        if action == "WAIT" and brain.sector_blocked(positions, symbol, equity):
                            room = brain.sector_room(positions, symbol, equity)
                            size_mult = min(size_mult, max(0.2, room))
                            print(f">>> SECTOR: sizing down to {room:.0%}")
                        if action == "WAIT" and drawdown < -0.02 and conviction < 0.7:
                            size_mult = min(size_mult, 0.55)
                            print(f">>> DRAWDOWN-CONSERVATIVE: sizing down to 55%")
                        if action == "WAIT":
                            sized_notional = max(200, planned_notional * conviction * size_mult)
                            # Pattern memory veto: skip if similar past setups lost money.
                            try:
                                pat = brain.hash_pattern(df, live_regime)
                                hist = brain.check_pattern_memory(symbol, pat)
                                if hist is not None and hist < -2:
                                    action, detail = "PATTERN-MEM", f"similar setups averaged {hist:+.1f}%"
                                elif hist is not None:
                                    # Learned size boost: this exact pattern already
                                    # WON/WON-lost money in the past -> size accordingly.
                                    # (Boost confidence on winners, trim on mild losers.)
                                    if hist >= 2.0:
                                        conviction = min(1.0, conviction + 0.12)
                                        sized_notional = max(200, planned_notional * conviction * size_mult)
                                        print(f">>> PATTERN-BOOST: pattern hist {hist:+.1f}% -> conviction {conviction:.0%}, ${sized_notional:,.0f}")
                                    elif hist >= 0.5:
                                        conviction = min(1.0, conviction + 0.05)
                                        sized_notional = max(200, planned_notional * conviction * size_mult)
                                    elif hist < 0:
                                        conviction = max(0.0, conviction - 0.10)
                                        sized_notional = max(200, planned_notional * conviction * size_mult)
                                        print(f">>> PATTERN-TRIM: pattern hist {hist:+.1f}% -> conviction {conviction:.0%}, ${sized_notional:,.0f}")
                            except Exception:
                                pass

                    if action == "WAIT":
                        # Diversification: don't over-concentrate in correlated assets.
                        try:
                            gate = brain.correlation_gate(closes, symbol, held_symbols)
                            if gate["blocked"]:
                                action, detail = "CORR-BLOCKED", f"{gate['alike']} held assets >70% correlated"
                            elif gate["alike"] == 1:
                                conviction = max(0.0, conviction - 0.15)
                                sized_notional = max(200, planned_notional * conviction * size_mult)
                                print(f">>> CORR-PENALTY: 1 correlated hold -> conviction {conviction:.0%}")
                        except Exception:
                            pass

                    if action == "WAIT":
                        # AI news shield: prefer a stored night-runner verdict, else live.
                        verdict, reason = None, ""
                        try:
                            st = brain.latest_ai_verdict(symbol)
                            if st:
                                verdict, reason, _age = st
                        except Exception:
                            pass
                        if not verdict:
                            try:
                                verdict, reason = news_mod.sentiment(symbol) if news_mod else ("NEUTRAL", "no news module")
                            except Exception:
                                verdict, reason = "NEUTRAL", ""
                        if verdict == "BEARISH":
                            action, detail = "NEWS-BLOCKED", reason[:60]

                    if action == "WAIT":
                        # Framework strategies: the risk-based size is the
                        # authority, but still respect the heat/sector caps so
                        # no single trade breaks the portfolio-level limits.
                        if fw_size is not None:
                            sized_notional = max(200, int(fw_size * size_mult))
                        oid, st, fill_price = smart_buy(symbol, sized_notional, price)
                        if st == "filled" and fill_price:
                            brain.set_position_strategy(symbol, cfg.get("label", "?"))
                            if fw_mode and fw_size is not None:
                                brain.mark_framework_first_open(symbol, cfg.get("label", "?"))
                            # Save framework SL/TP to config so risk_manager can use them.
                            if symbol in framework_levels:
                                _fl = framework_levels[symbol]
                                cfg["fw_sl"] = _fl["sl"]
                                cfg["fw_tp"] = _fl["tp"]
                                cfg["fw_entry"] = _fl["entry"]
                                cfg["symbol"] = symbol
                                brain.save_config(cfg)
                            action, detail = "BUY", f"${sized_notional:,.0f} conv={conviction:.0%}"
                            brain.log_trade(datetime.now().isoformat(timespec="seconds"), symbol, "BUY",
                                            sized_notional, None, None, st, "smart-limit",
                                            fill_price=fill_price, signal_price=price,
                                            strategy=cfg.get("label", "?"))
                            deployed += sized_notional
                        else:
                            # not filled yet — record intent (fill_price=None) so a
                            # later reconcile_trades pass can match and fill it.
                            action, detail = "BUY-QUEUED", f"{symbol} order {st or 'err'} — will reconcile"
                            brain.log_trade(datetime.now().isoformat(timespec="seconds"), symbol, "BUY",
                                            sized_notional, None, None, st or "queued", "smart-limit",
                                            fill_price=None, signal_price=price,
                                            strategy=cfg.get("label", "?"))

            elif holding is not None and brain.is_exit_signal(df, cfg) and cfg.get("mode") != "hold":
                if symbol not in brain.CRYPTO and not market_open:
                    action, detail = "SELL-PENDING", "exit signal — market closed, sell next open"
                else:
                    qty = holding.qty
                    oid, st, fill_price = smart_sell(symbol, qty, price)
                    if st == "filled" and fill_price:
                        brain.clear_position_peak(symbol)
                        action, detail = "SELL", f"exit signal: {qty} x {symbol}"
                        _label = brain.pop_position_strategy(symbol)
                        brain.log_trade(datetime.now().isoformat(timespec="seconds"), symbol, "SELL",
                                        None, float(qty), None, st, "smart-limit",
                                        fill_price=fill_price, signal_price=price,
                                        strategy=_label)
                        brain.log_closed_trade(symbol, _label, float(holding.avg_entry_price), fill_price)
                        brain.analyze_trade_outcome(symbol, _label, float(holding.avg_entry_price),
                                                    fill_price, live_regime)
                        try:
                            brain.learn_on_exit(symbol, float(holding.avg_entry_price),
                                                fill_price, live_regime,
                                                brain.hash_pattern(df, live_regime) if not df.empty else None)
                        except Exception:
                            pass
                        brain.send_alert(f"SELL {symbol} ({_label})")
                    else:
                        action, detail = "SELL-QUEUED", f"{symbol} order {st or 'err'} — position still open"

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

    # Reconcile any orders that were queued earlier and have since filled.
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        filled_orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=100))
        fixed = brain.reconcile_trades(filled_orders)
        for sym, side, fill, slip in fixed:
            print(f"  reconciled: {side} {sym} filled @ ${fill}")
        pos_fixed = brain.reconcile_with_positions(list(client.get_all_positions()))
        for sym, side, fill, slip in pos_fixed:
            print(f"  reconciled (position): {side} {sym} @ ${fill}")
    except Exception as e:
        print(f"  reconcile skipped: {e}")

    print(f"\nFinal equity: ${equity:,.2f}")


if __name__ == "__main__":
    run_cloud()
