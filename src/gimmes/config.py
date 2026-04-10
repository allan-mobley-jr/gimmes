"""Two-layer configuration: secrets from env vars, strategy params from database."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("gimmes.config")

GIMMES_HOME = Path(os.getenv("GIMMES_HOME", str(Path.home() / ".gimmes"))).expanduser()

load_dotenv(dotenv_path=GIMMES_HOME / ".env")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Curated series watchlist organised by category.  Used as the Pydantic
# default for ScannerConfig.series and seeded into the database on first init.
SERIES_CATEGORIES: dict[str, list[str]] = {
    "Inflation & CPI": [
        "KXCPI", "KXCPICORE", "KXCPIYOY", "KXCPICOREYOY",
        "KXECONSTATCPI", "KXECONSTATCPICORE", "KXECONSTATCPIYOY",
        "KXECONSTATCORECPIYOY", "KXPCECORE",
    ],
    "GDP & Growth": [
        "KXGDP", "KXGDPNOM", "KXGDPUSMAX",
    ],
    "Fed & Rates": [
        "KXFED", "KXFEDDECISION", "KXFEDCOMBO", "KXRATECUTCOUNT",
        "KXFEDCHGCOUNT", "KXFEDMEET", "KXEMERCUTS", "KXFEDDISSENT",
    ],
    "Employment": [
        "KXJOBLESSCLAIMS", "KXUE", "KXU3", "KXPAYROLLS", "KXADP",
    ],
    "Housing & Mortgage": [
        "KXMORTGAGERATE", "KXHOUSINGSTART", "KXEHSALES", "KXNHSALES",
    ],
    "Other Econ": [
        "KXISMPMI", "KXRECSSNBER", "KXEFFTARIFF", "KXTARIFFREVENUE",
    ],
    "Financials": [
        "KXINX", "KXINXU", "KXINXMAXY", "KXINXMINY",
        "KXNASDAQ100", "KXNASDAQ100U", "KXNASDAQ100Y",
        "KXUSTYLD", "KXTNOTEW", "KX10Y2Y", "KX10Y3M", "KX3MTBILL",
        "KXGOLDW", "KXSILVERW", "KXWTI", "KXWTIMAX",
    ],
    "Politics": [
        "CONTROLH", "CONTROLS",
    ],
}

# Flat list derived from the structured categories above.
DEFAULT_SERIES: list[str] = [
    t for tickers in SERIES_CATEGORIES.values() for t in tickers
]

# YES-side equity index series — backtesting shows BUY YES is only
# profitable on equity index contracts at high prices (>= 70c).
YES_SIDE_SERIES: list[str] = [
    "KXINX", "KXINXW", "KXINXM", "KXINXAB", "KXINXZ",
    "KXNASDAQ100", "KXNASDAQ100W", "KXNASDAQ100M", "KXNASDAQ100Z",
]


class Mode(StrEnum):
    DRIVING_RANGE = "driving_range"
    CHAMPIONSHIP = "championship"


# ---------------------------------------------------------------------------
# Config sub-models — each field carries wizard metadata via json_schema_extra
# ---------------------------------------------------------------------------


class PaperTradingConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Paper Trading",
        "section_description": (
            "Settings for Driving Range mode — your practice environment with virtual money."
        ),
        "section_order": 1,
    })

    starting_balance: float = Field(
        default=10_000.00, gt=0.0,
        json_schema_extra={
            "display_name": "Starting Balance",
            "description": (
                "The virtual bankroll you start with in Driving Range (paper trading) mode.\n"
                "This is play money — no real dollars are at risk. It lets you practice\n"
                "and see how the system performs before using real funds.\n"
                "(The Paper Bankroll setting separately limits how much can be invested at once.)\n"
                "\n"
                "A higher balance lets you take more/larger positions. A lower balance\n"
                "forces tighter discipline, which can be better practice."
            ),
            "min_val": 100.0,
            "max_val": 1_000_000.0,
        },
    )


class SideOverrides(BaseModel):
    """Per-side parameter overrides, used when strategy.side = 'both'."""

    min_market_price: float | None = Field(
        default=None,
        json_schema_extra={
            "display_name": "Min Market Price",
            "description": "Override min price for this side (None = use flat default).",
        },
    )
    max_market_price: float | None = Field(
        default=None,
        json_schema_extra={
            "display_name": "Max Market Price",
            "description": "Override max price for this side (None = use flat default).",
        },
    )
    min_true_probability: float | None = Field(
        default=None,
        json_schema_extra={
            "display_name": "Min True Probability",
            "description": "Override min probability for this side (None = use flat default).",
        },
    )
    gimme_threshold: int | None = Field(
        default=None,
        json_schema_extra={
            "display_name": "Gimme Threshold",
            "description": "Override threshold for this side (None = use flat default).",
        },
    )


class StrategyConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Strategy",
        "section_description": (
            "Core strategy parameters that control what the system considers a 'gimme'.\n"
            "These determine which markets qualify for trading."
        ),
        "section_order": 2,
    })

    gimme_threshold: int = Field(
        default=75, ge=0, le=100,
        json_schema_extra={
            "display_name": "Gimme Threshold",
            "description": (
                "The minimum Gimme Score (0–100) a market must reach before the system\n"
                "will consider trading it. Think of it like a confidence bar — the higher\n"
                "you set this, the pickier the system is.\n"
                "\n"
                "  • 75 (default): Only trade high-confidence opportunities\n"
                "  • Lower (e.g. 60): More trades, but some will have weaker edges\n"
                "  • Higher (e.g. 85): Fewer trades, but each one is very strong"
            ),
            "min_val": 0,
            "max_val": 100,
        },
    )
    side: Literal["yes", "no", "both"] = Field(
        default="no",
        json_schema_extra={
            "display_name": "Trading Side",
            "description": (
                "Which side of the contract to buy.\n"
                "\n"
                "  'yes': Buy YES contracts — profit when the event happens.\n"
                "  'no' (default): Buy NO contracts — profit when the event does NOT happen.\n"
                "  'both': Run both YES and NO strategies simultaneously with per-side\n"
                "    parameters. Set overrides via strategy.yes_overrides.* and\n"
                "    strategy.no_overrides.* (and scanner.yes_series / scanner.no_series).\n"
                "\n"
                "When set to 'no', the scanner and scorer flip their perspective:\n"
                "the price range, sweet spots, and edge calculations all operate\n"
                "from the NO buyer's viewpoint.\n"
                "\n"
                "Probability inputs (--prob, min_true_probability) are always\n"
                "interpreted from the configured side's perspective. When side='no',\n"
                "provide your confidence that NO wins (not YES)."
            ),
            "choices": ["yes", "no", "both"],
        },
    )
    yes_overrides: SideOverrides = Field(
        default_factory=SideOverrides,
    )
    no_overrides: SideOverrides = Field(
        default_factory=SideOverrides,
    )
    min_market_price: float = Field(
        default=0.55, gt=0.0, lt=1.0,
        json_schema_extra={
            "display_name": "Min Market Price",
            "description": (
                "The lowest buy price (in dollars) the system will consider,\n"
                "from the configured side's perspective. Kalshi contracts trade\n"
                "between $0.00 and $1.00.\n"
                "\n"
                "When side='yes', this is the minimum YES contract price.\n"
                "When side='no', a $0.55 min means the system only considers NO\n"
                "contracts priced at 55 cents or above (YES at 45 cents or below).\n"
                "Very cheap contracts are long shots, not gimmes.\n"
                "\n"
                "  • 0.55 (default): Focus on contracts with clear conviction\n"
                "  • Lower (e.g. 0.40): Include less certain markets"
            ),
            "min_val": 0.01,
            "max_val": 0.99,
        },
    )
    max_market_price: float = Field(
        default=0.85, gt=0.0, lt=1.0,
        json_schema_extra={
            "display_name": "Max Market Price",
            "description": (
                "The highest buy price (in dollars) the system will consider,\n"
                "from the configured side's perspective. Contracts near $1.00\n"
                "are near-certainties with very little profit margin.\n"
                "\n"
                "  • 0.85 (default): Skip contracts above 85 cents\n"
                "  • Higher (e.g. 0.92): Include pricier contracts with thinner margins\n"
                "  • Lower (e.g. 0.75): Only look at contracts with bigger potential upside"
            ),
            "min_val": 0.01,
            "max_val": 0.99,
        },
    )
    min_true_probability: float = Field(
        default=0.90, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Min True Probability",
            "description": (
                "The minimum probability our model must assign to a contract before we'll\n"
                "trade it. This is how confident we need to be that the event will happen.\n"
                "\n"
                "The 'edge' is the gap between our estimated probability and the market\n"
                "price. If a contract is priced at $0.70 but we think the true probability\n"
                "is 95%, that's a 25 percentage point edge — a strong gimme.\n"
                "\n"
                "  • 0.90 (default): We must be at least 90% confident\n"
                "  • Higher (e.g. 0.95): Even pickier — near certainty required\n"
                "  • Lower (e.g. 0.85): More trades, but accepting weaker convictions"
            ),
            "min_val": 0.50,
            "max_val": 0.99,
        },
    )
    min_edge_after_fees: float = Field(
        default=0.05, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Min Edge After Fees",
            "description": (
                "The minimum edge (in percentage points) we need AFTER accounting for\n"
                "Kalshi's trading fees. Edge = our probability estimate minus the market\n"
                "price. If this is too small, fees eat the profit.\n"
                "\n"
                "Example: Contract at $0.70, we estimate 80% true probability.\n"
                "  Raw edge = 0.80 - 0.70 = 0.10 (10 percentage points)\n"
                "  After fees (say ~1pp): net edge = ~0.09\n"
                "  If min_edge_after_fees = 0.05, this passes.\n"
                "\n"
                "  • 0.05 (default): Require at least 5pp edge after fees\n"
                "  • Higher (e.g. 0.10): Only take trades with big edges\n"
                "  • Lower (e.g. 0.03): Accept thinner margins (riskier)"
            ),
            "min_val": 0.01,
            "max_val": 0.50,
        },
    )
    cycle_timeout: int = Field(
        default=2700, gt=0,
        json_schema_extra={
            "display_name": "Cycle Timeout",
            "description": (
                "Maximum number of seconds an autonomous trading cycle can run before\n"
                "it's automatically stopped. This prevents runaway cycles from tying up\n"
                "resources indefinitely.\n"
                "\n"
                "  • 2700 (default, 45 minutes): Standard cycle length\n"
                "  • Lower (e.g. 900): Shorter 15-minute cycles\n"
                "  • Higher (e.g. 5400): Allow longer 90-minute cycles"
            ),
            "min_val": 60,
            "max_val": 86400,
        },
    )


class SizingConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Position Sizing",
        "section_description": (
            "How much money to put into each trade. Supports Kelly Criterion\n"
            "(optimal for high-conviction plays) and expected-value sizing\n"
            "(better for variance plays with moderate probability)."
        ),
        "section_order": 3,
    })

    mode: Literal["kelly", "ev"] = Field(
        default="kelly",
        json_schema_extra={
            "display_name": "Sizing Mode",
            "description": (
                "Which formula to use for calculating position sizes.\n"
                "\n"
                "  'kelly' (default): Kelly Criterion — optimal for high-conviction\n"
                "  plays where your estimated probability is 90%+.\n"
                "\n"
                "  'ev': Expected-Value sizing — better for variance plays where\n"
                "  probability is moderate (30-60%) but expected value is positive.\n"
                "  Sizes scale linearly with edge-to-cost ratio."
            ),
            "choices": ["kelly", "ev"],
        },
    )
    kelly_fraction: float = Field(
        default=0.25, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Kelly Fraction",
            "description": (
                "How much of the 'optimal' bet size to actually use.\n"
                "\n"
                "The Kelly Criterion is a formula from probability theory that calculates\n"
                "the mathematically optimal bet size to maximize long-term growth. But\n"
                "full Kelly is aggressive — it assumes perfect probability estimates,\n"
                "which we don't have.\n"
                "\n"
                "So we use a FRACTION of Kelly:\n"
                "  • 0.25 (default, 'quarter-Kelly'): Very conservative. Slower growth\n"
                "    but much less risk of big drawdowns. Recommended for beginners.\n"
                "  • 0.50 ('half-Kelly'): Moderate. Faster growth but bumpier ride.\n"
                "  • 1.00 ('full Kelly'): Maximum growth rate in theory, but in practice\n"
                "    very volatile. Not recommended."
            ),
            "min_val": 0.01,
            "max_val": 1.0,
        },
    )
    max_position_pct: float = Field(
        default=0.05, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Max Position Size",
            "description": (
                "The maximum percentage of your bankroll that can go into a single trade.\n"
                "This is a hard cap that overrides Kelly sizing if Kelly suggests more.\n"
                "\n"
                "This protects you from concentration risk — putting too much into one\n"
                "bet, no matter how good it looks.\n"
                "\n"
                "  • 0.05 (default, 5%): At most $500 of a $10,000 bankroll per trade\n"
                "  • Lower (e.g. 0.02): Very conservative, many small bets\n"
                "  • Higher (e.g. 0.10): Allow larger bets on strong convictions"
            ),
            "min_val": 0.01,
            "max_val": 0.50,
        },
    )


class RiskConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Risk Management",
        "section_description": (
            "Safety limits that protect your bankroll from large losses.\n"
            "These are hard stops that override everything else."
        ),
        "section_order": 4,
    })

    max_open_positions: int = Field(
        default=15, gt=0,
        json_schema_extra={
            "display_name": "Max Open Positions",
            "description": (
                "The maximum number of trades the system can have open at the same time.\n"
                "Once this limit is hit, no new trades are allowed until existing ones\n"
                "close (either by settlement or manual close).\n"
                "\n"
                "This prevents over-diversification (spreading too thin) and limits your\n"
                "overall market exposure.\n"
                "\n"
                "  • 15 (default): Room for a diverse portfolio\n"
                "  • Lower (e.g. 5): Concentrated, focused portfolio\n"
                "  • Higher (e.g. 25): More simultaneous bets"
            ),
            "min_val": 1,
            "max_val": 100,
        },
    )
    daily_loss_limit_pct: float = Field(
        default=0.15, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Daily Loss Limit",
            "description": (
                "If your losses for the day exceed this percentage of your bankroll,\n"
                "the system stops trading for the rest of the day. This is a circuit\n"
                "breaker that prevents catastrophic losses from a bad streak.\n"
                "\n"
                "  • 0.15 (default, 15%): Stop after losing $1,500 of a $10,000 bankroll\n"
                "  • Lower (e.g. 0.05): Very cautious — stops early\n"
                "  • Higher (e.g. 0.25): More tolerance for daily swings"
            ),
            "min_val": 0.01,
            "max_val": 0.50,
        },
    )
    bankroll_paper: float = Field(
        default=5_000.0, gt=0.0,
        json_schema_extra={
            "display_name": "Paper Bankroll",
            "description": (
                "The maximum total cost basis for paper (driving range) trading.\n"
                "This caps deployed capital across all open paper positions.\n"
                "It is separate from your starting balance — it limits how much\n"
                "of that virtual money can be actively invested at once.\n"
                "\n"
                "  • 5,000.00 (default): Half of the $10,000 paper balance\n"
                "  • Lower (e.g. 1000): Limit paper exposure while learning\n"
                "  • Higher (e.g. 8000): Allow more paper capital deployment"
            ),
            "min_val": 1.0,
            "max_val": 1_000_000.0,
        },
    )
    bankroll_real: float = Field(
        default=0.0, ge=0.0, le=1_000_000.0,
        json_schema_extra={
            "display_name": "Championship Bankroll",
            "description": (
                "The maximum total cost basis for championship (real money) trading.\n"
                "This caps deployed capital across all open real positions.\n"
                "Set to 0 until you're ready — you'll be prompted to confirm\n"
                "or adjust every time you enter championship mode.\n"
                "\n"
                "  • 0 (default): Not yet configured — must set before trading\n"
                "  • 500: Conservative starting point for real money\n"
                "  • Higher (e.g. 2000): Allow more real capital deployment"
            ),
            "min_val": 0.0,
            "max_val": 1_000_000.0,
        },
    )
    monitor_price_trigger_pp: int = Field(
        default=10, ge=1, le=50,
        json_schema_extra={
            "display_name": "Monitor Price Trigger",
            "description": (
                "Flag open positions when the market price moves this many percentage\n"
                "points from your entry price. This triggers monitoring alerts so you\n"
                "can review positions that have moved significantly.\n"
                "\n"
                "  • 10 (default): Flag at 10pp move from entry\n"
                "  • Lower (e.g. 5): More sensitive — flag smaller moves\n"
                "  • Higher (e.g. 20): Only flag large moves"
            ),
            "min_val": 1,
            "max_val": 50,
        },
    )
    position_stop_loss_pct: float = Field(
        default=0.15, ge=0.05, le=0.50,
        json_schema_extra={
            "display_name": "Position Stop-Loss",
            "description": (
                "Flag a position when its unrealized loss reaches this percentage\n"
                "of its cost basis. This triggers a Monitor flag so Caddie Master\n"
                "can review the position — it does NOT automatically sell.\n"
                "\n"
                "  • 0.15 (default, 15%): Flag when loss hits 15% of cost basis\n"
                "  • Lower (e.g. 0.05): Very sensitive — flag small losses early\n"
                "  • Higher (e.g. 0.30): Only flag when loss is substantial"
            ),
            "min_val": 0.05,
            "max_val": 0.50,
        },
    )
    position_take_profit_pct: float = Field(
        default=0.80, ge=0.50, le=0.95,
        json_schema_extra={
            "display_name": "Position Take-Profit",
            "description": (
                "Flag a position when its unrealized gain reaches this percentage\n"
                "of its maximum possible profit. This triggers a Monitor flag so\n"
                "Caddie Master can review — it does NOT automatically sell.\n"
                "\n"
                "  • 0.80 (default, 80%): Flag when gain hits 80% of max profit\n"
                "  • Lower (e.g. 0.60): Flag earlier to lock in gains sooner\n"
                "  • Higher (e.g. 0.95): Only flag when nearly at max profit"
            ),
            "min_val": 0.50,
            "max_val": 0.95,
        },
    )
    max_event_exposure_pct: float = Field(
        default=0.15, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Max Event Exposure",
            "description": (
                "Maximum percentage of bankroll deployed in a single event.\n"
                "An event groups related markets (e.g. all CPI brackets for March).\n"
                "Prevents over-concentration in correlated outcomes.\n"
                "\n"
                "  • 0.15 (default, 15%): Max $1,500 per event on $10K bankroll\n"
                "  • Lower (e.g. 0.10): Stricter diversification\n"
                "  • Higher (e.g. 0.25): Allow more concentrated event bets"
            ),
            "min_val": 0.01,
            "max_val": 1.0,
        },
    )
    max_series_exposure_pct: float = Field(
        default=0.30, gt=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Max Series Exposure",
            "description": (
                "Maximum percentage of bankroll deployed in a single series.\n"
                "A series groups all events of a kind (e.g. all CPI across months).\n"
                "Prevents over-concentration in a single economic indicator.\n"
                "\n"
                "  • 0.30 (default, 30%): Max $3,000 per series on $10K bankroll\n"
                "  • Lower (e.g. 0.15): Stricter series diversification\n"
                "  • Higher (e.g. 0.50): Allow more concentrated series bets"
            ),
            "min_val": 0.01,
            "max_val": 1.0,
        },
    )


class OrdersConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Order Execution",
        "section_description": "How the system places trades on Kalshi.",
        "section_order": 5,
    })

    preferred_order_type: str = Field(
        default="maker",
        json_schema_extra={
            "display_name": "Preferred Order Type",
            "description": (
                "Whether to use 'maker' (limit) or 'taker' (market) orders.\n"
                "\n"
                "  Maker (limit order): You set your price and wait for someone to\n"
                "  trade against you. Fees are ~75% lower but the order might not fill\n"
                "  if the market moves away from your price.\n"
                "\n"
                "  Taker (market order): You trade immediately at the current best price.\n"
                "  Higher fees but guaranteed fill.\n"
                "\n"
                "  • 'maker' (default): Lower fees, preferred for gimmes strategy\n"
                "  • 'taker': Instant fills, useful when you need to enter quickly"
            ),
            "choices": ["maker", "taker"],
        },
    )


class ScannerConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Market Scanner",
        "section_description": (
            "Filters that control which markets the Scout examines.\n"
            "These determine the initial pool of candidates before scoring."
        ),
        "section_order": 6,
    })

    min_volume: int = Field(
        default=100, ge=0, le=100_000,
        json_schema_extra={
            "display_name": "Min Volume (24h)",
            "description": (
                "The minimum number of contracts traded in the last 24 hours for a market\n"
                "to be considered. Low-volume markets are illiquid — hard to get in and\n"
                "out of, and prices may not reflect true sentiment.\n"
                "\n"
                "  • 100 (default): Reasonable activity floor\n"
                "  • Lower (e.g. 25): Include quieter markets (may be harder to trade)\n"
                "  • Higher (e.g. 500): Only well-traded markets"
            ),
            "min_val": 0,
            "max_val": 100_000,
        },
    )
    min_open_interest: int = Field(
        default=50, ge=0, le=100_000,
        json_schema_extra={
            "display_name": "Min Open Interest",
            "description": (
                "The minimum number of contracts currently held by traders. Open interest\n"
                "shows how much money is committed to a market — higher means more\n"
                "participants and generally more reliable pricing.\n"
                "\n"
                "  • 50 (default): Moderate participation required\n"
                "  • Lower (e.g. 10): Include newer or niche markets\n"
                "  • Higher (e.g. 200): Only well-established markets"
            ),
            "min_val": 0,
            "max_val": 100_000,
        },
    )
    max_days_to_resolution: float = Field(
        default=90.0, ge=1.0, le=365.0,
        json_schema_extra={
            "display_name": "Max Days to Resolution",
            "description": (
                "Skip markets that won't resolve for longer than this many days.\n"
                "Very long-dated contracts tie up capital and have more uncertainty.\n"
                "\n"
                "  • 90 (default): Up to ~3 months out\n"
                "  • Lower (e.g. 30): Focus on near-term events only\n"
                "  • Higher (e.g. 180): Include longer-dated opportunities"
            ),
            "min_val": 1.0,
            "max_val": 365.0,
        },
    )
    min_days_to_resolution: float = Field(
        default=0.5, ge=0.0, le=30.0,
        json_schema_extra={
            "display_name": "Min Days to Resolution",
            "description": (
                "Skip markets that resolve sooner than this many days. Very short-dated\n"
                "markets may not leave enough time for maker orders to fill.\n"
                "\n"
                "  • 0.5 (default, 12 hours): Filter out markets resolving in < 12 hours\n"
                "  • Lower (e.g. 0.1): Include markets resolving in a few hours\n"
                "  • Higher (e.g. 1.0): Require at least a full day"
            ),
            "min_val": 0.0,
            "max_val": 30.0,
        },
    )
    series: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SERIES),
        json_schema_extra={
            "display_name": "Series Watchlist",
            "description": (
                "The list of Kalshi series tickers to scan. A 'series' is a group of\n"
                "related markets (e.g. KXCPI covers all CPI-related contracts).\n"
                "\n"
                "By default, the scanner only looks at markets in these series rather\n"
                "than scanning ALL of Kalshi. This keeps scans fast and focused on\n"
                "categories where the system has informational edge.\n"
                "\n"
                "Use 'gimmes discover <Category>' to find new series tickers.\n"
                "Categories: Economics, Politics, Financials, etc.\n"
                "\n"
                "You can add or remove tickers from this list. Enter a comma-separated\n"
                "list to replace, or press Enter to keep the current list."
            ),
        },
    )
    staleness_cycles: int = Field(
        default=5, ge=0, le=50,
        json_schema_extra={
            "display_name": "Staleness Cycles",
            "description": (
                "Skip markets with no trading activity (no volume, no OI change,\n"
                "no price change) for this many consecutive scan cycles. Markets\n"
                "with stable prices but active volume are NOT considered stale.\n"
                "Set to 0 to disable staleness filtering.\n"
                "\n"
                "  • 5 (default): Skip after 5 inactive scans\n"
                "  • 0: Never skip for staleness\n"
                "  • Higher (e.g. 10): More tolerant of quiet markets"
            ),
            "min_val": 0,
            "max_val": 50,
        },
    )
    yes_series: list[str] = Field(
        default_factory=lambda: list(YES_SIDE_SERIES),
        json_schema_extra={
            "display_name": "YES-Side Series",
            "description": (
                "Series watchlist for the YES side in dual-side mode.\n"
                "Defaults to equity index series (S&P 500, Nasdaq-100)."
            ),
        },
    )
    no_series: list[str] | None = Field(
        default=None,
        json_schema_extra={
            "display_name": "NO-Side Series",
            "description": (
                "Series watchlist for the NO side in dual-side mode.\n"
                "None = use the main series watchlist."
            ),
        },
    )


class ScoringWeights(BaseModel):
    edge_size: float = Field(
        default=0.30, ge=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Weight: Edge Size",
            "description": (
                "How much weight to give the size of the edge (gap between our estimated\n"
                "probability and the market price). Bigger edges mean more potential profit.\n"
                "\n"
                "All five scoring weights must add up to 1.0. Increasing one means\n"
                "decreasing others."
            ),
            "min_val": 0.0,
            "max_val": 1.0,
        },
    )
    signal_strength: float = Field(
        default=0.25, ge=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Weight: Signal Strength",
            "description": (
                "How much weight to give the number and quality of confirming signals.\n"
                "More independent sources agreeing on an outcome increases confidence.\n"
                "\n"
                "All five scoring weights must add up to 1.0."
            ),
            "min_val": 0.0,
            "max_val": 1.0,
        },
    )
    liquidity_depth: float = Field(
        default=0.15, ge=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Weight: Liquidity Depth",
            "description": (
                "How much weight to give market liquidity — whether there are enough\n"
                "orders on the book for us to actually fill our trade at a good price.\n"
                "Thin markets can cause slippage (worse fills than expected).\n"
                "\n"
                "All five scoring weights must add up to 1.0."
            ),
            "min_val": 0.0,
            "max_val": 1.0,
        },
    )
    settlement_clarity: float = Field(
        default=0.15, ge=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Weight: Settlement Clarity",
            "description": (
                "How much weight to give the clarity of the contract's settlement rules.\n"
                "Some Kalshi contracts have ambiguous resolution criteria or subjective\n"
                "carve-outs that could lead to unexpected outcomes. Higher weight means\n"
                "the system avoids ambiguous contracts more aggressively.\n"
                "\n"
                "All five scoring weights must add up to 1.0."
            ),
            "min_val": 0.0,
            "max_val": 1.0,
        },
    )
    time_to_resolution: float = Field(
        default=0.15, ge=0.0, le=1.0,
        json_schema_extra={
            "display_name": "Weight: Time to Resolution",
            "description": (
                "How much weight to give the time until the contract resolves. There's\n"
                "a sweet spot — too soon and we can't fill; too far out and capital is\n"
                "locked up. Higher weight means the system penalizes contracts outside\n"
                "the ideal time window more.\n"
                "\n"
                "All five scoring weights must add up to 1.0."
            ),
            "min_val": 0.0,
            "max_val": 1.0,
        },
    )

    @model_validator(mode="after")
    def _check_weights_sum(self) -> ScoringWeights:
        total = (
            self.edge_size + self.signal_strength + self.liquidity_depth
            + self.settlement_clarity + self.time_to_resolution
        )
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"Scoring weights must sum to 1.0 (got {total:.4f})"
            )
        return self


class ScoringConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "section_name": "Scoring Weights",
        "section_description": (
            "How the Gimme Score is calculated. Each dimension gets a weight (0–1)\n"
            "and all five must add up to 1.0. Adjust these to shift the system's\n"
            "priorities — e.g., increase edge_size weight to favor high-edge trades\n"
            "even if liquidity is thinner."
        ),
        "section_order": 7,
    })

    weights: ScoringWeights = Field(default_factory=ScoringWeights)


# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------


class GimmesConfig(BaseModel):
    mode: Mode = Mode.DRIVING_RANGE

    # Kalshi credentials (prod — used for market data in both modes)
    api_key: str = ""
    private_key_path: Path = Path()
    private_key_password: str | None = None

    # API URLs (always prod — paper trading simulates orders locally)
    base_url: str = PROD_BASE_URL
    ws_url: str = PROD_WS_URL

    # Strategy parameters (from database)
    paper: PaperTradingConfig = Field(default_factory=PaperTradingConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)

    # Database
    db_path: Path = Field(default_factory=lambda: GIMMES_HOME / "gimmes.db")

    @property
    def is_championship(self) -> bool:
        return self.mode == Mode.CHAMPIONSHIP

    @property
    def position_table(self) -> str:
        """Return the position table name for the current mode."""
        return "positions" if self.is_championship else "paper_positions"

    @property
    def bankroll(self) -> float:
        """Return the mode-appropriate bankroll."""
        if self.is_championship:
            return self.risk.bankroll_real
        return self.risk.bankroll_paper

    @property
    def sides_to_scan(self) -> list[Literal["yes", "no"]]:
        """Return the list of sides to scan for candidates."""
        if self.strategy.side == "both":
            return ["yes", "no"]
        return [self.strategy.side]  # type: ignore[list-item]

    def effective_config_for_side(
        self, side: Literal["yes", "no"],
    ) -> GimmesConfig:
        """Return a config tuned for a specific side.

        When ``strategy.side`` is ``"yes"`` or ``"no"``, returns *self*
        unchanged.  When ``"both"``, returns a copy with strategy and
        scanner fields overridden from the per-side settings.
        """
        if self.strategy.side != "both":
            return self

        overrides = (
            self.strategy.yes_overrides
            if side == "yes"
            else self.strategy.no_overrides
        )

        strategy_kwargs = self.strategy.model_dump()
        strategy_kwargs["side"] = side
        strategy_kwargs.pop("yes_overrides", None)
        strategy_kwargs.pop("no_overrides", None)

        for field_name in (
            "min_market_price",
            "max_market_price",
            "min_true_probability",
            "gimme_threshold",
        ):
            val = getattr(overrides, field_name)
            if val is not None:
                strategy_kwargs[field_name] = val

        scanner_kwargs = self.scanner.model_dump()
        side_series = (
            self.scanner.yes_series
            if side == "yes"
            else self.scanner.no_series
        )
        if side_series is not None:
            scanner_kwargs["series"] = side_series

        return self.model_copy(update={
            "strategy": StrategyConfig(**strategy_kwargs),
            "scanner": ScannerConfig(**scanner_kwargs),
        })


# ---------------------------------------------------------------------------
# Database-backed config I/O
# ---------------------------------------------------------------------------

# Sub-models in wizard walkthrough order (field_name -> model class).
CONFIG_SECTIONS: list[tuple[str, type[BaseModel]]] = [
    ("paper", PaperTradingConfig),
    ("strategy", StrategyConfig),
    ("sizing", SizingConfig),
    ("risk", RiskConfig),
    ("orders", OrdersConfig),
    ("scanner", ScannerConfig),
    ("scoring", ScoringConfig),
]


def _query_config_db(db_path: Path, sql: str) -> list[tuple]:
    """Run a read-only query against the config table, returning rows.

    Returns an empty list if the database or config table does not exist.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise ValueError(
            f"Failed to read config from {db_path}: {e}. "
            "Check database integrity or run 'gimmes init' to recreate."
        ) from e


def _load_config_from_db(db_path: Path) -> dict:
    """Read config key-value pairs from SQLite, return nested dict."""
    rows = _query_config_db(db_path, "SELECT key, value FROM config")

    result: dict = {}
    for key, raw_value in rows:
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            logger.warning(
                "Corrupt config value for '%s' (falling back to default). "
                "Raw value: %r",
                key,
                raw_value,
            )
            continue
        parts = key.split(".")
        current = result
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = parsed
    return result


def config_keys_in_db(db_path: Path) -> set[str]:
    """Return the set of dotted keys that have explicit values in the database."""
    rows = _query_config_db(db_path, "SELECT key FROM config")
    return {row[0] for row in rows}


def save_config_value(key: str, value: object, db_path: Path | None = None) -> None:
    """Write a single config value to the database."""
    save_config_values({key: value}, db_path=db_path)


def save_config_values(values: dict[str, object], db_path: Path | None = None) -> None:
    """Write multiple config values atomically."""
    resolved_db = db_path or GIMMES_HOME / "gimmes.db"
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved_db))
    try:
        # Ensure config table exists (handles pre-migration databases)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def load_config(db_path: Path | None = None) -> GimmesConfig:
    """Load configuration from env vars and database."""
    mode_str = os.getenv("GIMMES_MODE", "driving_range").lower()
    mode = Mode(mode_str)

    # Both modes use prod credentials (driving range reads real market data)
    api_key = os.getenv("KALSHI_PROD_API_KEY", "")
    key_path_str = os.getenv("KALSHI_PROD_PRIVATE_KEY_PATH", "")
    private_key_path = Path(key_path_str).expanduser() if key_path_str else Path()
    private_key_password = os.getenv("KALSHI_PRIVATE_KEY_PASSWORD") or None

    # Load user values from DB
    resolved_db = db_path or GIMMES_HOME / "gimmes.db"
    overrides = _load_config_from_db(resolved_db)

    # Warn when strategy-critical fields fall back to code defaults.
    # These fields directly affect trade direction and filtering, so silent
    # drift from a code-default change can cause unintended trades.
    strategy_critical = ("side", "min_market_price", "max_market_price")
    strategy_overrides = overrides.get("strategy", {})
    missing = [f for f in strategy_critical if f not in strategy_overrides]
    if missing:
        logger.warning(
            "Strategy fields not pinned in database (using code defaults): %s. "
            "Run 'gimmes config set' for each to persist your intended values.",
            ", ".join(f"strategy.{f}" for f in missing),
        )

    return GimmesConfig(
        mode=mode,
        api_key=api_key,
        private_key_path=private_key_path,
        private_key_password=private_key_password,
        db_path=resolved_db,
        paper=PaperTradingConfig(**overrides.get("paper", {})),
        strategy=StrategyConfig(**overrides.get("strategy", {})),
        sizing=SizingConfig(**overrides.get("sizing", {})),
        risk=RiskConfig(**overrides.get("risk", {})),
        orders=OrdersConfig(**overrides.get("orders", {})),
        scanner=ScannerConfig(**overrides.get("scanner", {})),
        scoring=ScoringConfig(**overrides.get("scoring", {})),
    )
