# Sprint 23 Closeout Report — Research Integrity, Benchmark Validation & Release Readiness

Status: **MERGED — PR #2, merge commit `461b899` (feature commit `3644680`), required check `quality-gate` passed, no bypass. Manual UI verification was only partial (section 6).** Nothing here is a claim that any strategy has an edge. The locked holdout has not been evaluated and was not touched.

## 1. Repository and release audit

- Sprint 23 merged via PR #1 (merge commit `bbd8273`, feature commit `7a92dc3`); CI runs #5 (PR) and #6 (main) passed on GitHub Actions, Python 3.12.
- **Branch protection on `main` (read-only inspection of the repository settings, 2026-11-04):**
  - Require status checks: **yes**, required check `quality-gate`.
  - Require a pull request before merging: **no**.
  - Require branches to be up to date: no.
  - Do not allow bypassing the above settings: **no** — administrators can bypass (this is how an earlier direct push to `main` succeeded, GitHub printing "Bypassed rule violations").
  - Allow force pushes: no. Allow deletions: no.
  - Repository rulesets: not inspected.
- **Not changed by me.** Recommended owner actions: enable "Require a pull request before merging", enable "Do not allow bypassing the above settings", and keep force-push/deletion disabled. Until then the protection is advisory for the repository owner.

## 2. Real-data validation (24 combinations)

`scripts/closeout_validation.py` (isolated directory `/tmp/atp-sprint23-research`, no holdout code path, no synthetic substitution). Run on the owner's machine on 2026-11-04, window 2015-01-01 to 2026-11-03, engine v3, provider Yahoo Finance (all rows labelled REAL). **Result: 24 of 24 COMPLETED, 0 data-audit failures, 24 of 24 replays REPRODUCED.** Per combination the script records provider, requested/returned range, snapshot id and hash, data-audit checks, development/validation scorecards, walk-forward (fixed 504/126 bar folds, not tuned), replay result and a classification (full table in `closeout_report.md`).

The first two attempts are part of the record because they changed the platform:
1. All 24 failed `DATA_INVALID`: Yahoo returned a price-less placeholder bar (volume present, open/high/low/close all NaN) for 2026-11-03. The provider now drops rows with *no* price and logs a warning; rows with only some prices missing are still rejected.
2. Three combinations were labelled `DATA_QUALITY_FAILURE`, and the earlier DIA/AAPL `DATA_INVALID` errors, were ~1e-13 float noise in Yahoo's adjusted prices (high equals close at 4 decimals). The platform validation now uses a 1e-9 relative tolerance (a genuine 0.01% error is still rejected) and the audit uses the same constant, counting within-tolerance bars separately.

## 3. Equity-timing quirk — root cause and fix

See ADR-0062. Defect confirmed: under next-bar-open fills equity at the signal bar contained a fill that happens at the next open. Fixed as an observation-time correction (engine v3); fills, trades and final equity unchanged. Hand-calculable regression tests in `tests/test_equity_timing.py` (entry, hold, exit, costs on the fill bar, still-open position, no-trade, same-bar fills unchanged). Earlier run records are untouched and classified `ENGINE_CHANGED`.

**Consequence for interpretation:** any equity-derived figure (Sharpe, volatility, drawdown, window returns) produced before engine v3 differs by up to one bar's gap per trade and must not be compared with v3 results. Pre-v3 numbers from the first real-data run should be treated as superseded.

## 4. Benchmarks and risk metrics

Three comparators kept apart: full buy-and-hold, exposure-matched buy-and-hold, and the active strategy. Added benchmark volatility/Sharpe/max drawdown, exposure-matched return, excess versus exposure-matched, and mean/peak capital deployed (already present: strategy volatility, Sharpe, Sortino, drawdown, exposure time, trade count/sample class, holding period, gross/net, net at 3x costs). Hand-calculable tests in `tests/test_strategy_scorecard.py`. The Strategy Research page lists the new rows with explicit labels.

## 5. Holdout governance

The previous guard (one row per run id) was **not sufficient**: a new run over the same bars could evaluate the holdout again. Replaced by a spec hash plus an insert-only consumption ledger keyed by instrument and bar range (ADR-0062). Verified with synthetic fixtures only (`tests/test_strategy_research_service.py`): second run refused, per-instrument independence, crash while computing stays consumed, persistence failure aborts and allows a safe retry, dependency drift refuses without consuming, ledger rows immutable.

## 6. Manual UI verification

**PARTIAL.** The app was started with `./scripts/start_app.sh` and the Strategy Research page was exercised in a browser (one Donchian/SPY run was created, which added one append-only record, run `0e4369c638794475`, to the owner's `data/research` database). The remaining pages (Opportunity Scanner, Paper Trading, Portfolio & Execution, Activity, Monitoring & Alerts, Setup & System Status) and the Compare-runs tab were **not** manually verified; they are covered only by the automated Streamlit tests. The disabled holdout button for earlier-engine runs is covered by an automated test (`test_holdout_button_is_disabled_for_a_run_from_an_earlier_engine_version`), not by a manual check.

## 7. Regression

Sandbox (Python 3.10, partial stubs; Streamlit and joblib tests skipped there): equity-timing 8/8, scorecard 31/31, research service 56/56, closeout script 3/3, portfolio backtest engine 41/41, paper runtime 99/99, architecture 62 passed 5 skipped, research architecture 9/9, app launch 28/28; `compileall` clean. **Authoritative run on the owner's machine (Python 3.14.6):** `ruff check src scripts tests` all checks passed; `pytest -q` **2151 passed, 0 failed** (0 skipped shown); `scripts/paper_runtime_validation.py --skip-external` PASS 26/26; `scripts/alert_delivery_validation.py` PASS 18/18. Final run on the owner's machine before the commit: `ruff check src scripts tests` all checks passed; `pytest -q` **2152 passed** (includes the new disabled-holdout-button test). CI (Python 3.12) on PR #2: `quality-gate` passed. Merged as `461b899`.

## 8. Files

Changed: `src/backtesting/portfolio_engine.py`, `src/strategy_research/{scorecard,service,store}.py`, `src/dashboard/page_strategy_research.py`, tests, `DECISIONS.md`, `CHANGELOG.md`. New: `src/strategy_research/governance.py`, `scripts/closeout_validation.py`, `tests/test_equity_timing.py`, `tests/test_closeout_validation_script.py`, this report.

## 9. Research interpretation (labelled engine v3 / methodology of ADR-0061 + ADR-0062)

Engine v3, default parameters, development + validation windows and rolling walk-forward only; **holdout never evaluated**. Earlier v1/v2 observations are superseded.

- **Net returns:** validation net return was positive in 20 of 24 combinations and negative in 4 (ema_cross IWM -4.91%, rsi_mean_reversion MSFT -1.32% and NVDA -2.06%, donchian_breakout IWM -3.85%). The validation window is one short stretch in which most of these instruments rose (full buy-and-hold positive in 20 of 24), so a positive strategy return mostly reflects being partly invested in a rising market.
- **Against buy-and-hold:** the strategy trailed *full* buy-and-hold in every combination where buy-and-hold was positive (often by a very wide margin: NVDA 246-288%). It beat it only where buy-and-hold was negative (IWM), which is the expected effect of being partly in cash. Against the *exposure-matched* comparator it was ahead in only 6 of 24 (ema_cross IWM and AMD; rsi_mean_reversion QQQ, MSFT, NVDA; donchian_breakout IWM), i.e. no evidence that entry/exit timing added value. Neither comparator is like-for-like with a risk-sized strategy that deploys only part of its capital, so this is a caution against over-reading the positives, not proof of the opposite.
- **Risk:** validation max drawdowns were small (about 3-10%) with Sharpe from -0.46 to 1.01; low drawdown here largely reflects partial exposure (time invested 28-72%).
- **Sample size:** no combination reaches the 30-closed-trade bar in the validation window: ema_cross 10-18 trades, donchian_breakout 11-14, rsi_mean_reversion 1-4 (VERY_SMALL). Every figure is anecdotal by the platform's own definition; rsi_mean_reversion results in particular carry almost no information.
- **Walk-forward:** chained out-of-sample returns were positive in all 24 (0.10% to 62%), but they span many years with a single fixed configuration, are not compared here against buy-and-hold over the same folds, and cover the same instruments chosen with hindsight. They do not establish an edge.
- **Biases that remain:** survivorship (today's large winners such as NVDA, AAPL, MSFT, AMD), 24 combinations examined together (some positives are expected by chance), one historical path, daily bars with simulated costs.
- **Conclusion:** none of the three strategies has demonstrated a tradable edge. The research ladder may label the 20 positive-validation runs RESEARCH_CANDIDATE; that is a label for "worth further out-of-sample testing", not evidence. Nothing here supports promotion or live use.

## 10. Engineering status / Research status / Remaining risks

- **Engineering status:** a real timing defect and a real holdout-reuse hole were found and fixed, with tests; two data-layer defects surfaced by real data were fixed. Merged via PR #2 (`461b899`) with 2152 tests and CI passing. Manual UI verification covered only the Strategy Research page.
- **Research status:** no strategy has demonstrated an edge (section 9). Real-data results under engine v3 exist for 24 combinations on development/validation data only. No holdout has been evaluated.
- **Remaining risks:** `main` protection lets the owner bypass; the spec's strict dependency pinning may refuse holdouts after an upgrade (by design); exposure-matched benchmark is an approximation at fill bars; first real-provider data glitches can still produce DATA_INVALID.
