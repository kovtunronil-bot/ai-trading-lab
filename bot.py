import sys
import time

import yfinance as yf
import brain

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from keys import API_KEY, SECRET_KEY


client = TradingClient(API_KEY, SECRET_KEY, paper=True)

state = brain.load_state()
if len(sys.argv) > 1 and sys.argv[1] == "resume":
    if state.get("lockdown") and (len(sys.argv) < 3 or sys.argv[2] != "LOCKDOWN"):
        print("Robot is in LOCKDOWN (-20%). Deliberate action required:")
        print("    py bot.py resume LOCKDOWN")
        sys.exit(0)
    state["halted"] = False
    state["lockdown"] = False
    brain.save_state(state)
    print("Circuit breakers CLEARED. Robot is back in business.")

stale = [s for s in brain.ALL if brain.config_is_stale(brain.load_config(s))]
if stale:
    brain.evolve_all(stale)
else:
    for s in brain.ALL:
        cfg = brain.load_config(s)
        print(f"Champion {s}: {cfg['label']} (score {cfg['test_score']}, checked {cfg['updated'][:10]})")
print("=" * 56)

account = client.get_account()
equity = float(account.equity)

clock = client.get_clock()
market_open = bool(clock.is_open)
print(f"US market: {'OPEN' if market_open else 'CLOSED'} | next {'close' if market_open else 'open'} {clock.next_close if market_open else clock.next_open}")
brain.log_journal("ALL", 0.0, "MARKET", f"{'open' if market_open else 'closed'}", equity)

if state["peak_equity"] is None or equity > state["peak_equity"]:
    state["peak_equity"] = equity
peak = float(state["peak_equity"])
drawdown = equity / peak - 1

breaker_level = None
for threshold, name in brain.CIRCUIT_BREAKERS:
    if drawdown <= threshold:
        breaker_level = name

print(f"Equity ${equity:,.2f} | peak ${peak:,.2f} | drawdown {drawdown*100:.1f}%")
print(f"Safety ladder -> now: {breaker_level or 'CLEAR'}")
brain.log_equity(equity, peak)

if state["halted"]:
    tag = "LOCKDOWN" if state.get("lockdown") else "HALT"
    print(f"\n*** ROBOT FROZEN: {tag} ***")
    if tag == "LOCKDOWN":
        print("Revival requires deliberate confirmation:")
        print("    py bot.py resume LOCKDOWN")
    else:
        print("To let it trade again after reviewing things:")
        print("    py bot.py resume")
    brain.log_journal("ALL", 0.0, "FROZEN", f"{tag} | equity ${equity:,.2f}", equity)
    sys.exit(0)


print("\n[DATA PASS] downloading all markets once...")
data = {}
data_4h = {}
for symbol in brain.ALL:
    try:
        df = yf.Ticker(brain.yf_symbol(symbol)).history(period="1y")
        if df.empty or len(df["Close"]) < 30:
            raise ValueError("no price data returned")
        data[symbol] = df
    except Exception as e:
        msg = str(e)[:90]
        print(f"  {symbol}: DATA PROBLEM, skipped today ({msg})")
        brain.log_journal(symbol, 0.0, "DATA-SKIP", msg, equity)
        brain.log_incident("DATA-SKIP", f"{symbol}: {msg}")
    try:
        df_h = yf.Ticker(brain.yf_symbol(symbol)).history(period="60d", interval="1h")
        if df_h is not None and len(df_h) >= 40:
            data_4h[symbol] = df_h.resample("4h").agg({"Open": "first", "High": "max",
                                                        "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    except Exception:
        pass

vols = {}
closes = {}
for symbol, df in data.items():
    vols[symbol] = brain.realized_vol(df["Close"])
    closes[symbol] = df["Close"]

positions = list(client.get_all_positions())

print("\n[INDEPENDENT RISK MANAGER] per-position stops...")
for p in list(positions):
    try:
        entry = float(p.avg_entry_price)
        cur = float(p.current_price)
        if entry > 0 and cur < entry * (1 - brain.POSITION_STOP_LOSS):
            if p.symbol not in brain.CRYPTO and not market_open:
                print(f"  STOPLOSS HIT on {p.symbol} but market closed — will execute at next open run")
                continue
            client.close_position(p.symbol)
            brain.clear_position_peak(p.symbol)
            _lbl = brain.pop_position_strategy(p.symbol)
            brain.log_closed_trade(p.symbol, _lbl, entry, cur)
            brain.analyze_trade_outcome(p.symbol, _lbl, entry, cur,
                                        brain.current_regime(data.get(p.symbol, pd.DataFrame())))
            try:
                _df = data.get(p.symbol, pd.DataFrame())
                _regime = brain.current_regime(_df)
                _pattern = brain.hash_pattern(_df, _regime) if not _df.empty else None
                brain.learn_on_exit(brain.internal_sym(p.symbol), entry, cur, _regime, _pattern)
            except Exception:
                pass
            msg = f"{p.symbol} fell {(cur/entry-1)*100:.1f}% below entry ${entry:.2f} -> force-closed"
            print(f"  STOPLOSS HIT: {msg}")
            brain.log_journal(p.symbol, cur, "STOPLOSS", msg[:100], equity)
            brain.send_alert(f"STOPLOSS | ׳¢׳¦׳™׳¨׳× ׳”׳₪׳¡׳“: {msg}")
            positions.remove(p)
        else:
            print(f"  {p.symbol}: ok ({(cur/entry-1)*100:+.1f}% vs entry)")
    except Exception as e:
        print(f"  check problem on {p.symbol}: {e}")

print("\n[PROFIT LOCK] trailing protection on winners...")
for p in list(positions):
    try:
        entry = float(p.avg_entry_price)
        cur = float(p.current_price)
        peak = brain.update_position_peak(p.symbol, cur, entry)
        if brain.profit_lock_hit(entry, cur, peak):
            if p.symbol not in brain.CRYPTO and not market_open:
                print(f"  {p.symbol} profit-lock hit but market closed — will execute at next open run")
                continue
            client.close_position(p.symbol)
            brain.clear_position_peak(p.symbol)
            _lbl = brain.pop_position_strategy(p.symbol)
            brain.log_closed_trade(p.symbol, _lbl, entry, cur)
            brain.analyze_trade_outcome(p.symbol, _lbl, entry, cur,
                                        brain.current_regime(data.get(p.symbol, pd.DataFrame())))
            try:
                _df = data.get(p.symbol, pd.DataFrame())
                _regime = brain.current_regime(_df)
                _pattern = brain.hash_pattern(_df, _regime) if not _df.empty else None
                brain.learn_on_exit(brain.internal_sym(p.symbol), entry, cur, _regime, _pattern)
            except Exception:
                pass
            msg = f"{p.symbol} peaked at ${peak:.2f}, now ${cur:.2f} -> profit locked"
            print(f"  LOCKED: {msg}")
            brain.log_journal(p.symbol, cur, "PROFIT-LOCK", msg[:100], equity)
            brain.send_alert(f"💰🔒 Profit locked | רווח ננעל: {msg}")
            positions.remove(p)
        else:
            print(f"  {p.symbol}: peak ${peak:.2f}, now ${cur:.2f}")
    except Exception as e:
        print(f"  lock check problem on {p.symbol}: {e}")

print("\n[FLAT TRADE EXIT] freeing stuck capital...")
for p in list(positions):
    try:
        if brain.is_flat_trade(p):
            if p.symbol not in brain.CRYPTO and not market_open:
                print(f"  {p.symbol}: flat {brain.days_in_trade(p)}d — will exit at next open")
                continue
            client.close_position(p.symbol)
            brain.clear_position_peak(p.symbol)
            brain.log_closed_trade(p.symbol, brain.pop_position_strategy(p.symbol),
                                   float(p.avg_entry_price), float(p.current_price))
            try:
                _df = data.get(brain.internal_sym(p.symbol), pd.DataFrame())
                _regime = brain.current_regime(_df)
                _pattern = brain.hash_pattern(_df, _regime) if not _df.empty else None
                brain.learn_on_exit(brain.internal_sym(p.symbol), float(p.avg_entry_price),
                                    float(p.current_price), _regime, _pattern)
            except Exception:
                pass
            msg = f"{p.symbol} flat {brain.days_in_trade(p)}d — freed ${float(p.market_value):,.0f}"
            print(f"  TIME EXIT: {msg}")
            brain.log_journal(p.symbol, float(p.current_price), "FLAT-EXIT", msg[:100], equity)
            brain.send_alert(f"⏰ FLAT EXIT | {msg}")
            positions.remove(p)
        else:
            print(f"  {p.symbol}: held {brain.days_in_trade(p)}d, ok")
    except Exception as e:
        print(f"  flat check problem on {p.symbol}: {e}")

if breaker_level == "LOCKDOWN":
    print("\n!!! LEVEL 3 LOCKDOWN ג€” SELLING EVERYTHING (market orders: certainty first) !!!")
    for p in positions:
        try:
            client.close_position(p.symbol)
            brain.log_journal(p.symbol, 0.0, "LOCKDOWN-SELL", f"closed {p.qty} x {p.symbol}", equity)
            print(f"  closed {p.symbol}")
        except Exception as e:
            print(f"  problem closing {p.symbol}: {e}")
    state["halted"] = True
    state["lockdown"] = True
    brain.save_state(state)
    msg = f"נ”’ LOCKDOWN | ׳ ׳¢׳™׳׳” ׳׳׳׳”: hit {drawdown*100:.1f}%. All sold. Revive ONLY with: py bot.py resume LOCKDOWN"
    brain.send_alert(msg)
    brain.log_journal("ALL", 0.0, "FROZEN", msg[:100], equity)
    print(msg)
    sys.exit(0)

if breaker_level == "HALT":
    print("\n!!! LEVEL 2 HALT ג€” SELLING EVERYTHING (market orders) !!!")
    for p in positions:
        try:
            client.close_position(p.symbol)
            brain.log_journal(p.symbol, 0.0, "HALT-SELL", f"closed {p.qty} x {p.symbol}", equity)
            print(f"  closed {p.symbol}")
        except Exception as e:
            print(f"  problem closing {p.symbol}: {e}")
    state["halted"] = True
    brain.save_state(state)
    msg = f"נ›‘ HALT | ׳¢׳¦׳™׳¨׳”: hit {drawdown*100:.1f}%. All sold. Review then run: py bot.py resume"
    brain.send_alert(msg)
    brain.log_journal("ALL", 0.0, "FROZEN", msg[:100], equity)
    print(msg)
    sys.exit(0)

allow_entries = breaker_level != "CAUTION"
if not allow_entries:
    print("\nLEVEL 1 CAUTION: new entries BLOCKED today.")
    brain.send_alert(f"ג ן¸ CAUTION | ׳–׳”׳™׳¨׳•׳×: {drawdown*100:.1f}% drawdown - no new buys today / ׳׳™׳ ׳§׳ ׳™׳•׳× ׳”׳™׳•׳")

held_symbols = [p.symbol for p in positions]
try:
    all_open_orders = list(client.get_orders())
    pending_symbols = {brain.internal_sym(o.symbol) for o in all_open_orders}
    from datetime import timezone as _tz
    now_utc = datetime.now(_tz.utc)
    for o in all_open_orders:
        age = now_utc - o.submitted_at.replace(tzinfo=_tz.utc) if o.submitted_at else None
        if age and age.total_seconds() > 7200:
            try:
                client.cancel_order_by_id(o.id)
                pending_symbols.discard(brain.internal_sym(o.symbol))
                print(f"  canceled stale order ({age.total_seconds()/3600:.1f}h old): {o.symbol} {o.side}")
            except Exception:
                pass
except Exception:
    all_open_orders = []
    pending_symbols = set()


def cancel_stale_orders(symbol):
    for o in all_open_orders:
        if brain.internal_sym(o.symbol) == brain.internal_sym(symbol):
            try:
                client.cancel_order_by_id(o.id)
                print(f"    canceled stale order: {o.symbol} {o.side} qty={o.qty}")
            except Exception:
                pass


def qty_from_notional(notional, ref_price):
    q = round(float(notional) / max(ref_price, 1e-9), 4)
    return q


def _await_fill(order):
    deadline = time.time() + 90
    while time.time() < deadline:
        o = client.get_order_by_id(order.id)
        st = str(o.status)
        if st in ("filled", "canceled", "expired", "rejected"):
            return st, getattr(o, "filled_avg_price", None)
        if st in ("accepted", "new", "pending_new"):
            elapsed = time.time() - (deadline - 90)
            if elapsed > 30:
                return "queued", getattr(o, "filled_avg_price", None)
        time.sleep(12)
    return "timeout", None


def _tif(symbol):
    if symbol in brain.CRYPTO:
        return TimeInForce.GTC
    return TimeInForce.DAY


def smart_buy(symbol, notional, ref_price):
    tif = _tif(symbol)
    steps = brain.best_limit_pct(symbol, "buy")
    t0 = time.time()
    for lim_pct in steps:
        limit_price = round(ref_price * lim_pct, 2)
        qty = qty_from_notional(notional, limit_price)
        if qty <= 0:
            return None, "rejected", None
        try:
            o = client.submit_order(LimitOrderRequest(
                symbol=symbol, side=OrderSide.BUY, time_in_force=tif,
                qty=qty, limit_price=limit_price))
        except Exception as e:
            print(f"    limit submit failed: {e}")
            continue
        st, fill = _await_fill(o)
        elapsed = time.time() - t0
        if st == "filled":
            brain.log_execution(symbol, "buy", lim_pct, elapsed, "filled")
            print(f"    filled limit @ ${fill or limit_price} (attempt {steps.index(lim_pct)+1}/2)")
            return o.id, "filled", float(fill) if fill else limit_price
        if st == "queued":
            brain.log_execution(symbol, "buy", lim_pct, elapsed, "queued")
            print(f"    order queued for market open @ ${limit_price}")
            return o.id, "queued", None
        brain.log_execution(symbol, "buy", lim_pct, elapsed, "timeout")
        try:
            client.cancel_order_by_id(o.id)
            print(f"    timeout -> canceling ${limit_price} limit, widening...")
        except Exception:
            pass
    o = client.submit_order(MarketOrderRequest(
        symbol=symbol, side=OrderSide.BUY, time_in_force=tif,
        notional=str(round(float(notional)))))
    st, fill = _await_fill(o)
    print("    fallback MARKET order placed (participation guaranteed)")
    return o.id, st or "submitted", float(fill) if fill else None


def smart_sell(symbol, qty, ref_price):
    tif = _tif(symbol)
    steps = brain.best_limit_pct(symbol, "sell", default_steps=[0.999, 0.9975])
    t0 = time.time()
    for lim_pct in steps:
        limit_price = round(ref_price * lim_pct, 2)
        try:
            o = client.submit_order(LimitOrderRequest(
                symbol=symbol, side=OrderSide.SELL, time_in_force=tif,
                qty=float(qty), limit_price=limit_price))
        except Exception as e:
            print(f"    limit sell failed: {e}")
            continue
        st, fill = _await_fill(o)
        elapsed = time.time() - t0
        if st == "filled":
            brain.log_execution(symbol, "sell", lim_pct, elapsed, "filled")
            return o.id, "filled", float(fill) if fill else limit_price
        if st == "queued":
            brain.log_execution(symbol, "sell", lim_pct, elapsed, "queued")
            return o.id, "queued", None
        brain.log_execution(symbol, "sell", lim_pct, elapsed, "timeout")
        try:
            client.cancel_order_by_id(o.id)
        except Exception:
            pass
    o = client.submit_order(MarketOrderRequest(
        symbol=symbol, side=OrderSide.SELL, time_in_force=tif, qty=float(qty)))
    st, fill = _await_fill(o)
    return o.id, st or "submitted", float(fill) if fill else None


actions = {}
for symbol in brain.ALL:
    cfg = brain.load_config(symbol)
    if symbol not in data:
        continue
    df = data[symbol]
    close = df["Close"]
    price = float(close.iloc[-1])

    live_regime = brain.current_regime(df)
    if not brain.config_is_stale(cfg) and cfg.get("regime") and cfg.get("regime") != live_regime:
        old_score = cfg.get("test_score", -99)
        if old_score >= 10.0:
            print(f"\n*** {symbol}: REGIME SHIFT ({cfg['regime']} -> {live_regime}) but strategy score {old_score:.1f} is strong — keeping {cfg['label']}")
            cfg["regime"] = live_regime
            brain.save_config(cfg)
        else:
            print(f"\n*** {symbol}: REGIME SHIFT ({cfg['regime']} -> {live_regime})! Re-evolving...")
            brain.evolve_symbol(symbol)
            cfg = brain.load_config(symbol)
            cfg["regime"] = live_regime
            brain.save_config(cfg)

    auto_label, auto_msg = brain.auto_adjust_strategy(symbol)
    if auto_label:
        print(f"  AUTO-ADJUST: {auto_msg}")
        cfg = brain.load_config(symbol)

    want_in, buy_txt, sell_txt, notes = brain.current_state(df, cfg)
    holding = next((p for p in positions if brain.alpaca_sym(p.symbol) == brain.alpaca_sym(symbol)), None)
    planned_notional = brain.vol_targeted_notional(vols, symbol)

    h4_df = data_4h.get(symbol)
    if h4_df is not None and len(h4_df) >= 30:
        tf_dir, tf_agree = brain.multi_tf_signal(df, h4_df, cfg)
    else:
        tf_dir, tf_agree = "unknown", 0.5

    ensemble_label, ensemble_agree, ensemble_type = brain.ensemble_signal(df, cfg, symbol)
    conviction = brain.conviction_score(symbol, cfg, tf_agree,
                                        ensemble_agree=ensemble_agree,
                                        regime=live_regime)
    sized_notional = planned_notional * conviction
    days_held = brain.days_in_trade(holding) if holding else 0

    mom = brain.momentum_score(df)
    if mom > 0.5:
        sized_notional *= 1.15
    elif mom < -0.5:
        sized_notional *= 0.85

    print(f"\n--- {symbol} | ${price:.2f} | regime: {live_regime} | strategy: {cfg['label']} ---")
    print(f"entry needs : {buy_txt}")
    print(f"exit if     : {sell_txt}")
    print(f"planned size: ${planned_notional:,.0f} | conviction: {conviction:.0%} | sized: ${sized_notional:,.0f}")
    print(f"multi-TF    : daily+4h={tf_dir} (agreement {tf_agree:.0%})")
    print(f"ensemble    : {ensemble_type} ({ensemble_agree:.0%} agree) -> {ensemble_label}")
    print(f"momentum    : {mom:+.2f} ({'strong' if abs(mom) > 1 else 'normal'})")
    if holding:
        print(f"held        : {days_held} days | flat check: {'FLAT — exit candidate' if brain.is_flat_trade(holding) else 'ok'}")
    if notes:
        print("notes       : " + "; ".join(notes))

    action, detail = "WAIT", ""
    try:
        deployed = sum(float(p.market_value) for p in positions)
        recent_mom = brain.recent_momentum(df)
        if want_in and holding is None and allow_entries:
            if recent_mom < -0.03:
                action, detail = "MOMENTUM-BLOCKED", f"price dropped {recent_mom*100:+.1f}% in 5 days — buying falling knife"
                print(f">>> MOMENTUM: {detail}")
            elif symbol in pending_symbols:
                action, detail = "PENDING", "order already working on Alpaca — no duplicates"
                print(f">>> {detail}")
            elif symbol not in brain.CRYPTO and not market_open:
                action, detail = "MARKET-CLOSED", f"signal fired (${planned_notional} planned) — will trade at next open run"
                print(f">>> {detail}")
            else:
                size_mult = 1.0
                size_reasons = []

                hc = brain.dynamic_heat_cap(live_regime, drawdown)
                if deployed + sized_notional > hc * equity:
                    size_mult = min(size_mult, max(0.0, ((hc * equity) - deployed) / max(sized_notional, 1)))
                    size_reasons.append(f"heat cap {hc*100:.0f}%")
                    print(f">>> HEAT: sizing down {size_mult:.0%} to respect {hc*100:.0f}% cap ({live_regime})")

                if conviction < brain.MIN_CONVICTION:
                    action, detail = "LOW-CONVICTION", f"conviction {conviction:.0%} below {brain.MIN_CONVICTION:.0%}"
                    print(f">>> CONVICTION: {conviction:.0%} too low — skipped")
                elif conviction < brain.FREE_SAIL_CONVICTION:
                    size_mult = min(size_mult, 0.6)
                    size_reasons.append(f"medium conviction {conviction:.0%}")
                    print(f">>> Conviction {conviction:.0%}: scaling down to 60%")

                if action == "WAIT" and brain.sector_blocked(positions, symbol, equity):
                    sector_room = brain.sector_room(positions, symbol, equity)
                    size_mult = min(size_mult, max(0.2, sector_room))
                    size_reasons.append(f"sector room {sector_room:.0%}")
                    print(f">>> SECTOR: sizing down to {sector_room:.0%} to respect cap")

                if action == "WAIT" and drawdown < -0.02 and conviction < 0.7:
                    size_mult = min(size_mult, 0.55)
                    size_reasons.append(f"drawdown {drawdown*100:+.1f}% + conv {conviction:.0%}")
                    print(f">>> DRAWDOWN-CONSERVATIVE: sizing down to 55%")

                if action == "WAIT" and brain.detect_divergence(df) == "bearish":
                    size_mult = min(size_mult, 0.5)
                    size_reasons.append("bearish divergence")
                    print(f">>> DIVERGENCE: sizing down to 50% (bearish trap)")

                if action == "WAIT" and brain.avoid_entry_window(symbol)[0]:
                    _, timing_msg = brain.avoid_entry_window(symbol)
                    size_mult = min(size_mult, 0.5)
                    size_reasons.append("late close timing")
                    print(f">>> TIMING: sizing down to 50% ({timing_msg})")

                if action == "WAIT" and brain.should_avoid(symbol, live_regime):
                    action, detail = "LOSS-MEMORY", f"{symbol} has 3+ losses in {live_regime} regime"
                    print(f">>> LOSS MEMORY: avoiding — {detail}")

                if action == "WAIT":
                    sized_notional = max(200, planned_notional * conviction * size_mult)
                    if size_reasons:
                        print(f">>> SIZING: ${sized_notional:,.0f} ({'; '.join(size_reasons)})")
                    pattern = brain.hash_pattern(df, live_regime)
                    hist_pnl = brain.check_pattern_memory(symbol, pattern)
                    if hist_pnl is not None and hist_pnl < -2:
                        action, detail = "PATTERN-MEM", f"similar past setups averaged {hist_pnl:+.1f}%"
                        print(f">>> PATTERN MEMORY: historical loss — {detail}")
                    else:
                        gate = brain.correlation_gate(closes, symbol, held_symbols)
                        corr_penalty = 0.0
                        if gate["blocked"]:
                            action, detail = "CORR-BLOCKED", f"{gate['alike']} held assets >70% correlated (avg {gate['avg']})"
                            print(f">>> CORRELATION RISK MANAGER: entry BLOCKED — {detail}")
                        else:
                            if gate["alike"] == 1:
                                corr_penalty = 0.15
                                print(f"    correlation penalty: -15% (1 correlated hold)")
                            if corr_penalty > 0:
                                conviction = max(0.0, conviction - corr_penalty)
                                sized_notional = planned_notional * conviction * size_mult
                                print(f">>> CORR-SIZE: conviction {conviction:.0%}, sized ${sized_notional:,.0f}")
                            news_verdict = "NEUTRAL"
                            try:
                                import news as news_mod
                                verdict, reason = news_mod.sentiment(symbol)
                                news_verdict = verdict
                                print(f">>> NEWS SHIELD: {verdict} — {reason}")
                                brain.log_journal(symbol, price, "NEWS", f"{verdict}: {reason}"[:100], equity)
                                if verdict == "BEARISH":
                                    action, detail = "NEWS-BLOCKED", f"bearish headlines: {reason[:80]}"
                                    print(">>> NEWS SHIELD says NO — entry canceled")
                                    brain.send_alert(f"NEWS-BLOCKED: {symbol}")
                                    brain.log_journal(symbol, price, action, detail, equity)
                                    actions[symbol] = (action, detail)
                                    continue
                            except Exception as e:
                                print(f"    news check skipped ({e})")
                            if news_verdict != "NEUTRAL":
                                conviction = brain.conviction_score(symbol, cfg, tf_agree,
                                                                    news_verdict=news_verdict,
                                                                    ensemble_agree=ensemble_agree,
                                                                    regime=live_regime)
                                sized_notional = planned_notional * conviction * size_mult
                                print(f">>> NEWS-CONVICTION: {news_verdict} -> conviction {conviction:.0%}, sized ${sized_notional:,.0f}")
                            try:
                                import vision
                                if vision.model_available():
                                    eyes = vision.analyze_chart(symbol, df)
                                    print(f">>> EYES say:\n    " + eyes.replace("\n", "\n    "))
                                    brain.log_journal(symbol, price, "EYES", eyes.replace("\n", " | ")[:100], equity)
                                else:
                                    eyes = "(vision model not installed)"
                            except Exception as e:
                                eyes = f"(eyes error: {e})"
                                print(f"    {eyes}")
                            cancel_stale_orders(symbol)
                            oid, st, fill_price = smart_buy(symbol, sized_notional, price)
                            action, detail = "BUY", f"{symbol} ${sized_notional:,.0f} conv={conviction:.0%} (corr {gate['avg']})"
                            print(f">>> EXECUTED BUY {detail}")
                            held_symbols.append(symbol)
                            from datetime import datetime as _dt
                            brain.log_trade(_dt.now().isoformat(timespec="seconds"), symbol, "BUY",
                                            sized_notional, None, None, st, "smart-limit",
                                            fill_price=fill_price, signal_price=price,
                                            strategy=cfg.get("label", "?"))
                            brain.set_position_strategy(symbol, cfg.get("label", "?"))
                            brain.send_alert(f"נ’° BUY | ׳§׳ ׳™׳™׳” {symbol} ${planned_notional} @ ${fill_price or price}")
        elif want_in and holding is None and not allow_entries:
            action, detail = "BLOCKED", "CAUTION mode blocks entries"
            print(">>> signal fired but risk manager says NO new entries today")
        elif holding is not None and brain.is_flat_trade(holding):
            if symbol not in brain.CRYPTO and not market_open:
                action, detail = "FLAT-EXIT-PENDING", f"flat {brain.days_in_trade(holding)}d — will sell at next open"
                print(f">>> {detail}")
                brain.log_journal(symbol, price, action, detail, equity)
                actions[symbol] = (action, detail)
                continue
            qty = holding.qty
            cancel_stale_orders(symbol)
            oid, st, fill_price = smart_sell(symbol, qty, price)
            brain.clear_position_peak(symbol)
            action, detail = "FLAT-EXIT", f"flat {brain.days_in_trade(holding)}d — freed ${float(holding.market_value):,.0f}"
            print(f">>> TIME EXIT: {detail}")
            held_symbols = [h for h in held_symbols if h != symbol]
            from datetime import datetime as _dt
            brain.log_trade(_dt.now().isoformat(timespec="seconds"), symbol, "SELL",
                            None, float(qty), None, st, "smart-limit",
                            fill_price=fill_price, signal_price=price,
                            strategy=brain.get_position_strategy(symbol))
            _label = brain.pop_position_strategy(symbol)
            brain.log_closed_trade(symbol, _label, float(holding.avg_entry_price),
                                   fill_price or price)
            brain.analyze_trade_outcome(symbol, _label, float(holding.avg_entry_price),
                                        fill_price or price, live_regime)
            try:
                _pattern = brain.hash_pattern(df, live_regime) if not df.empty else None
                brain.learn_on_exit(symbol, float(holding.avg_entry_price),
                                    fill_price or price, live_regime, _pattern)
            except Exception:
                pass
            brain.send_alert(f"EXIT | flat exit: {symbol} ({brain.days_in_trade(holding)}d)")
        elif brain.is_exit_signal(df, cfg) and cfg["mode"] != "hold" and holding is not None:
            if symbol not in brain.CRYPTO and not market_open:
                action, detail = "EXIT-PENDING", "exit signal fired — market closed, will sell at next open run"
                print(f">>> {detail}")
                brain.log_journal(symbol, price, action, detail, equity)
                actions[symbol] = (action, detail)
                continue
            qty = holding.qty
            cancel_stale_orders(symbol)
            oid, st, fill_price = smart_sell(symbol, qty, price)
            brain.clear_position_peak(symbol)
            action, detail = "SELL", f"closed {qty} x {symbol}"
            print(f">>> EXECUTED SELL ג€” closed {detail}")
            held_symbols = [h for h in held_symbols if h != symbol]
            from datetime import datetime as _dt
            brain.log_trade(_dt.now().isoformat(timespec="seconds"), symbol, "SELL",
                            None, float(qty), None, st, "smart-limit",
                            fill_price=fill_price, signal_price=price,
                            strategy=brain.get_position_strategy(symbol))
            _label = brain.pop_position_strategy(symbol)
            brain.log_closed_trade(symbol, _label, float(holding.avg_entry_price),
                                   fill_price or price)
            brain.analyze_trade_outcome(symbol, _label, float(holding.avg_entry_price),
                                        fill_price or price, live_regime)
            try:
                _pattern = brain.hash_pattern(df, live_regime) if not df.empty else None
                brain.learn_on_exit(symbol, float(holding.avg_entry_price),
                                    fill_price or price, live_regime, _pattern)
            except Exception:
                pass
            brain.send_alert(f"📩 SELL | מכירה {symbol} (paper)")
        elif holding is not None:
            action, detail = "HOLD", f"{holding.qty} x {symbol} @ ${float(holding.market_value):,.2f}"
            print(f"status: invested ({detail}), waiting for exit signal")
            atr_val = float(brain._atr14(df).iloc[-1]) if len(df) >= 20 else None
            stop, target = brain.trailing_profit_targets(
                float(holding.avg_entry_price), price, atr=atr_val)
            if stop:
                print(f"trailing   : stop ${stop:.2f} | target ${target or 0:.2f}")
                if price <= stop:
                    if symbol not in brain.CRYPTO and not market_open:
                        action, detail = "TRAIL-EXIT-PENDING", f"trailing stop ${stop:.2f} hit — sell at next open"
                        print(f">>> TRAILING STOP: {detail}")
                    else:
                        qty = holding.qty
                        cancel_stale_orders(symbol)
                        oid, st, fill_price = smart_sell(symbol, qty, price)
                        brain.clear_position_peak(symbol)
                        action, detail = "TRAIL-EXIT", f"trailing stop ${stop:.2f} hit"
                        print(f">>> TRAILING STOP: {detail}")
                        held_symbols = [h for h in held_symbols if h != symbol]
                        from datetime import datetime as _dt
                        brain.log_trade(_dt.now().isoformat(timespec="seconds"), symbol, "SELL",
                                        None, float(qty), None, st, "smart-limit",
                                        fill_price=fill_price, signal_price=price,
                                        strategy=brain.get_position_strategy(symbol))
                        _label = brain.pop_position_strategy(symbol)
                        brain.log_closed_trade(symbol, _label, float(holding.avg_entry_price),
                                               fill_price or price)
                        brain.analyze_trade_outcome(symbol, _label, float(holding.avg_entry_price),
                                                    fill_price or price, live_regime)
                        try:
                            _pattern = brain.hash_pattern(df, live_regime) if not df.empty else None
                            brain.learn_on_exit(symbol, float(holding.avg_entry_price),
                                                fill_price or price, live_regime, _pattern)
                        except Exception:
                            pass
                        brain.send_alert(f"TRAIL-EXIT | trailing stop: {symbol}")
            if action == "HOLD" and want_in:
                can_scale, scale_reason = brain.should_scale_in(holding, conviction, df)
                if can_scale:
                    deployed_now = sum(float(p.market_value) for p in positions)
                    add_size = min(sized_notional * 0.4, brain.dynamic_heat_cap(live_regime, drawdown) * equity - deployed_now)
                    if add_size > 200:
                        cancel_stale_orders(symbol)
                        oid, st, fill_price = smart_buy(symbol, add_size, price)
                        action, detail = "SCALE-IN", f"{symbol} +${add_size:,.0f} ({scale_reason})"
                        print(f">>> SCALE-IN: {detail}")
                        brain.log_journal(symbol, price, action, detail, equity)
        else:
            reason = "filters blocked entry" if any("blocked" in n for n in notes) else "no signal yet"
            action, detail = "WAIT", reason
            print(f"status: in cash ג€” {reason}")
    except Exception as e:
        action, detail = "ERROR", str(e)[:120]
        print(f"ORDER PROBLEM: {e}")
        brain.log_incident("ORDER-ERROR", f"{symbol}: {e}")

    brain.log_journal(symbol, price, action, detail, equity)
    actions[symbol] = (action, detail)

print("\n" + "=" * 56)
print(f"Total paper equity: ${equity:,.2f} | lab.db + lessons updated")
brain.log_equity(equity, peak)

print("\n[SELF-LEARNING] analyzing trades...")
try:
    import learn
    learn.learn_from_open_trades()
    learn.track_regime_performance()
    learn.update_kelly_from_trades()
    print("  learning complete")
except Exception as e:
    print(f"  learn error: {e}")

positions_now = list(client.get_all_positions())

print("\n[SELF-AUDIT] claims vs reality...")
sold_claims = [s for s, (a, _) in actions.items() if a in ("SELL", "PROFIT-LOCK", "STOPLOSS")]
for s in sold_claims:
    if any(brain.alpaca_sym(p.symbol) == brain.alpaca_sym(s) for p in positions_now):
        brain.log_incident("PHANTOM-SELL", f"{s} journaled closed but position still exists")
        print(f"  !! DISCREPANCY: {s} claimed-sold yet still held — incident logged")
bought_claims = [s for s, (a, _) in actions.items() if a == "BUY"]
for s in bought_claims:
    held = any(brain.alpaca_sym(p.symbol) == brain.alpaca_sym(s) for p in positions_now)
    pend = s in pending_symbols
    if not held and not pend and market_open:
        brain.log_incident("PHANTOM-BUY", f"{s} journaled bought but no position/order found")
        print(f"  !! DISCREPANCY: {s} claimed-bought yet missing — incident logged")
skips = [1 for _, (a, _) in actions.items() if a == "ERROR"]
if skips:
    brain.log_incident("RUN-ERROR", f"{len(skips)} order errors this run")
if not (any("DISCREPANCY" in l for l in []) ):
    print("  audit complete")

try:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    filled_orders = client.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, limit=100))
    fixed = brain.reconcile_trades(filled_orders)
    for sym, side, fill, slip in fixed:
        print(f"reconciled: {side} {sym} filled @ ${fill} (slippage {slip} bps)")
except Exception as e:
    print(f"reconcile skipped: {e}")
try:
    fixed2 = brain.reconcile_with_positions(positions_now)
    for sym, side, fill, slip in fixed2:
        print(f"reconciled (position): {side} {sym} @ ${fill} (slippage {slip} bps)")
except Exception as e:
    print(f"position reconcile skipped: {e}")
brain.save_positions_snapshot(positions_now)
dest = brain.backup_db()
if dest:
    print(f"backup saved: {dest}")

traded = [s for s, (a, _) in actions.items() if a in ("BUY", "SELL")]
quiet = "quiet" in sys.argv
if traded or not quiet:
    brain.send_alert(f"נ₪– Robot OK | ׳”׳¨׳•׳‘׳•׳˜ ׳¢׳‘׳“ | markets/׳©׳•׳•׳§׳™׳: {len(brain.ALL)} | traded/׳׳¡׳—׳¨: {', '.join(traded) if traded else 'none/׳›׳׳•׳'} | equity/׳”׳•׳ ${equity:,.0f}")

try:
    import dashboard
    html = dashboard.build_html()
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)
except Exception as e:
    print(f"dashboard refresh skipped: {e}")

