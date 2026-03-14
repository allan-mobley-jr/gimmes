"""Two-layer configuration: secrets from env vars, strategy params from TOML."""

from __future__ import annotations

import os
import tomllib  # type: ignore[no-redef]
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

GIMMES_HOME = Path(os.getenv("GIMMES_HOME", str(Path.home() / ".gimmes"))).expanduser()

load_dotenv(dotenv_path=GIMMES_HOME / ".env")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

DEFAULT_CONFIG_PATH = GIMMES_HOME / "config" / "gimmes.toml"


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
    series: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    edge_size: float = 0.30
    signal_strength: float = 0.25
    liquidity_depth: float = 0.15
    settlement_clarity: float = 0.15
    time_to_resolution: float = 0.15


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)


class PaperTradingConfig(BaseModel):
    starting_balance: float = 10_000.00


# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------


class GimmesConfig(BaseModel):
    mode: Mode = Mode.DRIVING_RANGE

    # Kalshi credentials (prod — used for market data in both modes)
    api_key: str = ""
    private_key_path: Path = Path()

    # API URLs (always prod — paper trading simulates orders locally)
    base_url: str = PROD_BASE_URL
    ws_url: str = PROD_WS_URL

    # Strategy parameters (from TOML)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    paper: PaperTradingConfig = Field(default_factory=PaperTradingConfig)

    # Database
    db_path: Path = Field(default_factory=lambda: GIMMES_HOME / "gimmes.db")

    @property
    def is_championship(self) -> bool:
        return self.mode == Mode.CHAMPIONSHIP


def load_config(config_path: Path | None = None) -> GimmesConfig:
    """Load configuration from env vars and TOML file."""
    mode_str = os.getenv("GIMMES_MODE", "driving_range").lower()
    mode = Mode(mode_str)

    # Both modes use prod credentials (driving range reads real market data)
    api_key = os.getenv("KALSHI_PROD_API_KEY", "")
    key_path_str = os.getenv("KALSHI_PROD_PRIVATE_KEY_PATH", "")
    private_key_path = Path(key_path_str).expanduser() if key_path_str else Path()

    # Load TOML config
    toml_path = config_path or DEFAULT_CONFIG_PATH
    toml_data: dict = {}  # type: ignore[type-arg]
    if toml_path.exists():
        try:
            with open(toml_path, "rb") as f:
                toml_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f"Failed to parse {toml_path}: {e}. "
                "Fix or delete the TOML file to continue."
            ) from e

    return GimmesConfig(
        mode=mode,
        api_key=api_key,
        private_key_path=private_key_path,
        strategy=StrategyConfig(**toml_data.get("strategy", {})),
        sizing=SizingConfig(**toml_data.get("sizing", {})),
        risk=RiskConfig(**toml_data.get("risk", {})),
        orders=OrdersConfig(**toml_data.get("orders", {})),
        scanner=ScannerConfig(**toml_data.get("scanner", {})),
        scoring=ScoringConfig(**toml_data.get("scoring", {})),
        paper=PaperTradingConfig(**toml_data.get("paper", {})),
    )
