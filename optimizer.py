import yfinance as yf
import pandas as pd

TICKERS = ["AAPL", "MSFT", "TSLA", "NVDA", "SPY"]
START = "2015-01-01"
CAPITAL = 10000
MA_PAIRS = [(10, 50), (20, 100), (50, 200), (5, 20), (30, 150)]


def years(index):
    return (index[-1] - index[0]).days / 365.25


def stats(equity):
    total = float(equity.iloc[-1] / CAPITAL - 1) * 100
    cagr = ((float(equity.iloc[-1]) / CAPITAL) ** (1 / years(equity.index)) - 1) * 100
    mdd = float((equity / equity.cummax() - 1).min()) * 100
    return total, cagr, mdd


prices = {}
for t in TICKERS:
    prices[t] = yf.Ticker(t).history(start=START)["Close"]
    print(f"downloaded {t}: {len(prices[t])} days")

print()
results = []

for t in TICKERS:
    close = prices[t]
    ret = close.pct_change().fillna(0)
    bh_equity = CAPITAL * (1 + ret).cumprod()
    bh_total, bh_cagr, bh_mdd = stats(bh_equity)
    results.append([t, "BUY&HOLD", f"{bh_total:.0f}%", f"{bh_cagr:.1f}%", f"{bh_mdd:.0f}%", "-", f"{bh_total:.0f}%"])

    for fast, slow in MA_PAIRS:
        pos = (close.rolling(fast).mean() > close.rolling(slow).mean()).astype(int).shift(1).fillna(0)
        equity = CAPITAL * (1 + pos * ret).cumprod()
        total, cagr, mdd = stats(equity)
        trades = int(pos.diff().abs().sum())
        diff = total - bh_total
        results.append([t, f"SMA {fast}/{slow}", f"{total:.0f}%", f"{cagr:.1f}%", f"{mdd:.0f}%", str(trades), f"{diff:+.0f}%"])

df = pd.DataFrame(results, columns=["Ticker", "Strategy", "Total", "CAGR/yr", "MaxDD", "Trades", "vs B&H"])
print(df.to_string(index=False))
print()

beats = df[(df["Strategy"] != "BUY&HOLD") & (df["vs B&H"].str.startswith("+"))]
total_combos = len(TICKERS) * len(MA_PAIRS)
print(f"VERDICT: {len(beats)}/{total_combos} strategy runs beat buy-and-hold")
if len(beats):
    print("\nThe winners:")
    print(beats.to_string(index=False))
