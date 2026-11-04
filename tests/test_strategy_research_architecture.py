"""Dependency and safety boundaries for strategy research (Sprint 23, ADR-0061). Checks real import statements."""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
RESEARCH = sorted((SRC / "strategy_research").glob("*.py"))
IMPORT = re.compile(r"^\s*(?:from|import)\s+(src\.[\w.]+)", re.MULTILINE)


def imports(path: pathlib.Path) -> set[str]:
    return set(IMPORT.findall(path.read_text()))


def test_research_never_imports_trading_promotion_scanning_agents_or_the_ui():
    forbidden = ("src.broker", "src.execution", "src.promotion", "src.dashboard", "src.scanner", "src.ai", "src.appsvc",
                 "src.monitoring", "src.reconciliation", "src.data.yfinance_provider", "src.paper.store", "src.paper.runtime",
                 "src.paper.service")
    offenders = {p.name: sorted(i for i in imports(p) if i.startswith(forbidden)) for p in RESEARCH}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_research_only_borrows_the_closed_bar_helper_from_the_paper_package():
    paper = {i for p in RESEARCH for i in imports(p) if i.startswith("src.paper")}
    assert paper <= {"src.paper.bars"}


def test_research_source_has_no_order_intent_proposal_or_promotion_vocabulary():
    text = "\n".join(re.sub(r'""".*?"""', "", p.read_text(), flags=re.S) for p in RESEARCH)
    for word in ("submit_order", "PaperBroker", "to_trade_intent", "ApprovedTradeIntent", "register_strategy", "promote(",
                 "create_proposal", "approve_proposal"):
        assert word not in text, word


def test_only_the_application_layer_the_cli_and_the_ai_read_tools_use_the_research_package():
    allowed = {SRC / "appsvc" / "strategy_research.py", SRC / "ai" / "agents" / "strategy_research_tools.py",
               SRC / "ai" / "agents" / "tools.py", SRC / "cli" / "research_agent.py"}
    users = set()
    for path in SRC.rglob("*.py"):
        if "strategy_research" in path.parts and path.parent.name == "strategy_research":
            continue
        if any(i.startswith("src.strategy_research") for i in imports(path)):
            users.add(path)
    assert users <= allowed, sorted(str(u.relative_to(SRC)) for u in users - allowed)


def test_the_page_talks_only_to_the_application_layer():
    page = SRC / "dashboard" / "page_strategy_research.py"
    bad = [i for i in imports(page) if not i.startswith(("src.appsvc", "src.dashboard"))]
    assert bad == []


def test_the_ai_tools_have_no_dependency_on_writers_or_trading_packages():
    tool_file = SRC / "ai" / "agents" / "strategy_research_tools.py"
    assert not [i for i in imports(tool_file) if i.startswith(("src.broker", "src.execution", "src.risk", "src.portfolio",
                                                               "src.dashboard", "src.promotion", "src.appsvc"))]


def test_the_strategy_catalogue_depends_on_no_market_data_or_trading_code():
    bad = [i for i in imports(SRC / "strategies" / "catalogue.py")
           if i.startswith(("src.data.service", "src.data.yfinance_provider", "src.broker", "src.execution", "src.paper"))]
    assert bad == []


def test_donchian_uses_the_shared_indicator_engine_and_no_provider():
    text = (SRC / "strategies" / "donchian_breakout.py").read_text()
    assert 'self.indicator(data, "DONCHIAN_UPPER"' in text and "yfinance" not in text and "MarketDataService" not in text


def test_the_backtester_and_execution_model_were_not_forked_for_research():
    # research reuses Backtester.run_portfolio; it neither imports a second engine nor defines its own fill logic
    text = (SRC / "strategy_research" / "service.py").read_text()
    assert "run_portfolio" in text and "class PortfolioBacktestEngine" not in text and "def simulate" not in text
