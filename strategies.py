import numpy as np
import yfinance as yf
import pandas as pd

TICKERS = ["AAPL", "MSFT", "TSLA", "NVDA", "SPY", "QQQ"]
START = "2015-01-01"
CAPITAL = 10000


def years(index):
    return (index[-1] - index[0]).days / 365.25


def stats(equity):
    total = float(equity.iloc[-1] / CAPITAL - 1) * 100
    cagr = ((float(equity.iloc[-1]) / CAPITAL) ** (1 / years(equity.index)) - 1) * 100
    mdd = float((equity / equity.cummax() - 1).min()) * 100
    return total, cagr, mdd


def equity_from_position(close, pos):
    ret = close.pct_change().fillna(0)
    shifted = pos.shift(1).fillna(0)
    return CAPITAL * (1 + shifted * ret).cumprod()


def rsi_position(close, period, buy_below, sell_above):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    sig = pd.Series(np.nan, index=close.index)
    sig[rsi < buy_below] = 1
    sig[rsi > sell_above] = 0
    return sig.ffill().fillna(0)


def donchian_position(close, entry_days, exit_days):
    upper = close.rolling(entry_days).max().shift(1)
    lower = close.rolling(exit_days).min().shift(1)
    sig = pd.Series(np.nan, index=close.index)
    sig[close > upper] = 1
    sig[close < lower] = 0
    return sig.ffill().fillna(0)


STRATEGIES = {
    "B&H": lambda c: pd.Series(1.0, index=c.index),
    "RSI2 dip<10 sell>65": lambda c: rsi_position(c, 2, 10, 65),
    "RSI2 dip<5 sell>60": lambda c: rsi_position(c, 2, 5, 60),
    "RSI14 dip<30 sell>55": lambda c: rsi_position(c, 14, 30, 55),
    "Breakout 20d high": lambda c: donchian_position(c, 20, 10),
    "Breakout 55d high": lambda c: donchian_position(c, 55, 20),
}

prices = {t: yf.Ticker(t).history(start=START)["Close"] for t in TICKERS}
print(f"data ok: {len(TICKERS)} stocks since {START}\n")

results = []
for t in TICKERS:
    close = prices[t]
    bh_equity = equity_from_position(close, STRATEGIES["B&H"](close))
    bh_total, bh_cagr, bh_mdd = stats(bh_equity)
    results.append([t, "BUY&HOLD", f"{bh_total:.0f}%", f"{bh_mdd:.0f}%", "-", f"{bh_total:.0f}%"])

    for name, fn in STRATEGIES.items():
        if name == "B&H":
            continue
        equity = equity_from_position(close, fn(close))
        total, cagr, mdd = stats(equity)
        trades = int(fn(close).diff().abs().sum())
        results.append([t, name, f"{total:.0f}%", f"{mdd:.0f}%", str(trades), f"{total - bh_total:+.0f}%"])

df = pd.DataFrame(results, columns=["Ticker", "Strategy", "Total", "MaxDD", "Trades", "vs B&H"])
print(df.to_string(index=False))

beats = df[(df["Strategy"] != "BUY&HOLD") & (df["vs B&H"].str.startswith("+"))]
n_runs = len(TICKERS) * (len(STRATEGIES) - 1)
print(f"\nVERDICT: {len(beats)}/{n_runs} beat buy-and-hold")
if len(beats):
    print("\nThe winners:")
    print(beats.to_string(index=False))
