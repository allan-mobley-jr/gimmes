"""Unit tests for pre-trade validator."""

from gimmes.config import GimmesConfig, Mode, ScannerConfig, StrategyConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.risk.validator import validate_trade


def _make_market(**kwargs) -> Market:  # type: ignore[no-untyped-def]
    defaults = {
        "ticker": "KXTEST",
        "status": MarketStatus.ACTIVE,
        "yes_bid": 0.68,
        "yes_ask": 0.72,
        "last_price": 0.70,
        "rules_primary": "This market resolves YES if X happens.",
    }
    defaults.update(kwargs)
    return Market(**defaults)


HOURLY_TICKER = "KXBTCD-26JUN23H14-T119999.99"


def _hourly_config() -> GimmesConfig:
    """KXBTCD is an hourly series, NO side — shared by the hourly
    floor (#722) and band (#750) test classes."""
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(side="no"),
        scanner=ScannerConfig(hourly_series=["KXBTCD"]),
    )


class TestValidateTrade:
    def test_all_checks_pass(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert len(result.failures) == 0

    def test_daily_loss_exceeded(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=-2000,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("Daily loss" in f for f in result.failures)

    def test_max_positions(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=15,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("max positions" in f.lower() for f in result.failures)

    def test_insufficient_edge(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.72,  # Only 2pp edge, below 5pp min
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("edge" in f.lower() for f in result.failures)

    def test_true_probability_at_boundary_passes(self, config: GimmesConfig) -> None:
        """Trade approved when true probability equals min_true_probability (>= not >)."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,  # Exactly at 0.90 default threshold
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("true probability ok" in c.lower() for c in result.checks)

    def test_true_probability_too_low(self, config: GimmesConfig) -> None:
        """Trade rejected when true probability is below min_true_probability."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.85,  # Below 0.90 default min
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("true probability" in f.lower() for f in result.failures)

    def test_duplicate_position(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
        )
        assert result.approved is False
        assert any("duplicate" in f.lower() or "already" in f.lower() for f in result.failures)

    def test_size_up_bypasses_duplicate_check(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is True
        assert any("size up" in c.lower() for c in result.checks)

    def test_size_up_still_enforces_other_checks(self, config: GimmesConfig) -> None:
        """SIZE UP bypasses duplicate check but other checks still apply."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=-2000,  # Exceeds 15% daily loss on 10k bankroll
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is False
        assert any("daily" in f.lower() or "loss" in f.lower() for f in result.failures)
        assert not any("duplicate" in f.lower() or "already" in f.lower() for f in result.failures)

    def test_size_up_rejects_when_no_existing_position(self, config: GimmesConfig) -> None:
        """SIZE UP must fail if there's no existing position to add to."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],  # No existing position
            config=config,
            size_up=True,
        )
        assert result.approved is False
        assert any("no existing position" in f.lower() for f in result.failures)

    def test_size_up_skips_position_count_check(self, config: GimmesConfig) -> None:
        """SIZE UP should not be blocked by position count at max."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=15,  # At max
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is True
        assert any("position count" in c.lower() and "skipped" in c.lower() for c in result.checks)

    def test_size_up_checks_aggregate_position_size(self, config: GimmesConfig) -> None:
        """SIZE UP should reject when aggregate exposure exceeds max_position_pct."""
        market = _make_market()
        # max_position_pct=0.05, bankroll=10000 → max $500
        # $300 new + $300 existing = $600 > $500
        result = validate_trade(
            market=market,
            trade_dollars=300,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
            existing_cost_basis=300,
        )
        assert result.approved is False
        assert any("exceeds max" in f.lower() for f in result.failures)

    def test_size_up_aggregate_within_limit_passes(self, config: GimmesConfig) -> None:
        """SIZE UP should pass when aggregate exposure is within max_position_pct."""
        market = _make_market()
        # $100 new + $300 existing = $400 < $500
        result = validate_trade(
            market=market,
            trade_dollars=100,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
            existing_cost_basis=300,
        )
        assert result.approved is True
        assert any("new" in c.lower() and "existing" in c.lower() for c in result.checks)

    def test_non_size_up_ignores_existing_cost_basis(self, config: GimmesConfig) -> None:
        """Non-SIZE UP trades should not consider existing_cost_basis."""
        market = _make_market()
        # $200 trade alone is fine (< $500), but $200 + $400 = $600 would fail
        # Since size_up=False, only $200 should be checked
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            size_up=False,
            existing_cost_basis=400,
        )
        assert result.approved is True
        # Should NOT mention "existing" in the size check
        size_checks = [c for c in result.checks if "position size" in c.lower()]
        assert all("existing" not in c.lower() for c in size_checks)

    def test_settlement_risk_high(self, config: GimmesConfig) -> None:
        market = _make_market(
            rules_primary="Kalshi reserves the right to cancel at sole discretion. "
                          "Death carveout applies. Subjective determination may apply."
        )
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("settlement" in f.lower() for f in result.failures)

    def test_none_probability_skips_true_prob_check(self, config: GimmesConfig) -> None:
        """When true_probability is None, the min probability check is skipped."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("true probability check skipped" in c.lower() for c in result.checks)

    def test_none_probability_skips_edge_check(self, config: GimmesConfig) -> None:
        """When true_probability is None, edge check is skipped."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("skipped" in c.lower() for c in result.checks)
        assert not any("edge" in f.lower() for f in result.failures)

    def test_none_probability_still_enforces_other_checks(
        self, config: GimmesConfig,
    ) -> None:
        """Even without probability, other checks still apply."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=-2000,  # Exceeds 15% daily loss
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("daily loss" in f.lower() for f in result.failures)

    def test_bankroll_exceeded(self, config: GimmesConfig) -> None:
        """Trade rejected when deployed_cost_basis + trade_dollars > bankroll."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            deployed_cost_basis=400,  # 400 + 200 = 600 > 500 bankroll
        )
        assert result.approved is False
        assert any("bankroll" in f.lower() for f in result.failures)

    def test_bankroll_within_limit(self, config: GimmesConfig) -> None:
        """Trade approved when deployed_cost_basis + trade_dollars <= bankroll."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            deployed_cost_basis=100,  # 100 + 200 = 300 < 500 bankroll
        )
        assert result.approved is True
        assert any("bankroll ok" in c.lower() for c in result.checks)


class TestPriceBoundGate:
    """#658: within one tick of a bound the edge formula collapses to
    prob - 0 — the validator must reject, not print 'Edge OK (88%)'
    for an unfillable order. Prices drift to the bound between Caddie
    research and Closer execution, so the live check is the backstop."""

    def test_no_side_floor_rejected(self, config: GimmesConfig) -> None:
        config.strategy.side = "no"
        market = _make_market(yes_bid=1.0, yes_ask=1.0, last_price=1.0)
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.88,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("Price at bound" in f for f in result.failures)
        assert not any("Edge OK" in c for c in result.checks)

    def test_yes_side_ceiling_rejected(self, config: GimmesConfig) -> None:
        config.strategy.side = "yes"
        market = _make_market(yes_bid=0.99, yes_ask=0.99, last_price=0.99)
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.999,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("Price at bound" in f for f in result.failures)

    def test_count_only_order_still_rejected_at_bound(
        self, config: GimmesConfig,
    ) -> None:
        """#672: the bound rejection is not probability-gated — a
        manual `order --count N` without --prob is just as unfillable
        at the bound (pre-fix the whole check was skipped)."""
        config.strategy.side = "no"
        market = _make_market(yes_bid=1.0, yes_ask=1.0, last_price=1.0)
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("Price at bound" in f for f in result.failures)

    def test_mid_range_edge_check_unchanged(
        self, config: GimmesConfig,
    ) -> None:
        market = _make_market()  # midpoint 0.70
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert not any("Price at bound" in f for f in result.failures)
        assert any("Edge" in c for c in result.checks)


class TestHourlyProbabilityFloor:
    """#722: hourly-series tickers gate on hourly_min_true_probability;
    the global floor and its message literals stay untouched."""

    def _validate(self, config: GimmesConfig, ticker: str, prob: float):
        return validate_trade(
            market=_make_market(ticker=ticker),
            trade_dollars=200,
            true_probability=prob,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )

    def test_hourly_floor_accepts_072(self) -> None:
        result = self._validate(_hourly_config(), HOURLY_TICKER, 0.72)
        assert not any("probability too low" in f.lower() for f in result.failures)
        assert any("hourly floor" in c for c in result.checks)

    def test_hourly_floor_boundary_at_070(self) -> None:
        # >= not >: exactly 0.70 passes. This also pins the floor==gate
        # composition (#722 review): apply_base_rate_floor promotes any
        # lower KXBTCD estimate to exactly 0.70, so check 5 is a
        # formality on auto-sized NO orders by design — check 6 (edge
        # after fees) and Caddie's sanity checks are the binding gates.
        result = self._validate(_hourly_config(), HOURLY_TICKER, 0.70)
        assert not any("probability too low" in f.lower() for f in result.failures)
        assert any("hourly floor" in c for c in result.checks)

    def test_hourly_floor_rejects_below(self) -> None:
        result = self._validate(_hourly_config(), HOURLY_TICKER, 0.65)
        assert result.approved is False
        assert any(
            "70% hourly minimum" in f for f in result.failures
        ), result.failures

    def test_global_floor_untouched_for_non_hourly(self) -> None:
        result = self._validate(_hourly_config(), "KXTEST", 0.72)
        assert result.approved is False
        failures = [f for f in result.failures if "probability too low" in f.lower()]
        assert failures == ["True probability too low: 72% < 90% minimum"]

    def test_inertness_message_byte_identical(self, config: GimmesConfig) -> None:
        # hourly_series empty: a KXBTCD ticker fails with the exact
        # pre-#722 literal — no "hourly" wording anywhere
        result = self._validate(config, HOURLY_TICKER, 0.85)
        failures = [f for f in result.failures if "probability too low" in f.lower()]
        assert failures == ["True probability too low: 85% < 90% minimum"]

    def test_event_exposure_binds_across_hourly_strikes(self) -> None:
        # Strikes of one hour share an event_ticker, so the 4c cap is the
        # effective per-hour position cap. The KXBTCD event shape
        # (KXBTCD-<date>H<hour> grouping all strikes) is asserted here
        # from Kalshi's series convention; live verification of the API
        # response shape is a #721 part D deliverable.
        config = _hourly_config()
        market = _make_market(
            ticker="KXBTCD-26JUN23H14-T118999.99",
            event_ticker="KXBTCD-26JUN23H14",
        )
        result = validate_trade(
            market=market,
            trade_dollars=600,
            true_probability=0.72,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXBTCD-26JUN23H14-T119999.99"],
            config=config,
            # Sibling strike already deployed in the same hourly event:
            # 1000 + 600 = 1600 > the 15% x 10k = 1500 cap
            event_exposure=1000.0,
        )
        assert result.approved is False
        assert any("event" in f.lower() for f in result.failures)


class TestHourlyPriceBand:
    """#750: hourly tickers gate on the side-relative market price band
    at validation time — review-level band checks can be minutes stale,
    and the #743 approval-price cap alone passes a collapsed NO price
    as "improvement". Non-hourly tickers see no band lines at all."""

    def _validate(self, config: GimmesConfig, ticker: str, *, yes_bid: float, yes_ask: float):
        return validate_trade(
            market=_make_market(
                ticker=ticker, yes_bid=yes_bid, yes_ask=yes_ask,
                last_price=round((yes_bid + yes_ask) / 2, 4),
            ),
            trade_dollars=200,
            true_probability=0.72,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )

    def test_in_band_passes(self) -> None:
        # NO effective mid 0.40 — inside 0.30-0.85
        result = self._validate(
            _hourly_config(), HOURLY_TICKER,
            yes_bid=0.58, yes_ask=0.62,
        )
        assert any("Hourly band OK" in c for c in result.checks)
        assert not any("outside band" in f for f in result.failures)

    def test_floor_boundary_passes(self) -> None:
        # NO effective mid exactly 0.30 — >= not >, the floor is in-band
        result = self._validate(
            _hourly_config(), HOURLY_TICKER,
            yes_bid=0.68, yes_ask=0.72,
        )
        assert any("Hourly band OK" in c for c in result.checks)

    def test_below_floor_rejected(self) -> None:
        # NO effective mid 0.21 — the c1989 loss shape (#750)
        result = self._validate(
            _hourly_config(), HOURLY_TICKER,
            yes_bid=0.77, yes_ask=0.81,
        )
        assert result.approved is False
        assert any(
            "Hourly price outside band" in f and "#750" in f
            for f in result.failures
        ), result.failures

    def test_above_ceiling_rejected(self) -> None:
        # NO effective mid 0.90 — above the 0.85 ceiling
        result = self._validate(
            _hourly_config(), HOURLY_TICKER,
            yes_bid=0.08, yes_ask=0.12,
        )
        assert result.approved is False
        assert any("Hourly price outside band" in f for f in result.failures)

    def test_non_hourly_sees_no_band_lines(self) -> None:
        # Same collapsed price on a non-hourly ticker: no band check,
        # no band failure — the band is scoped to the hourly lane.
        result = self._validate(
            _hourly_config(), "KXTEST",
            yes_bid=0.77, yes_ask=0.81,
        )
        assert not any("band" in c.lower() for c in result.checks)
        assert not any("band" in f.lower() for f in result.failures)

    def test_inert_when_hourly_series_empty(self, config: GimmesConfig) -> None:
        # Stock install: KXBTCD is not an hourly ticker, no band lines
        result = self._validate(
            config, HOURLY_TICKER,
            yes_bid=0.77, yes_ask=0.81,
        )
        assert not any("band" in c.lower() for c in result.checks)
        assert not any("band" in f.lower() for f in result.failures)


class TestMarketStatusCheck:
    """#784 check 0: validation never APPROVEs an untradeable market."""

    @staticmethod
    def _cfg() -> GimmesConfig:
        return GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="yes", min_true_probability=0.9),
        )

    def _validate_status(self, status):
        m = _make_market()
        m.status = status
        return validate_trade(
            m, 50.0, 0.95, 1000.0, 0.0, 0, [], self._cfg(),
        )

    def test_determined_fails(self) -> None:
        result = self._validate_status(MarketStatus.DETERMINED)
        assert not result.approved
        assert any("not active" in f for f in result.failures)

    def test_closed_fails(self) -> None:
        result = self._validate_status(MarketStatus.CLOSED)
        assert not result.approved

    def test_active_passes_with_check_line(self) -> None:
        result = self._validate_status(MarketStatus.ACTIVE)
        assert any("Market status OK" in c for c in result.checks)
