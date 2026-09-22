"""AI/ML-based signal research -- src/ai/.

One capability among many, not the center of the project (this
module's own docstring hasn't changed its mind about that). Sprint 10
(`ROADMAP.md`, `DECISIONS.md` ADR-0043) implements Candidate B from the
Sprint 9 roadmap split: classic machine learning on engineered
features, reproducible and leakage-safe, feeding the existing
Strategy -> Signal -> Risk -> Execution pipeline as one more signal
source -- never a replacement for the strategy, risk, execution,
portfolio, or backtesting layers, and never LLM-based trading (that
stays explicit future work).

The chain, each stage its own module:

    src.data.MarketDataService.get_dataset()   (canonical candles -- not this package's job)
        -> src.ai.features.FeatureBuilder      (past-only numeric features)
        -> src.ai.labels.LabelBuilder          (3-class forward-return target)
        -> src.ai.dataset.build_training_table (aligned X/y, warmup/horizon rows dropped)
        -> src.ai.splitting                    (chronological split + purge/embargo, walk-forward)
        -> src.ai.model.AIModel                (fit/predict/predict_proba -- LogisticRegression today)
        -> src.ai.identity.model_spec_id        (deterministic model identity)
        -> src.ai.artifacts.ModelArtifactStore  (joblib persistence + content hash)
        -> src.ai.registry.ModelRegistry        (metadata + artifact lookup by model_id)
        -> src.ai.training.MLTrainingService    (wires all of the above together)
        -> src.strategies.ai_signal.AISignalStrategy (Signal, via the existing Strategy SDK)

This package has no idea orders, brokers, portfolios, or risk limits
exist -- an architecture test (`tests/test_architecture.py`) enforces
that no file here imports `src.broker`, `src.execution`, `src.risk`,
`src.portfolio`, or `src.dashboard`, and that nothing here ever
imports `yfinance`/`src.data.yfinance_provider` directly (every candle
this package sees arrives already validated and canonicalized via
`src.data.models.CandleDataset`).
"""
