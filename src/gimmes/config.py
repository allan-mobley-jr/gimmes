"""Two-layer configuration: secrets from env vars, strategy params from TOML."""

from __future__ import annotations

import os
import tomllib  # type: ignore[no-redef]
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
PROD_WS_URL = "wss://api.kalshi.com/trade-api/ws/v2"

DEFAULT_CONFIG_PATH = Path("config/gimmes.toml")


class Mode(StrEnum):
    DRIVING_RANGE = "driving_range"
    CHAMPIONSHIP = "championship"


# ---------------------------------------------------------------------------
# TOML sub-models
# ---------------------------------------------------------------------------


class StrategyConfig(BaseModel):
    gimme_threshold: int = 75
    min_market_price: float = 0.55
    max_market_price: float = 0.85
    min_true_probability: float = 0.90
    min_edge_after_fees: float = 0.05


class SizingConfig(BaseModel):
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.05


class RiskConfig(BaseModel):
    max_open_positions: int = 15
    daily_loss_limit_pct: float = 0.15


class OrdersConfig(BaseModel):
    preferred_order_type: str = "maker"


class ScannerConfig(BaseModel):
    min_volume: int = 100
    min_open_interest: int = 50
    max_days_to_resolution: float = 90.0
    min_days_to_resolution: float = 0.5
    categories: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    edge_size: float = 0.30
    signal_strength: float = 0.25
    liquidity_depth: float = 0.15
    settlement_clarity: float = 0.15
    time_to_resolution: float = 0.15


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)


# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------


class GimmesConfig(BaseModel):
    mode: Mode = Mode.DRIVING_RANGE

    # Kalshi credentials
    api_key: str = ""
    private_key_path: Path = Path()

    # API URLs
    base_url: str = DEMO_BASE_URL
    ws_url: str = DEMO_WS_URL

    # Strategy parameters (from TOML)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)

    # Database
    db_path: Path = Path("gimmes.db")

    @property
    def is_championship(self) -> bool:
        return self.mode == Mode.CHAMPIONSHIP


def load_config(config_path: Path | None = None) -> GimmesConfig:
    """Load configuration from env vars and TOML file."""
    mode_str = os.getenv("GIMMES_MODE", "driving_range").lower()
    mode = Mode(mode_str)

    # Select credentials based on mode
    if mode == Mode.CHAMPIONSHIP:
        api_key = os.getenv("KALSHI_PROD_API_KEY", "")
        key_path_str = os.getenv("KALSHI_PROD_PRIVATE_KEY_PATH", "")
        base_url = PROD_BASE_URL
        ws_url = PROD_WS_URL
    else:
        api_key = os.getenv("KALSHI_DEMO_API_KEY", "")
        key_path_str = os.getenv("KALSHI_DEMO_PRIVATE_KEY_PATH", "")
        base_url = DEMO_BASE_URL
        ws_url = DEMO_WS_URL

    private_key_path = Path(key_path_str).expanduser() if key_path_str else Path()

    # Load TOML config
    toml_path = config_path or DEFAULT_CONFIG_PATH
    toml_data: dict = {}  # type: ignore[type-arg]
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            toml_data = tomllib.load(f)

    return GimmesConfig(
        mode=mode,
        api_key=api_key,
        private_key_path=private_key_path,
        base_url=base_url,
        ws_url=ws_url,
        strategy=StrategyConfig(**toml_data.get("strategy", {})),
        sizing=SizingConfig(**toml_data.get("sizing", {})),
        risk=RiskConfig(**toml_data.get("risk", {})),
        orders=OrdersConfig(**toml_data.get("orders", {})),
        scanner=ScannerConfig(**toml_data.get("scanner", {})),
        scoring=ScoringConfig(**toml_data.get("scoring", {})),
    )
