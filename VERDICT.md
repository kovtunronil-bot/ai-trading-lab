# AI Trading Robot — October Verdict Contract

Measured go/no-go for moving to real money. No profit promises; only honest,
reproducible evidence. Each Sunday `weekly_report.py` scores these gates and
writes the result into `_reports/weekly_YYYY-MM-DD.txt`.

## Gates (all must PASS)

| Gate | Requirement | Why |
|------|-------------|-----|
| Stability | No full-day outage in the last 14 days (equity_history gap > 60h). | The bot must survive its own environment (GitHub schedule, auto-tune, code). |
| Drawdown | Current drawdown >= -12%. | Risk limits are part of the framework; breaching means the sizing/heat model is wrong. |
| Framework trades | >= 15 closed framework trades AND avg pnl >= +0.25%. | Forward live evidence of positive expectancy on framework trades alone (legacy results don't count). |
| Readiness | readiness_score >= 85/100. | The existing integrity/execution/performance scorecard. |

## Rules of the road until then

- Single tuning path: the measured strategy scan (auto-promotion, strict 4y/3y gate).
  No other config changes. Evolution is disabled from live exactly because it clobbered AAPL.
- No new features without evidence the current one fails. "Improve" = fix a
  measured failure, not add surface area.
- If a gate regresses, the weekly report says NOT READY and the bot keeps paper
  trading — real money is never based on momentum or vibes.

## Snapshot (2026-09-06, first report)

Equity $97,483.65 | DD -2.5% | Readiness 75/100 | Framework closed trades: 0.
Stability: PASS (guardian + tz fix active). Drawdown: PASS.
Framework trades: PENDING — waiting for first exits. Readiness: below gate.
Overall verdict: **PENDING**. Weekly cadence (Sundays 06:00 UTC) tracks this to the line.

Status: GREEN — guardrails hold, bot cadence healthy, no config churn since AAPL fix.