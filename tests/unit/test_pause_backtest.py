"""Tests for gimmes.reporting.pause_backtest (#556 / Phase 1a)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.reporting.pause_backtest import (
    GAP_BUCKETS,
    BacktestSummary,
    GapBucket,
    HourBucket,
    TradeBacktest,
    bucketize_gaps,
    build_summary,
    collect_trades,
    render_markdown,
)


def _seed_db(db: Path) -> None:
    """Create the candidates + trades schema and seed minimal rows."""
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            market_price REAL NOT NULL DEFAULT 0,
            model_probability REAL NOT NULL DEFAULT 0,
            edge REAL NOT NULL DEFAULT 0,
            gimme_score REAL NOT NULL DEFAULT 0,
            research_memo TEXT NOT NULL DEFAULT '',
            scanned_at TEXT NOT NULL,
            edge_size_score REAL NOT NULL DEFAULT 0,
            signal_strength_score REAL NOT NULL DEFAULT 0,
            liquidity_depth_score REAL NOT NULL DEFAULT 0,
            settlement_clarity_score REAL NOT NULL DEFAULT 0,
            time_to_resolution_score REAL NOT NULL DEFAULT 0,
            cap_blocked INTEGER NOT NULL DEFAULT 0,
            recommendation TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'yes',
            count INTEGER NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()


def _add_candidate(
    db: Path, ticker: str, scanned_at: str, *, gimme_score: float = 70.0,
    edge: float = 0.05, cap_blocked: int = 0,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO candidates (ticker, scanned_at, gimme_score, edge, "
        "cap_blocked) VALUES (?, ?, ?, ?, ?)",
        (ticker, scanned_at, gimme_score, edge, cap_blocked),
    )
    conn.commit()
    conn.close()


def _add_trade(
    db: Path, ticker: str, action: str, timestamp: str, *,
    side: str = "yes", agent: str = "closer",
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trades (ticker, action, side, timestamp, agent) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, action, side, timestamp, agent),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# collect_trades + first-seen lookup
# ---------------------------------------------------------------------------


class TestCollectTrades:
    def test_first_seen_picks_min_scanned_at(self, tmp_path: Path) -> None:
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        # Two candidate sightings of the same ticker — earlier wins.
        _add_candidate(
            db, "KX1", "2026-05-07 00:00:00",
            gimme_score=80.0, edge=0.10,
        )
        _add_candidate(
            db, "KX1", "2026-05-07 00:05:00",
            gimme_score=85.0, edge=0.12,
        )
        _add_trade(db, "KX1", "open", "2026-05-07T00:10:00+00:00")

        trades, warnings = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert warnings == []
        assert len(trades) == 1
        t = trades[0]
        # Earlier candidate, gap should be 10 minutes = 600s.
        assert t.first_seen_time == datetime(2026, 5, 7, 0, 0, tzinfo=UTC)
        assert t.gap_seconds == 600.0
        assert t.gimme_score == 80.0
        assert t.edge == 0.10

    def test_trade_with_no_candidate_records_none(self, tmp_path: Path) -> None:
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        _add_trade(db, "KX_GHOST", "open", "2026-05-07T00:10:00+00:00")

        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert len(trades) == 1
        assert trades[0].first_seen_time is None
        assert trades[0].gap_seconds is None
        assert trades[0].gimme_score is None

    def test_close_action_excluded_by_default(self, tmp_path: Path) -> None:
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        _add_candidate(db, "KX1", "2026-05-07 00:00:00")
        _add_trade(db, "KX1", "open", "2026-05-07T00:10:00+00:00")
        _add_trade(db, "KX1", "close", "2026-05-07T01:00:00+00:00")

        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        # Default actions=("open",); close excluded.
        assert len(trades) == 1
        assert trades[0].action == "open"

        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
            actions=("open", "close"),
        )
        assert {t.action for t in trades} == {"open", "close"}

    def test_skip_action_always_excluded(self, tmp_path: Path) -> None:
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        _add_trade(db, "KX1", "skip", "2026-05-07T00:10:00+00:00")

        # Even when actions explicitly includes "skip" we *would* return it,
        # but the default ("open",) must not.
        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert trades == []

    def test_missing_db_returns_warning(self, tmp_path: Path) -> None:
        trades, warnings = collect_trades(
            tmp_path / "missing.db",
            date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert trades == []
        assert any("DB not found" in w for w in warnings)

    def test_first_seen_excludes_candidates_after_trade(
        self, tmp_path: Path,
    ) -> None:
        """If the only candidate row for a ticker was scanned AFTER the
        trade was placed, ``first_seen_time`` must be ``None``. Pins the
        SQL contract that catches the ``T`` vs space separator bug
        — without `datetime(scanned_at) <= datetime(?)` SQLite would lex-
        compare and incorrectly include same-day post-trade sightings.
        """
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        # Trade at 14:00, candidate at 14:30 (later). The candidate must
        # NOT count as a first-sighting for this trade.
        _add_candidate(db, "KX1", "2026-05-07 14:30:00")
        _add_trade(db, "KX1", "open", "2026-05-07T14:00:00+00:00")

        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert len(trades) == 1
        assert trades[0].first_seen_time is None
        assert trades[0].gap_seconds is None

    def test_first_seen_picks_the_pre_trade_row_when_post_trade_also_exists(
        self, tmp_path: Path,
    ) -> None:
        """Two candidate rows: one before the trade, one after. The query
        must select only the pre-trade row."""
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        _add_candidate(db, "KX1", "2026-05-07 13:50:00", gimme_score=70.0)
        _add_candidate(db, "KX1", "2026-05-07 14:30:00", gimme_score=99.0)
        _add_trade(db, "KX1", "open", "2026-05-07T14:00:00+00:00")

        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        # Pre-trade sighting at 13:50 → 10 minutes before 14:00.
        assert trades[0].first_seen_time == datetime(
            2026, 5, 7, 13, 50, tzinfo=UTC,
        )
        assert trades[0].gap_seconds == 600.0
        assert trades[0].gimme_score == 70.0  # not the post-trade 99

    def test_cap_blocked_flagged(self, tmp_path: Path) -> None:
        db = tmp_path / "gimmes.db"
        _seed_db(db)
        _add_candidate(
            db, "KX1", "2026-05-07 00:00:00", cap_blocked=1,
        )
        _add_trade(db, "KX1", "open", "2026-05-07T00:10:00+00:00")
        trades, _ = collect_trades(
            db, date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert trades[0].cap_blocked_at_first_seen is True


# ---------------------------------------------------------------------------
# bucketize_gaps
# ---------------------------------------------------------------------------


class TestBucketizeGaps:
    @pytest.mark.parametrize(
        "gap,expected_label",
        [
            (0.0, "0-60s"),
            (59.999, "0-60s"),
            (60.0, "60-300s"),
            (299.999, "60-300s"),
            (300.0, "5-10min"),
            (599.999, "5-10min"),
            (600.0, "10-30min"),
            (1799.999, "10-30min"),
            (1800.0, "30min+"),
            (5000.0, "30min+"),
        ],
    )
    def test_boundaries(
        self, gap: float, expected_label: str, tmp_path: Path,
    ) -> None:
        t = TradeBacktest(
            trade_id=1, ticker="KX1", action="open", side="yes",
            trade_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            first_seen_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            gap_seconds=gap, hour_of_window_edt=20,
            trade_window_name="outside", gimme_score=70.0, edge=0.05,
            cap_blocked_at_first_seen=False,
        )
        buckets = bucketize_gaps([t])
        # Find the bucket with count==1.
        hit = next(b for b in buckets if b.count == 1)
        assert hit.label == expected_label

    def test_none_gaps_excluded_from_pct_denominator(
        self, tmp_path: Path,
    ) -> None:
        t_known = TradeBacktest(
            trade_id=1, ticker="KX1", action="open", side="yes",
            trade_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            first_seen_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            gap_seconds=120.0, hour_of_window_edt=20,
            trade_window_name="outside", gimme_score=None, edge=None,
            cap_blocked_at_first_seen=False,
        )
        t_unknown = TradeBacktest(
            trade_id=2, ticker="KX2", action="open", side="yes",
            trade_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            first_seen_time=None, gap_seconds=None,
            hour_of_window_edt=20, trade_window_name="outside",
            gimme_score=None, edge=None,
            cap_blocked_at_first_seen=False,
        )
        buckets = bucketize_gaps([t_known, t_unknown])
        # Total denominator = 1 (only known-gap trade), so the bucket the
        # known trade lands in must be 100%.
        hit = next(b for b in buckets if b.count == 1)
        assert hit.pct_of_total == 100.0

    def test_empty_input_yields_zero_buckets(self) -> None:
        buckets = bucketize_gaps([])
        assert all(b.count == 0 and b.pct_of_total == 0.0 for b in buckets)
        # Bucket count matches the GAP_BUCKETS module constant.
        assert len(buckets) == len(GAP_BUCKETS)


# ---------------------------------------------------------------------------
# render_markdown determinism + recommendation logic
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def _summary(
        self, *, fast_pct: float = 0.0, total: int = 1,
    ) -> BacktestSummary:
        # Construct a synthetic gap-bucket distribution where ``fast_pct``
        # of trades land in 60-300s; rest in 30min+.
        fast_count = int(round(total * fast_pct / 100))
        slow_count = total - fast_count
        gap_buckets = [
            GapBucket(
                label=label, lower_seconds=lower, upper_seconds=upper,
                count=(
                    fast_count if label == "60-300s"
                    else slow_count if label == "30min+"
                    else 0
                ),
                pct_of_total=(
                    fast_pct if label == "60-300s"
                    else (100.0 - fast_pct) if label == "30min+"
                    else 0.0
                ),
            )
            for label, lower, upper in GAP_BUCKETS
        ]
        return BacktestSummary(
            trades=[],
            hour_buckets=[
                HourBucket(
                    hour_edt=10, cycles_observed=5, days_observed=3,
                    trades_placed=2, trades_per_cycle=0.4,
                ),
            ],
            gap_buckets=gap_buckets,
            by_window_name={"jobless_claims": total},
            date_from=date(2026, 4, 20),
            date_to=date(2026, 5, 7),
            cycles_audited=100,
            trades_with_no_candidate=0,
        )

    def test_recommendation_do_not_raise_at_30pct(self) -> None:
        md = render_markdown(self._summary(fast_pct=35.0, total=100))
        assert "DO NOT RAISE" in md

    def test_recommendation_likely_safe_at_5pct(self) -> None:
        md = render_markdown(self._summary(fast_pct=5.0, total=100))
        assert "LIKELY SAFE TO RAISE" in md

    def test_recommendation_wait_in_borderline(self) -> None:
        md = render_markdown(self._summary(fast_pct=12.0, total=100))
        assert "WAIT FOR #553" in md

    def test_recommendation_inconclusive_with_zero_trades(self) -> None:
        md = render_markdown(self._summary(fast_pct=0.0, total=0))
        assert "INCONCLUSIVE" in md

    def test_render_is_deterministic(self) -> None:
        s = self._summary(fast_pct=12.0, total=10)
        a = render_markdown(s)
        b = render_markdown(s)
        assert a == b

    def test_render_includes_all_required_sections(self) -> None:
        md = render_markdown(self._summary(fast_pct=12.0, total=10))
        assert "## Executive summary" in md
        assert "## Methodology" in md
        assert "## Hour-of-window" in md
        assert "## Gap distribution" in md
        assert "## Per-trade detail" in md
        assert "## By trade window" in md
        assert "## Caveats" in md
        assert "## Recommendation" in md
        assert "#553" in md


# ---------------------------------------------------------------------------
# build_summary end-to-end
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_end_to_end_with_one_cycle_and_one_trade(
        self, tmp_path: Path,
    ) -> None:
        gimmes_home = tmp_path / "home"
        log_dir = gimmes_home / "logs"
        log_dir.mkdir(parents=True)
        db = gimmes_home / "gimmes.db"
        _seed_db(db)
        _add_candidate(db, "KX1", "2026-05-07 14:00:00")
        _add_trade(db, "KX1", "open", "2026-05-07T14:05:00+00:00")
        # Minimal cycle log so aggregate_hours has something to bucket.
        cycle_log = log_dir / "cycle-001.json"
        cycle_log.write_text(json.dumps([
            {"type": "user", "timestamp": "2026-05-07T14:00:00.000Z"},
            {"type": "assistant", "message": {"content": [{
                "type": "text",
                "text": "Scout returned 3 candidates. PROCEED",
            }]}},
            {"type": "user", "timestamp": "2026-05-07T14:30:00.000Z"},
            {"type": "result", "is_error": False},
        ]))

        s = build_summary(
            log_dir=log_dir, db_path=db,
            date_from=date(2026, 5, 7), date_to=date(2026, 5, 7),
        )
        assert len(s.trades) == 1
        assert s.cycles_audited == 1
        assert s.trades[0].gap_seconds == 300.0  # 5 minutes
        # The 60-300s bucket boundary is exclusive at 300s, so 300.0 lands
        # in 5-10min. Sanity check.
        five_min = next(b for b in s.gap_buckets if b.label == "5-10min")
        assert five_min.count == 1


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCli:
    def test_pause_backtest_writes_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gimmes_home = tmp_path / "home"
        (gimmes_home / "logs").mkdir(parents=True)
        db = gimmes_home / "gimmes.db"
        _seed_db(db)
        _add_candidate(db, "KX1", "2026-05-07 14:00:00")
        _add_trade(db, "KX1", "open", "2026-05-07T14:05:00+00:00")
        # Cycle log so audit_hours has data.
        (gimmes_home / "logs" / "cycle-001.json").write_text(json.dumps([
            {"type": "user", "timestamp": "2026-05-07T14:00:00.000Z"},
            {"type": "result", "is_error": False},
        ]))

        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)

        out_path = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "pause-backtest",
                "--from", "2026-05-07",
                "--to", "2026-05-07",
                "--output", str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out_path.exists()
        content = out_path.read_text()
        assert "Phase 1a Pause Backtest" in content
        assert "KX1" in content

    def test_pause_backtest_json_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gimmes_home = tmp_path / "home"
        (gimmes_home / "logs").mkdir(parents=True)
        db = gimmes_home / "gimmes.db"
        _seed_db(db)
        _add_trade(db, "KX1", "open", "2026-05-07T14:05:00+00:00")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "pause-backtest",
                "--from", "2026-05-07",
                "--to", "2026-05-07",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        # Strip Rich's prepended whitespace if any, then parse.
        parsed = json.loads(result.output.strip())
        assert "trades" in parsed
        assert "hour_buckets" in parsed
        assert "gap_buckets" in parsed
        assert parsed["date_from"] == "2026-05-07"

    def test_invalid_from_date_exits_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gimmes_home = tmp_path / "home"
        (gimmes_home / "logs").mkdir(parents=True)
        db = gimmes_home / "gimmes.db"
        _seed_db(db)
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["pause-backtest", "--from", "not-a-date"],
        )
        assert result.exit_code == 1

    def test_missing_db_exits_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gimmes_home = tmp_path / "home"
        (gimmes_home / "logs").mkdir(parents=True)
        # No gimmes.db file.
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)
        runner = CliRunner()
        result = runner.invoke(
            app, ["pause-backtest"],
        )
        assert result.exit_code == 1
        assert "No database" in result.output
