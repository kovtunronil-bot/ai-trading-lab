import yfinance as yf
import pandas as pd

TICKER = "AAPL"
START = "2021-01-01"
CAPITAL = 10000

data = yf.Ticker(TICKER).history(start=START)
close = data["Close"]

sma_fast = close.rolling(50).mean()
sma_slow = close.rolling(200).mean()

position = (sma_fast > sma_slow).astype(int).shift(1).fillna(0)

asset_ret = close.pct_change().fillna(0)
strategy_ret = position * asset_ret

buy_hold_equity = CAPITAL * (1 + asset_ret).cumprod()
strategy_equity = CAPITAL * (1 + strategy_ret).cumprod()

trades = int(position.diff().abs().sum())
time_in_market = float(position.mean()) * 100


def max_drawdown(equity):
    return float((equity / equity.cummax() - 1).min()) * 100


def years_of_data(index):
    return (index[-1] - index[0]).days / 365.25


def annual_return(final_value):
    return ((final_value / CAPITAL) ** (1 / years_of_data(close.index)) - 1) * 100


print("=" * 46)
print(f" BACKTEST: {TICKER}   {close.index[0].date()} -> {close.index[-1].date()}")
print(f" Starting capital: ${CAPITAL:,.0f}")
print("=" * 46)
print(f"{'':16}{'> Strategy':>14}{'Buy & hold':>13}")
print("-" * 46)
print(f"{'Final value':16}${strategy_equity.iloc[-1]:>12,.0f}${buy_hold_equity.iloc[-1]:>12,.0f}")
print(f"{'Total return':16}{(strategy_equity.iloc[-1]/CAPITAL-1)*100:>13.1f}%{(buy_hold_equity.iloc[-1]/CAPITAL-1)*100:>12.1f}%")
print(f"{'Per year (CAGR)':16}{annual_return(strategy_equity.iloc[-1]):>13.1f}%{annual_return(buy_hold_equity.iloc[-1]):>12.1f}%")
print(f"{'Max drawdown':16}{max_drawdown(strategy_equity):>13.1f}%{max_drawdown(buy_hold_equity):>12.1f}%")
print("-" * 46)
print(f" Trades made: {trades} | Time invested: {time_in_market:.0f}% of days")

today_signal = "INVESTED (holding)" if position.iloc[-1] == 1 else "IN CASH (sold)"
print(f" Current signal: {today_signal}")
