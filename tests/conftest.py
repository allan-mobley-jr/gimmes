"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gimmes.config import GimmesConfig, Mode
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def config() -> GimmesConfig:
    """Default test config (driving range, no real credentials)."""
    return GimmesConfig(mode=Mode.DRIVING_RANGE)


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
