# Paper = Science Instrument — Statistical Learning that Outpaces Human Discipline

Date: 2026-09-22
Status: Approved (sections 1-4) — pending implementation plan
Type: Architectural

## Vision (user-stated, in their words)

"I want the bot to be better than a human" = **a statistical edge plus a discipline no
human can sustain**. Concretely: scan many markets continuously, measure statistically
which strategies actually win, and refuse low-odds trades — things a person cannot keep
up because of fatigue, emotion, and attention limits.

The user identified the current biggest blocker: **the bot learns too slowly — entries
are starved by safety gates**. In the paper account the risk is fictional, so gates that
block entries (heat cap, conviction, breaker, correlation, news, pattern-memory veto)
only sacrifice statistical sample size. Real-money safety is not affected by what we
measure in paper.

## Approach (approved)

**"Paper = science instrument."** The executed behavior of the bot (sizes, SL/TP,
risk-manager exits, global emergency breakers) stays identical whether relaxed or not.
The only change in paper: **entry-block gates stop blocking and start tagging.** A trade
that a gate would have vetoed is instead entered (at its live-rule size) and the open
trade row records a `tags` value. This yields a controlled experiment: the same fill,
measured with and without each gate's verdict, so we can quantify *whether each gate
pays for itself* — the evidence base for pruning or keeping gates in real money.

Three alternatives were declined:
1. **Decoupled measurement (signals pipeline)** — keep live behavior untouched, log
   vetoed signals offline and mark their hypothetical outcomes. Rejected: heavy build
   (offline outcomes engine) and hypothetical fills have their own assumptions.
2. **Faster harvesting only** (more scan frequency / more markets, gates unchanged).
   Rejected: does not fix the n-starvation — the heat cap blocks most signals anyway.
3. **Full autonomy rewrite.** Rejected by user: too aggressive for now.

## Design Layers

### Layer 1 — What relaxes vs. what stays (approved)

Principle: **decision-affine mechanics and exits stay identical; entry-block gates relax
to tags in paper only.**

Relax (entry executes anyway, trade is tagged):

| Gate | Tag |
|---|---|
| Portfolio heat cap full-block (`HEAT-BLOCKED`) | `heat` |
| Conviction < MIN_CONVICTION block (`LOW-CONVICTION`) | `low_conv` |
| Regime hard-block (`REGIME-BLOCKED` in BEAR_TREND/BEAR_RANGE/HIGH_VOL) | `hard_regime:<name>` |
| Correlation block (`CORR-BLOCKED`) | `corr` |
| News bearish veto (`NEWS-BLOCKED`) | `news` |
| Pattern-memory veto (`PATTERN-MEM` hist < -2) | `patmem` |
| Legacy momentum block (`MOMENTUM-BLOCKED`) | `momentum` |

Always block (no relaxation, in any environment):

- `strategy_is_failing` — this IS the learning: never keep trading a strategy with
  proven losing odds. It is the edge mechanism, not a safety filter.
- Global `allow_entries` veto (HALT / LOCKDOWN / daily loss-limit / intraday
  kill-switch) — systemic protection, not a per-trade quality judgement.
- Fundamental / volume / liquidity gates (F/V/L) — market-hygiene; they exist so a
  fill is real and liquid, not alpha filtering.
- `PENDING` (order already working) skip — not a gate; concurrency hygiene.

Sizing stays exactly as live rules dictate (including CAUTION half-size, heat
scale-down, conviction scale-down, sector/correlation size adjustments, drawdown
conservative scaling). Tags record *entry* provenance only; exits are identical for
every cohort.

### Layer 2 — Tagging and data model (approved)

- `trades.tags` — new TEXT column (comma-separated, e.g. `heat,hard_regime:BEAR_TREND`).
  Empty string for untagged. Idempotent migration via `ALTER TABLE ... ADD COLUMN` in
  `brain.init_db`, same pattern as `slippage_bps`/`strategy`/`signal_price`.
- `trade_pnl.tags` — same column. Copied at close time from the open BUY row's tags
  (single-writer, one position per symbol → the latest BUY row for the symbol before
  the exit TS is authoritative).
- `brain.log_trade(..., tags="")` — extended signature (defaults empty, non-breaking).
- `brain.gate_decision(gate_name, would_block, relaxed, tags)` — pure helper used by
  every relaxable gate branch. Returns `(enter, tags)`. Unit-testable; cloud_bot is a
  thin caller.
- `brain.framework_stats(cohort="clean" | "full" | "by_tag")` — n / avg / sum over
  framework closed trades filtered by tags:
  - `clean` = tags empty (would have entered under full live gates),
  - `full` = all framework trades (incl. gated),
  - `by_tag` = per-tag sub-stats.
- `brain.expectancy_ledger(trades)` — pure function returning the cohort matrix (all
  cohorts listed) that feeds the weekly report and dashboard.

Framework-strategy qualification: replace the crude `strategy LIKE '%flag%' OR '%orb%'`
filter with a single explicit `FRAMEWORK_STRATEGIES` membership test used consistently
by the cohort stats and the October gate.

### Layer 3 — Go-live gate semantics and the n-acceleration engine (approved)

- The October Performance gate measures the **`clean` cohort**: n>=15, avg pnl_pct
  >= +0.25%. "What will trade in real money equals what is counted." Unchanged in
  spirit; only now it is computed from the clean subset.
- The acceleration comes from **evidence-driven pruning, with human approval at every
  step**:
  - Each week the report renders the *expectancy ledger* and the *pruning
    recommendations*: for each gate, does its tagged cohort underperform clean?
    - materially worse → the gate is real protection; keep it.
    - not worse (or better) → the gate costs n without protection; propose relaxing
      it in LIVE, subject to explicit user sign-off. Never automatic.
  - Pruning widens the clean cohort over time, which is the real accelerator — n stops
    "waiting for luck" and grows by measured, approved gate reductions.

### Layer 4 — Implementation, testing, rollout (approved)

Implementation (TDD, `unittest`, monkeypatch `brain.DB_FILE`/`brain._DB_CONN`):

1. `init_db` migration: `trades.tags` + `trade_pnl.tags` (idempotent, fresh + existing DB).
2. `brain.gate_decision(gate_name, would_block, relaxed, tags)`.
3. `brain.framework_stats(cohort)` + `FRAMEWORK_STRATEGIES` + `brain.expectancy_ledger`.
4. `cloud_bot`: `PAPER_RELAXED` env flag (default 0); in each relaxable gate branch,
   use `gate_decision`; pass `tags` into `log_trade` (both BUY and BUY-QUEUED paths);
   propagate tags to trade_pnl at close; journal detail may append tag list for live
   observability.
5. `weekly_report.py` section [5]/[6]: expectancy ledger + pruning recommendations,
   October gate computed on the `clean` cohort.
6. `dashboard.py`: render cohort ledger.

Tests: migration (new + legacy DBs), `log_trade` stores tags, `framework_stats` three
cohorts, `gate_decision` (relaxed/non-relaxed × block/allow), `expectancy_ledger`
grouping, propagation of tags from trades to trade_pnl at close, weekly gate on clean.

Rollout:

- Push all changes to `master` via the established temp-worktree push pattern.
- `bot.yml` sets `PAPER_RELAXED: 1` for the paper environment only; a comment marks
  that go-live flips it to 0.
- Dispatch a run; verify: gated setups now enter in paper with tags observable in the
  journal/log, and the dashboard/weekly ledger renders.
- This feature changes paper trading behavior only. Real-money behavior is untouched
  until explicit user approval backed by ledger evidence.

## Constraints / Notes

- Cloud single-writer: DB/config/state are committed only by cloud workflows; the local
  repo stays stale; never sync local configs to cloud.
- All changes go through TDD; `pytest` is not installed — `python -m unittest`.
- All pushes use the fetch-with-token → temp worktree → copy → commit → push
  `HEAD:master` → remove worktree pattern.
- The existing CAUTION breaker behavior (allow entries at half size, user-approved)
  is unchanged by this design; sizes stay live-identical in relaxed mode.

## Non-Goals

- No changes to strategy logic, indicators, signal generation, or SL/TP exit rules.
- No automatic live-gate relaxation: pruning is report-driven and requires explicit
  user approval each time.
- No change to real-money safeguards (HALT/LOCKDOWN, daily limits, kill-switch,
  strategy_is_failing, F/V/L hygiene).