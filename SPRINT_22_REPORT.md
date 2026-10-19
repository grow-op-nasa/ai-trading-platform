# Sprint 22 report: reliability, reproducible research and continuous integration

Date: 2026-10-19. Branch: `main`. Commits: `d4e01bd`, `b1cd396`, `14dec9d` (ADR-0059).

## 1. Sprint outcome

All three objectives are implemented, pushed, and CI is green on `main`.

- **CI gate.** A GitHub Actions workflow runs tests, byte-compilation, Ruff and two self-contained validations on every push and pull request to `main`. It needs no secrets and contacts no live provider, broker, SMTP server or AI service.
- **Declared history requirements.** Strategies now declare, in a typed and validated object, how many complete bars they need. The scanner's parameter-name heuristic is deleted. An undeclared or invalid strategy cannot be scanned, run in the paper runtime, or promoted.
- **Reproducible evaluations.** Scans and paper cycles store the exact normalised market data in a content-addressed snapshot. A scan can be replayed offline in three layers (data, strategy, risk), with an honest `NOT_REPLAYABLE` outcome when data or compatible code is missing.

Two parts are still unconfirmed: a real-data scan followed by `scripts/replay_scan.py`, and a manual browser walkthrough of the Reproducibility panel (see section 7).

## 2. CI

- `.github/workflows/ci.yml`, job `quality-gate`, Python 3.12, pip cache, read-only permissions, no `continue-on-error`.
- Steps, in order: install pinned dependencies (`requirements-dev.txt`, Ruff 0.9.10), `pytest -q`, `compileall`, `ruff check src scripts tests`, `paper_runtime_validation.py --skip-external`, `alert_delivery_validation.py`.
- Ruff runs the default rules (`E4,E7,E9,F`) with no ignores, no excludes and no blanket suppressions.
- `scheduler_validation.py` and other scripts that can reach live services are deliberately not run in CI.
- `tests/test_ci_workflow.py` pins the workflow's shape.
- **Observed results on GitHub.**
  - CI #1 on `d4e01bd`: **failed**, as expected, because Ruff reported 197 errors on that commit.
  - CI #2 on `14dec9d`: **passed** (3m 4s). I confirmed the run status on the Actions page; I did not open the per-step logs.
- **Branch protection.** A classic rule for `main` was created in GitHub settings. Its details panel kept loading, so I could not confirm that `quality-gate` is listed as a required check. Check this in Settings, Branches, Edit.

## 3. Strategy history contract

- `src/strategies/history.py` defines `HistoryRequirement`, a parameter-aware declaration returned by each strategy's `history_requirement()`. `EMACrossStrategy(fast=8, slow=21)` and `(fast=50, slow=200)` declare different requirements.
- **Meaning.** `min_bars` is the minimum number of consecutive complete bars, ending at the decision bar, for the decision to count. Fewer bars gives `NOT_EVALUABLE` or `BLOCKED`, never `NO_SIGNAL`.
- **Derivations.** EMA bars are the count at which the first bar's residual weight is at most 1%. RSI uses the same rule plus one bar. ATR needs its period, a rolling window needs its window, an n-bar return needs n+1, and a composite strategy takes the maximum of its parts. The 1% tolerance is a documented policy choice, not a proven bound.
- **Where it applies.**
  - Scanner: each scan stores the requirement and each symbol stores its assessment. A requirement the lookback window can never hold is rejected at setup.
  - Paper runtime and preflight: an unmet or undeclared requirement blocks the cycle before any intent (`INSUFFICIENT_HISTORY`, `HISTORY_REQUIREMENT_UNKNOWN`).
  - Experiments: recorded as provenance only; a backtest is never refused for it.
  - Promotion and the candidate harness: a candidate without a valid declaration is refused.
- Lookup statuses are DECLARED, UNDECLARED and INVALID. Nothing is ever defaulted.

## 4. Snapshot architecture

- `src/snapshots/` stores gzip JSON at `data/snapshots/<aa>/<sha256>.json.gz`. The id is the SHA-256 of the canonical payload; creation time is not part of it, so identical data is stored once.
- The round trip is exact: integer-nanosecond timestamps plus the index unit, floats by Python repr, non-finite values as tokens.
- Every read verifies the bytes against the id, the format version, and the rebuilt frame's fingerprint. Writes are atomic (temp file, fsync, `os.link`). A damaged object is quarantined as `*.corrupt-<ts>`, never trusted.
- The scanner stores and read-back-verifies the snapshot before using the data. On failure the symbol becomes an honest `ERROR` / `SNAPSHOT_FAILED`. Paper cycles also store a snapshot and block before any intent if that fails.
- Each result records snapshot id, evaluation time, library versions, evaluated window and the private risk context.
- A snapshot of 275 synthetic bars measured about 11.7 KB compressed.
- Architecture boundaries are enforced by tests: only `src.appsvc` imports `src.scanner`, and `src/snapshots/meta.py` exists so `src.paper` never imports `src.scanner`.

## 5. Replay demonstration

- `scripts/replay_scan.py --scan-id ID` and `ScannerService.replay` report three layers per symbol.
  - **Data:** the snapshot is intact and reproduces the recorded dataset and evaluated bars.
  - **Strategy:** the registered strategy re-run on those bars. A changed strategy source hash is reported as `STRATEGY_CHANGED` and the symbol is `NOT_REPLAYABLE`.
  - **Risk:** the classification is re-derived from the captured context, not today's portfolio.
- Per-symbol outcomes are `REPRODUCED`, `DIFFERENT` and `NOT_REPLAYABLE`. Legacy scans are labelled `LEGACY_HEURISTIC` and never reported as reproduced.
- Sandbox tests show replay makes no provider call and leaves the scanner database, snapshot directory and registry byte-identical.
- In the app, each symbol has a Reproducibility panel and a "Replay this scan offline" button that never runs on its own.
- **Not yet demonstrated on real data.** The replay CLI was exercised in tests and by hand with synthetic data. A live-provider scan followed by replay has not been run.

## 6. Tests and regressions

- **Real-machine results (macOS, Python 3.14).**
  - First full run, at the first commit: `1953 passed`.
  - After the lint fixes: `1952 passed, 1 failed`. The failure was my own mistake, a removed `prefs` import that a test still used. It was fixed in `14dec9d`; `tests/test_dashboard_smoke.py` then passed (50 tests). I did not see a full local re-run after that last fix, but CI #2 ran the full suite and passed.
  - `ruff check src scripts tests`: all checks passed.
  - `paper_runtime_validation.py --skip-external`: 26 of 26 checks. `alert_delivery_validation.py`: 18 of 18 checks. Both were run locally on the first commit and again inside CI #2.
- New suites: `test_strategy_history`, `test_snapshot_store`, `test_scan_replay`, `test_paper_history`, `test_ci_workflow`. Extended: scanner, paper runtime, promotion, strategy-dev runner and app-launch tests.
- Sprint 21 tests tied to the old heuristic were migrated, not weakened or skipped. ADR-0059 section 9 lists each change.
- **Lint remediation.** Ruff found 197 errors on the first push. Most were in code from before this sprint (semicolon-joined statements, ambiguous `l` variables, unused imports and locals, two late imports). All were fixed in code with no suppressions or exclusions.

## 7. Safety and limitations

- Replay and snapshots create no orders, intents, cycles, proposals or fills, and do not mutate portfolio, registry or promotion state.
- The strategy hash covers the strategy class source only, not indicator code; a changed indicator shows up only as a `DIFFERENT` outcome.
- Replay proves the stored data gives the same answer. It cannot prove the provider's data was right.
- Snapshots are not garbage-collected or migrated across format versions.
- Snapshots are not yet taken for backtests and experiments (they record a fingerprint only), and there is no paper-cycle replay command.
- **Mistakes during this sprint, for the record.**
  - I reported "0 lint findings" from an approximate checker that did not cover the E7 rules. Real Ruff disproved it.
  - The first push went out before Ruff passed, because the commands were chained without a stop on failure. That is why CI #1 is red.
  - A zsh comment broke the first `rev-list` divergence check. The push was nevertheless a fast-forward and nothing upstream was overwritten.
  - I first named the wrong status check (`CI` instead of `quality-gate`).
- **Still to do.**
  1. Run one real-data scan from the app, then `python scripts/replay_scan.py --scan-id <id>` and confirm `REPRODUCED`.
  2. Walk through the Reproducibility panel and replay button in a real browser.
  3. Confirm `quality-gate` is the required check on the `main` rule.

## 8. Git release

- Pushed to `origin/main`: `aa80efe..d4e01bd` (Sprint 22), then `d4e01bd..14dec9d` (lint fixes and the `prefs` import fix, including `b1cd396`). No force-push, no history rewrite.
- `git rev-list --left-right --count HEAD...origin/main` before the second push: `2 0` (two commits ahead, none behind). Before the first push it errored because of the zsh comment, so it did not run.
- `git status --short` after the pushes printed nothing in the output I was given.
- This report file was written afterwards and is not yet committed.
