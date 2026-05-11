"""Unit tests for ``gimmes.store.ticker_resolver`` (#582)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    insert_candidate,
    insert_trade,
    upsert_position,
)
from gimmes.store.ticker_resolver import resolve_ticker


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


def _pos(ticker: str, *, count: int = 100) -> Position:
    return Position(
        ticker=ticker, side="yes", count=count, avg_price=0.50,
        market_price=0.50, cost_basis=50.0,
    )


def _trade(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        action=TradeDecision.Action.OPEN,
        side="yes",
        count=10,
        price=0.5,
        model_probability=0.6,
        gimme_score=70.0,
        edge=0.1,
        kelly_fraction=0.02,
        rationale="test",
        thesis="test thesis",
        agent="test",
        order_id="o1",
    )


class TestOpenPositionsSource:
    async def test_unique_prefix_resolves(self, db: Database) -> None:
        await upsert_position(db, _pos("KXJOBLESSCLAIMS-26MAY14-210000"))
        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        matches = await resolve_ticker(
            db, "KXJOB", source="open_positions",
        )
        assert matches == ["KXJOBLESSCLAIMS-26MAY14-210000"]

    async def test_ambiguous_prefix_returns_sorted_list(self, db: Database) -> None:
        for t in (
            "KXCPI-26APR-T0.5",
            "KXCPI-26MAY-T0.6",
            "KXCPIYOY-26APR-T3.7",
        ):
            await upsert_position(db, _pos(t))
        matches = await resolve_ticker(db, "KXCPI", source="open_positions")
        assert matches == [
            "KXCPI-26APR-T0.5",
            "KXCPI-26MAY-T0.6",
            "KXCPIYOY-26APR-T3.7",
        ]

    async def test_no_match_returns_empty(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        assert await resolve_ticker(
            db, "ZZZ", source="open_positions",
        ) == []

    async def test_wrong_suffix_misread_returns_empty(self, db: Database) -> None:
        # Pin current behavior: a misread suffix (agent guesses ``-21-250``
        # from a wrapped ``-210000``) must NOT silently resolve via prefix
        # LIKE. If a fuzzy-match fallback is added later, this test should
        # fail and force a deliberate decision. See #581 / #587.
        await upsert_position(db, _pos("KXJOBLESSCLAIMS-26MAY14-210000"))
        assert await resolve_ticker(
            db, "KXJOBLESSCLAIMS-26MAY14-21-250", source="open_positions",
        ) == []

    async def test_excludes_closed_positions(self, db: Database) -> None:
        # Closed position (count=0) must not match.
        await upsert_position(db, _pos("KXCLOSED-26APR-T0.5", count=0))
        await upsert_position(db, _pos("KXOPEN-26APR-T0.5", count=50))
        matches = await resolve_ticker(db, "KX", source="open_positions")
        assert matches == ["KXOPEN-26APR-T0.5"]

    async def test_exact_match_shortcut_when_prefix_of_others(
        self, db: Database,
    ) -> None:
        # ``KXCPI`` is itself a position AND a prefix of others.
        # Exact-match shortcut must collapse to just ``KXCPI``.
        await upsert_position(db, _pos("KXCPI"))
        await upsert_position(db, _pos("KXCPICORE-26APR-T0.3"))
        await upsert_position(db, _pos("KXCPIYOY-26APR-T3.7"))
        matches = await resolve_ticker(db, "KXCPI", source="open_positions")
        assert matches == ["KXCPI"]

    async def test_empty_prefix_raises(self, db: Database) -> None:
        with pytest.raises(ValueError):
            await resolve_ticker(db, "", source="open_positions")
        with pytest.raises(ValueError):
            await resolve_ticker(db, "   ", source="open_positions")

    async def test_lowercase_prefix_resolves_to_uppercase_ticker(
        self, db: Database,
    ) -> None:
        # Kalshi tickers are canonical uppercase. The resolver
        # uppercases input before matching so a lowercase paste from
        # a non-canonical source still finds the right ticker.
        await upsert_position(db, _pos("KXJOBLESSCLAIMS-26MAY14-210000"))
        matches = await resolve_ticker(
            db, "kxjob", source="open_positions",
        )
        assert matches == ["KXJOBLESSCLAIMS-26MAY14-210000"]

    async def test_lowercase_exact_match_takes_shortcut(
        self, db: Database,
    ) -> None:
        # Exact-match shortcut compares the uppercased cleaned input
        # against the (canonical-uppercase) match set, so a lowercase
        # full ticker still collapses to a single match.
        await upsert_position(db, _pos("KXCPI"))
        await upsert_position(db, _pos("KXCPICORE-26APR-T0.3"))
        matches = await resolve_ticker(db, "kxcpi", source="open_positions")
        assert matches == ["KXCPI"]

    async def test_wildcard_in_prefix_raises(self, db: Database) -> None:
        # ``%`` and ``_`` are SQL LIKE wildcards; reject them so the
        # resolver doesn't silently match unintended rows.
        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        with pytest.raises(ValueError):
            await resolve_ticker(db, "KX%", source="open_positions")
        with pytest.raises(ValueError):
            await resolve_ticker(db, "KX_FOO", source="open_positions")
        # Any other non-charset character (e.g. quote, space-inside)
        # also rejected.
        with pytest.raises(ValueError):
            await resolve_ticker(db, "KX'OR1=1", source="open_positions")


class TestKnownMarketsSource:
    async def test_unions_positions_candidates_trades(
        self, db: Database,
    ) -> None:
        # One ticker per source.
        await upsert_position(db, _pos("KXFROMPOS-26APR-T0.5"))
        await insert_candidate(
            db, "KXFROMCAND-26APR-T0.5",
            "Candidate", 0.50, 0.80, 0.10, 70, "memo",
        )
        await insert_trade(db, _trade("KXFROMTRADE-26APR-T0.5"))
        matches = await resolve_ticker(db, "KXFROM", source="known_markets")
        assert matches == [
            "KXFROMCAND-26APR-T0.5",
            "KXFROMPOS-26APR-T0.5",
            "KXFROMTRADE-26APR-T0.5",
        ]

    async def test_deduplicates_across_tables(self, db: Database) -> None:
        # Same ticker in positions AND trades AND candidates — appears once.
        ticker = "KXDUP-26APR-T0.5"
        await upsert_position(db, _pos(ticker))
        await insert_candidate(
            db, ticker, "X", 0.5, 0.8, 0.1, 70, "memo",
        )
        await insert_trade(db, _trade(ticker))
        matches = await resolve_ticker(db, "KXDUP", source="known_markets")
        assert matches == [ticker]

    async def test_no_match_returns_empty(self, db: Database) -> None:
        await upsert_position(db, _pos("KXSOMETHING-26APR-T0.5"))
        assert await resolve_ticker(
            db, "KXOTHER", source="known_markets",
        ) == []

    async def test_excludes_closed_positions_but_includes_closed_trades(
        self, db: Database,
    ) -> None:
        # A closed position (count=0) shouldn't surface, but any past
        # trade on it should still surface via the trades UNION arm —
        # ``market-info`` should still be resolvable for a market the
        # user previously traded but no longer holds.
        await upsert_position(db, _pos("KXCLOSED-26APR-T0.5", count=0))
        await insert_trade(db, _trade("KXCLOSED-26APR-T0.5"))
        matches = await resolve_ticker(
            db, "KXCLOSED", source="known_markets",
        )
        assert matches == ["KXCLOSED-26APR-T0.5"]

    async def test_exact_match_shortcut(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI"))
        await upsert_position(db, _pos("KXCPICORE-26APR-T0.3"))
        matches = await resolve_ticker(db, "KXCPI", source="known_markets")
        assert matches == ["KXCPI"]
