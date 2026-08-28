import sys

import yfinance as yf

import brain

from alpaca.trading.client import TradingClient
from keys import API_KEY, SECRET_KEY


CRYPTO_MARKETS = ["BTC/USD", "ETH/USD"]


def guard_symbol(client, symbol, equity):
    cfg = brain.load_config(symbol)
    if not cfg:
        print(f"{symbol}: no config yet — skipping")
        return
    df = yf.Ticker(brain.yf_symbol(symbol)).history(period="1y")
    if df.empty or len(df["Close"]) < 30:
        print(f"{symbol}: no data — skipping")
        return

    price = float(df["Close"].iloc[-1])
    positions = client.get_all_positions()
    pos = next((p for p in positions if p.symbol == symbol), None)
    want_in, _, _, _ = brain.current_state(df, cfg)

    if pos is None:
        print(f"{symbol}: ${price:,.2f} | not holding | entry needs {want_in} (guard never buys)")
        return

    entry = float(pos.avg_entry_price)
    qty = float(pos.qty)
    cur = float(pos.current_price)
    peak = brain.update_position_peak(symbol, cur, entry)

    reasons = []
    if entry > 0 and cur < entry * (1 - brain.POSITION_STOP_LOSS):
        reasons.append(f"STOPLOSS {(cur/entry-1)*100:.1f}% vs entry")
    if brain.profit_lock_hit(entry, cur, peak):
        reasons.append(f"PROFIT-LOCK peaked ${peak:.2f} now ${cur:.2f}")
    if cfg["mode"] != "hold" and not want_in:
        reasons.append("exit signal fired")

    if not reasons:
        print(f"{symbol}: holding {qty} @ ${entry:.2f}, now ${cur:.2f}, peak ${peak:.2f} — all calm")
        return

    client.close_position(symbol)
    brain.clear_position_peak(symbol)
    label = brain.pop_position_strategy(symbol)
    brain.log_closed_trade(symbol, label, entry, cur)
    msg = f"⚡ GUARD closed {symbol}: " + "; ".join(reasons)
    print(msg)
    brain.log_journal(symbol, cur, "GUARD-SELL", msg[:100], equity)
    brain.send_alert(f"⚡ Crypto Guard | שומר הקריפטו: {msg}")


def main():
    sys.stdout.reconfigure(errors="replace")
    client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    equity = float(client.get_account().equity)
    for symbol in CRYPTO_MARKETS:
        try:
            guard_symbol(client, symbol, equity)
        except Exception as e:
            print(f"{symbol}: guard problem — {str(e)[:100]}")


if __name__ == "__main__":
    main()
