"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gimmes.config import GimmesConfig, Mode, RiskConfig, StrategyConfig
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_ambient_cycle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """#768: the in-cycle order gates key on GIMMES_CYCLE. A pytest run
    from an in-cycle shell (the loop exports it) would mass-fail every
    order test that doesn't pass --agent closer — isolate the suite."""
    monkeypatch.delenv("GIMMES_CYCLE", raising=False)
    monkeypatch.delenv("GIMMES_SESSION_ID", raising=False)


@pytest.fixture
def config() -> GimmesConfig:
    """Default test config (driving range, side=yes pinned for test stability)."""
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(side="yes"),
        risk=RiskConfig(bankroll_paper=500.0, bankroll_real=500.0),
    )


@pytest.fixture
def sample_market() -> Market:
    """A sample market for testing."""
    return Market(
        ticker="KXTEST-26MAR-T50",
        event_ticker="KXTEST-26MAR",
        title="Test Market: Will X happen?",
        status=MarketStatus.ACTIVE,
        yes_bid=0.68,
        yes_ask=0.72,
        last_price=0.70,
        volume=5000,
        volume_24h=1200,
        open_interest=800,
        rules_primary="This market resolves YES if X happens before March 31, 2026.",
    )


@pytest.fixture
def sample_orderbook() -> Orderbook:
    """A sample orderbook for testing."""
    return Orderbook(
        ticker="KXTEST-26MAR-T50",
        yes_bids=[
            OrderbookLevel(price=0.68, quantity=200),
            OrderbookLevel(price=0.67, quantity=150),
            OrderbookLevel(price=0.65, quantity=300),
        ],
        no_bids=[
            OrderbookLevel(price=0.30, quantity=180),
            OrderbookLevel(price=0.29, quantity=250),
        ],
    )


@pytest.fixture
def markets_fixture() -> list[dict]:  # type: ignore[type-arg]
    """Load markets fixture data."""
    path = FIXTURES_DIR / "markets.json"
    if path.exists():
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    return []
