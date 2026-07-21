"""ai-trading-platform source package.

Organized by capability, not by strategy:

    src/data        - market data acquisition (the foundation)
    src/indicators  - technical indicators computed from data
    src/strategies  - trading logic that consumes data + indicators
    src/broker      - broker/exchange connectivity
    src/execution   - order routing and execution
    src/risk        - position sizing, risk limits, exposure checks
    src/analytics   - performance measurement, reporting
    src/ai          - ML/LLM-based components (one capability among many)
    src/dashboard   - Streamlit UI
    src/utils       - shared helpers (logging, config, etc.)
"""
