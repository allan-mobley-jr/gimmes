"""gimmes config — interactive configuration wizard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOML_FILE = PROJECT_ROOT / "config" / "gimmes.toml"


# ---------------------------------------------------------------------------
# Setting definitions
# ---------------------------------------------------------------------------


@dataclass
class Setting:
    """Metadata for a single configuration setting."""

    key: str  # TOML dotted key, e.g. "strategy.gimme_threshold"
    name: str  # Human-readable name
    description: str  # Plain-language explanation for novices
    type: str  # "int", "float", "str", "list"
    default: int | float | str | list[str]
    min_val: float | None = None
    max_val: float | None = None
    choices: list[str] = field(default_factory=list)

    @property
    def section(self) -> str:
        return self.key.split(".")[0]


# The order here defines the walkthrough order.
SETTINGS: list[Setting] = [
    # --- Paper ---
    Setting(
        key="paper.starting_balance",
        name="Starting Balance",
        description=(
            "The virtual bankroll you start with in Driving Range (paper trading) mode.\n"
            "This is play money — no real dollars are at risk. It lets you practice\n"
            "and see how the system performs before using real funds.\n"
            "\n"
            "A higher balance lets you take more/larger positions. A lower balance\n"
            "forces tighter discipline, which can be better practice."
        ),
        type="float",
        default=10_000.00,
        min_val=100.0,
        max_val=1_000_000.0,
    ),
    # --- Strategy ---
    Setting(
        key="strategy.gimme_threshold",
        name="Gimme Threshold",
        description=(
            "The minimum Gimme Score (0–100) a market must reach before the system\n"
            "will consider trading it. Think of it like a confidence bar — the higher\n"
            "you set this, the pickier the system is.\n"
            "\n"
            "  • 75 (default): Only trade high-confidence opportunities\n"
            "  • Lower (e.g. 60): More trades, but some will have weaker edges\n"
            "  • Higher (e.g. 85): Fewer trades, but each one is very strong"
        ),
        type="int",
        default=75,
        min_val=0,
        max_val=100,
    ),
    Setting(
        key="strategy.min_market_price",
        name="Min Market Price",
        description=(
            "The lowest contract price (in dollars) the system will look at.\n"
            "Kalshi contracts trade between $0.00 and $1.00, where the price\n"
            "roughly reflects the market's estimated probability of the event.\n"
            "\n"
            "A contract at $0.55 means the market thinks there's about a 55% chance.\n"
            "We only look at contracts above this floor because very cheap contracts\n"
            "(e.g. $0.10) are long shots, not gimmes.\n"
            "\n"
            "  • 0.55 (default): Focus on clear favorites\n"
            "  • Lower (e.g. 0.40): Include less certain markets"
        ),
        type="float",
        default=0.55,
        min_val=0.01,
        max_val=0.99,
    ),
    Setting(
        key="strategy.max_market_price",
        name="Max Market Price",
        description=(
            "The highest contract price (in dollars) the system will look at.\n"
            "Contracts near $1.00 are already priced as near-certainties, so there's\n"
            "very little profit left even if you're right.\n"
            "\n"
            "  • 0.85 (default): Skip contracts above 85 cents\n"
            "  • Higher (e.g. 0.92): Include pricier contracts with thinner margins\n"
            "  • Lower (e.g. 0.75): Only look at contracts with bigger potential upside"
        ),
        type="float",
        default=0.85,
        min_val=0.01,
        max_val=0.99,
    ),
    Setting(
        key="strategy.min_true_probability",
        name="Min True Probability",
        description=(
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
        type="float",
        default=0.90,
        min_val=0.50,
        max_val=0.99,
    ),
    Setting(
        key="strategy.min_edge_after_fees",
        name="Min Edge After Fees",
        description=(
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
        type="float",
        default=0.05,
        min_val=0.01,
        max_val=0.50,
    ),
    # --- Sizing ---
    Setting(
        key="sizing.kelly_fraction",
        name="Kelly Fraction",
        description=(
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
        type="float",
        default=0.25,
        min_val=0.01,
        max_val=1.0,
    ),
    Setting(
        key="sizing.max_position_pct",
        name="Max Position Size",
        description=(
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
        type="float",
        default=0.05,
        min_val=0.01,
        max_val=0.50,
    ),
    # --- Risk ---
    Setting(
        key="risk.max_open_positions",
        name="Max Open Positions",
        description=(
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
        type="int",
        default=15,
        min_val=1,
        max_val=100,
    ),
    Setting(
        key="risk.daily_loss_limit_pct",
        name="Daily Loss Limit",
        description=(
            "If your losses for the day exceed this percentage of your bankroll,\n"
            "the system stops trading for the rest of the day. This is a circuit\n"
            "breaker that prevents catastrophic losses from a bad streak.\n"
            "\n"
            "  • 0.15 (default, 15%): Stop after losing $1,500 of a $10,000 bankroll\n"
            "  • Lower (e.g. 0.05): Very cautious — stops early\n"
            "  • Higher (e.g. 0.25): More tolerance for daily swings"
        ),
        type="float",
        default=0.15,
        min_val=0.01,
        max_val=0.50,
    ),
    # --- Orders ---
    Setting(
        key="orders.preferred_order_type",
        name="Preferred Order Type",
        description=(
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
        type="str",
        default="maker",
        choices=["maker", "taker"],
    ),
    # --- Scanner ---
    Setting(
        key="scanner.min_volume",
        name="Min Volume (24h)",
        description=(
            "The minimum number of contracts traded in the last 24 hours for a market\n"
            "to be considered. Low-volume markets are illiquid — hard to get in and\n"
            "out of, and prices may not reflect true sentiment.\n"
            "\n"
            "  • 100 (default): Reasonable activity floor\n"
            "  • Lower (e.g. 25): Include quieter markets (may be harder to trade)\n"
            "  • Higher (e.g. 500): Only well-traded markets"
        ),
        type="int",
        default=100,
        min_val=0,
        max_val=100_000,
    ),
    Setting(
        key="scanner.min_open_interest",
        name="Min Open Interest",
        description=(
            "The minimum number of contracts currently held by traders. Open interest\n"
            "shows how much money is committed to a market — higher means more\n"
            "participants and generally more reliable pricing.\n"
            "\n"
            "  • 50 (default): Moderate participation required\n"
            "  • Lower (e.g. 10): Include newer or niche markets\n"
            "  • Higher (e.g. 200): Only well-established markets"
        ),
        type="int",
        default=50,
        min_val=0,
        max_val=100_000,
    ),
    Setting(
        key="scanner.max_days_to_resolution",
        name="Max Days to Resolution",
        description=(
            "Skip markets that won't resolve for longer than this many days.\n"
            "Very long-dated contracts tie up capital and have more uncertainty.\n"
            "\n"
            "  • 90 (default): Up to ~3 months out\n"
            "  • Lower (e.g. 30): Focus on near-term events only\n"
            "  • Higher (e.g. 180): Include longer-dated opportunities"
        ),
        type="float",
        default=90.0,
        min_val=1.0,
        max_val=365.0,
    ),
    Setting(
        key="scanner.min_days_to_resolution",
        name="Min Days to Resolution",
        description=(
            "Skip markets that resolve sooner than this many days. Very short-dated\n"
            "markets may not leave enough time for maker orders to fill.\n"
            "\n"
            "  • 0.5 (default, 12 hours): Filter out markets resolving in < 12 hours\n"
            "  • Lower (e.g. 0.1): Include markets resolving in a few hours\n"
            "  • Higher (e.g. 1.0): Require at least a full day"
        ),
        type="float",
        default=0.5,
        min_val=0.0,
        max_val=30.0,
    ),
    Setting(
        key="scanner.series",
        name="Series Watchlist",
        description=(
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
        type="list",
        default=[],
    ),
    # --- Scoring Weights ---
    Setting(
        key="scoring.weights.edge_size",
        name="Weight: Edge Size",
        description=(
            "How much weight to give the size of the edge (gap between our estimated\n"
            "probability and the market price). Bigger edges mean more potential profit.\n"
            "\n"
            "All five scoring weights must add up to 1.0. Increasing one means\n"
            "decreasing others."
        ),
        type="float",
        default=0.30,
        min_val=0.0,
        max_val=1.0,
    ),
    Setting(
        key="scoring.weights.signal_strength",
        name="Weight: Signal Strength",
        description=(
            "How much weight to give the number and quality of confirming signals.\n"
            "More independent sources agreeing on an outcome increases confidence.\n"
            "\n"
            "All five scoring weights must add up to 1.0."
        ),
        type="float",
        default=0.25,
        min_val=0.0,
        max_val=1.0,
    ),
    Setting(
        key="scoring.weights.liquidity_depth",
        name="Weight: Liquidity Depth",
        description=(
            "How much weight to give market liquidity — whether there are enough\n"
            "orders on the book for us to actually fill our trade at a good price.\n"
            "Thin markets can cause slippage (worse fills than expected).\n"
            "\n"
            "All five scoring weights must add up to 1.0."
        ),
        type="float",
        default=0.15,
        min_val=0.0,
        max_val=1.0,
    ),
    Setting(
        key="scoring.weights.settlement_clarity",
        name="Weight: Settlement Clarity",
        description=(
            "How much weight to give the clarity of the contract's settlement rules.\n"
            "Some Kalshi contracts have ambiguous resolution criteria or subjective\n"
            "carve-outs that could lead to unexpected outcomes. Higher weight means\n"
            "the system avoids ambiguous contracts more aggressively.\n"
            "\n"
            "All five scoring weights must add up to 1.0."
        ),
        type="float",
        default=0.15,
        min_val=0.0,
        max_val=1.0,
    ),
    Setting(
        key="scoring.weights.time_to_resolution",
        name="Weight: Time to Resolution",
        description=(
            "How much weight to give the time until the contract resolves. There's\n"
            "a sweet spot — too soon and we can't fill; too far out and capital is\n"
            "locked up. Higher weight means the system penalizes contracts outside\n"
            "the ideal time window more.\n"
            "\n"
            "All five scoring weights must add up to 1.0."
        ),
        type="float",
        default=0.15,
        min_val=0.0,
        max_val=1.0,
    ),
]

# Sections in walkthrough order, with display names and descriptions.
SECTIONS: list[tuple[str, str, str]] = [
    (
        "paper",
        "Paper Trading",
        "Settings for Driving Range mode — your practice environment with virtual money.",
    ),
    (
        "strategy",
        "Strategy",
        (
            "Core strategy parameters that control what the system considers a 'gimme'.\n"
            "These determine which markets qualify for trading."
        ),
    ),
    (
        "sizing",
        "Position Sizing",
        (
            "How much money to put into each trade. Uses the Kelly Criterion —\n"
            "a mathematical formula for optimal bet sizing — with conservative adjustments."
        ),
    ),
    (
        "risk",
        "Risk Management",
        (
            "Safety limits that protect your bankroll from large losses.\n"
            "These are hard stops that override everything else."
        ),
    ),
    (
        "orders",
        "Order Execution",
        "How the system places trades on Kalshi.",
    ),
    (
        "scanner",
        "Market Scanner",
        (
            "Filters that control which markets the Scout examines.\n"
            "These determine the initial pool of candidates before scoring."
        ),
    ),
    (
        "scoring",
        "Scoring Weights",
        (
            "How the Gimme Score is calculated. Each dimension gets a weight (0–1)\n"
            "and all five must add up to 1.0. Adjust these to shift the system's\n"
            "priorities — e.g., increase edge_size weight to favor high-edge trades\n"
            "even if liquidity is thinner."
        ),
    ),
]

SECTION_KEYS = [s[0] for s in SECTIONS]


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> tomlkit.TOMLDocument:
    """Load TOML file preserving comments and formatting."""
    if path.exists():
        return tomlkit.parse(path.read_text())
    return tomlkit.document()


def _get_nested(doc: tomlkit.TOMLDocument, dotted_key: str) -> object:
    """Get a value from a TOML document using a dotted key."""
    parts = dotted_key.split(".")
    current: object = doc
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _set_nested(doc: tomlkit.TOMLDocument, dotted_key: str, value: object) -> None:
    """Set a value in a TOML document using a dotted key, creating tables as needed."""
    parts = dotted_key.split(".")
    current: dict = doc  # type: ignore[assignment]
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        current = current[part]
    current[parts[-1]] = value


def _save_toml(doc: tomlkit.TOMLDocument, path: Path) -> None:
    """Write TOML document back to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def _format_current(value: object, setting: Setting) -> str:
    """Format a value for display."""
    if setting.type == "list" and isinstance(value, list):
        if len(value) > 6:
            return f"[{len(value)} items] {', '.join(str(v) for v in value[:6])}..."
        return ", ".join(str(v) for v in value)
    if setting.type == "float" and isinstance(value, float):
        if value < 1:
            return f"{value:.2f}"
        return f"{value:,.2f}"
    return str(value)


def _parse_input(raw: str, setting: Setting) -> int | float | str | list[str]:
    """Parse and validate user input for a setting. Raises ValueError on bad input."""
    raw = raw.strip()

    if setting.type == "int":
        val = int(raw)
        if setting.min_val is not None and val < setting.min_val:
            raise ValueError(f"Must be at least {int(setting.min_val)}")
        if setting.max_val is not None and val > setting.max_val:
            raise ValueError(f"Must be at most {int(setting.max_val)}")
        return val

    if setting.type == "float":
        val = float(raw)
        if setting.min_val is not None and val < setting.min_val:
            raise ValueError(f"Must be at least {setting.min_val}")
        if setting.max_val is not None and val > setting.max_val:
            raise ValueError(f"Must be at most {setting.max_val}")
        return val

    if setting.type == "str":
        if setting.choices and raw not in setting.choices:
            raise ValueError(f"Must be one of: {', '.join(setting.choices)}")
        return raw

    if setting.type == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]

    return raw


def _prompt_setting(setting: Setting, current_value: object) -> object:
    """Prompt the user for a single setting. Returns the new value or current if skipped."""
    display = _format_current(current_value, setting)

    console.print(f"\n  [bold]{setting.name}[/bold]")
    console.print(f"  Current value: [cyan]{display}[/cyan]")

    # Show description indented
    for line in setting.description.split("\n"):
        console.print(f"  [dim]{line}[/dim]")

    if setting.choices:
        console.print(f"  [dim]Options: {', '.join(setting.choices)}[/dim]")

    if setting.type == "list":
        hint = "comma-separated list, or Enter to keep"
    else:
        hint = "Enter to keep current"

    while True:
        raw = typer.prompt(f"  New value ({hint})", default="", show_default=False)
        if raw == "":
            return current_value
        try:
            return _parse_input(raw, setting)
        except ValueError as e:
            console.print(f"  [red]Invalid: {e}. Try again.[/red]")


def _validate_scoring_weights(doc: tomlkit.TOMLDocument) -> bool:
    """Check if scoring weights sum to 1.0 (within tolerance)."""
    weight_keys = [s.key for s in SETTINGS if s.key.startswith("scoring.weights.")]
    total = 0.0
    for key in weight_keys:
        val = _get_nested(doc, key)
        if isinstance(val, (int, float)):
            total += float(val)
    return abs(total - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


def run_config_wizard(section_filter: str | None = None) -> None:
    """Run the interactive config wizard."""
    if not TOML_FILE.exists():
        console.print(
            f"[red]Config file not found at {TOML_FILE}[/red]\n"
            "Run [bold]gimmes init[/bold] first to create it."
        )
        raise typer.Exit(1)

    doc = _load_toml(TOML_FILE)

    console.print("\n[bold cyan]GIMMES Configuration Wizard[/bold cyan]")
    console.print("[dim]Walk through each setting. Press Enter to keep the current value.[/dim]\n")

    if section_filter:
        if section_filter not in SECTION_KEYS:
            console.print(
                f"[red]Unknown section: {section_filter}[/red]\n"
                f"Valid sections: {', '.join(SECTION_KEYS)}"
            )
            raise typer.Exit(1)
        sections_to_show = [(k, n, d) for k, n, d in SECTIONS if k == section_filter]
    else:
        sections_to_show = SECTIONS

    changed = False

    for section_key, section_name, section_desc in sections_to_show:
        header = Text(f" {section_name} ", style="bold white on blue")
        console.print(Panel(header, subtitle=section_desc, expand=False))

        section_settings = [s for s in SETTINGS if s.key.startswith(f"{section_key}.")]

        for setting in section_settings:
            current = _get_nested(doc, setting.key)
            if current is None:
                current = setting.default

            new_value = _prompt_setting(setting, current)
            if new_value != current:
                _set_nested(doc, setting.key, new_value)
                changed = True
                console.print(f"  [green]Updated to: {_format_current(new_value, setting)}[/green]")

    # Validate scoring weights if any were touched
    scoring_touched = section_filter is None or section_filter == "scoring"
    if scoring_touched and not _validate_scoring_weights(doc):
        weight_keys = [s.key for s in SETTINGS if s.key.startswith("scoring.weights.")]
        total = sum(
            float(_get_nested(doc, k) or 0) for k in weight_keys
        )
        console.print(
            f"\n[yellow]Warning: Scoring weights sum to {total:.2f} instead of 1.00.[/yellow]\n"
            "The system will still work, but scores won't be properly normalized.\n"
            "Consider adjusting weights so they add up to 1.0."
        )

    if changed:
        _save_toml(doc, TOML_FILE)
        console.print(f"\n[bold green]Configuration saved to {TOML_FILE}[/bold green]")
    else:
        console.print("\n[dim]No changes made.[/dim]")
