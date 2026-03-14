"""gimmes init — interactive onboarding setup."""

from __future__ import annotations

import glob
import os
import stat
from pathlib import Path

import typer
from cryptography.hazmat.primitives import serialization
from rich.console import Console

from gimmes.config import GIMMES_HOME

console = Console()

# User files live in GIMMES_HOME (~/.gimmes/ by default)
ENV_FILE = GIMMES_HOME / ".env"
TOML_FILE = GIMMES_HOME / "config" / "gimmes.toml"

# Default content generated inline (no dependency on repo example files)
_DEFAULT_ENV = """\
# GIMMES Configuration

# Mode: driving_range (paper trading) or championship (real money)
GIMMES_MODE=driving_range

# Kalshi Production API credentials (used in both modes)
# Driving range reads real market data but simulates orders locally
KALSHI_PROD_API_KEY=your-prod-api-key-uuid
KALSHI_PROD_PRIVATE_KEY_PATH=~/.gimmes/keys/kalshi_private.pem
"""

_DEFAULT_TOML = """\
[strategy]
gimme_threshold = 75
min_market_price = 0.55
max_market_price = 0.85
min_true_probability = 0.90
min_edge_after_fees = 0.05

[sizing]
kelly_fraction = 0.25
max_position_pct = 0.05

[risk]
max_open_positions = 15
daily_loss_limit_pct = 0.15

[orders]
preferred_order_type = "maker"

[scanner]
min_volume = 100
min_open_interest = 50
max_days_to_resolution = 90
min_days_to_resolution = 0.5

[paper]
starting_balance = 10000.00

[scoring.weights]
edge_size = 0.30
signal_strength = 0.25
liquidity_depth = 0.15
settlement_clarity = 0.15
time_to_resolution = 0.15
"""
KEYS_DIR = GIMMES_HOME / "keys"
PEM_FILENAME = "kalshi_private.pem"


def _secure_env_file() -> None:
    """Set .env file permissions to 0600 (owner read/write only)."""
    if ENV_FILE.exists():
        ENV_FILE.chmod(0o600)


def _write_default_file(
    target: Path, content: str, label: str
) -> bool:
    """Write default content to target file. Returns True if written."""
    if target.exists():
        overwrite = typer.confirm(
            f"{label} already exists at {target}. Overwrite?",
            default=False,
        )
        if not overwrite:
            console.print(f"[dim]Skipping {label}[/dim]")
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if target == ENV_FILE:
        _secure_env_file()
    console.print(f"[green]Created {label}:[/green] {target}")
    return True


def _find_downloaded_key() -> Path | None:
    """Search ~/Downloads for a Kalshi private key file named gimmes/Gimmes."""
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        return None

    # Search for gimmes*.txt across common casings and merge all matches
    patterns = [
        str(downloads / "gimmes*.txt"),
        str(downloads / "Gimmes*.txt"),
        str(downloads / "GIMMES*.txt"),
    ]
    all_matches: list[str] = []
    for pattern in patterns:
        all_matches.extend(glob.glob(pattern))

    if not all_matches:
        return None

    # Deduplicate (macOS case-insensitive FS may return the same file for multiple patterns)
    unique = list(dict.fromkeys(all_matches))
    unique.sort(key=os.path.getmtime, reverse=True)
    return Path(unique[0])


def _validate_pem_content(content: bytes) -> bool:
    """Validate that the content is a valid RSA private key in PEM format."""
    try:
        key = serialization.load_pem_private_key(content, password=None)
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

        return isinstance(key, RSAPrivateKey)
    except Exception:
        return False


def _install_private_key(source: Path) -> Path | None:
    """Validate, copy, and secure the private key. Returns the PEM path or None."""
    content = source.read_bytes()

    if not _validate_pem_content(content):
        console.print(
            f"[red]The file {source.name} does not contain a valid RSA private key.[/red]"
        )
        console.print("Make sure you downloaded the private key file from Kalshi, not the API key.")
        return None

    KEYS_DIR.mkdir(exist_ok=True)
    pem_path = KEYS_DIR / PEM_FILENAME

    if pem_path.exists():
        overwrite = typer.confirm(
            f"Private key already exists at {pem_path}. Overwrite?", default=False
        )
        if not overwrite:
            console.print("[dim]Keeping existing private key[/dim]")
            return pem_path

    # Ensure writable before writing (previous install leaves file at 0400)
    if pem_path.exists():
        pem_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    pem_path.touch(mode=0o600, exist_ok=True)
    pem_path.write_bytes(content)
    pem_path.chmod(stat.S_IRUSR)

    console.print(f"[green]Private key installed:[/green] {pem_path}")
    console.print("[dim]Permissions set to 0400 (owner read-only)[/dim]")
    return pem_path


def _update_env_key_path(pem_path: Path) -> None:
    """Update KALSHI_PROD_PRIVATE_KEY_PATH in the .env file."""
    if not ENV_FILE.exists():
        console.print(
            "[yellow]Warning: .env not found — set KALSHI_PROD_PRIVATE_KEY_PATH"
            f"={pem_path} manually.[/yellow]"
        )
        return

    content = ENV_FILE.read_text()
    lines = content.splitlines()
    updated = False

    for i, line in enumerate(lines):
        if line.startswith("KALSHI_PROD_PRIVATE_KEY_PATH"):
            lines[i] = f"KALSHI_PROD_PRIVATE_KEY_PATH={pem_path}"
            updated = True
            break

    if not updated:
        lines.append(f"KALSHI_PROD_PRIVATE_KEY_PATH={pem_path}")

    ENV_FILE.write_text("\n".join(lines) + "\n")
    _secure_env_file()
    console.print(f"[green]Updated .env:[/green] KALSHI_PROD_PRIVATE_KEY_PATH={pem_path}")


def _offer_api_key_paste() -> None:
    """Offer the user the option to paste their API key or edit .env manually."""
    console.print(
        "\n[bold]API Key Setup[/bold]"
        "\n\nYou also need to set your API key in the .env file."
        "\nOpen .env in your editor and paste your API key as the value for KALSHI_PROD_API_KEY."
    )

    paste_now = typer.confirm(
        "\nAlternatively, paste it here now? (input will be hidden)",
        default=False,
    )

    if paste_now:
        if not ENV_FILE.exists():
            console.print("[red].env not found — run gimmes init again to create it first.[/red]")
            return
        api_key = typer.prompt("Paste your Kalshi API key", hide_input=True)
        if not api_key.strip():
            console.print("[red]No API key provided. You can set it in .env later.[/red]")
            return

        content = ENV_FILE.read_text()
        lines = content.splitlines()
        updated = False

        for i, line in enumerate(lines):
            if line.startswith("KALSHI_PROD_API_KEY"):
                lines[i] = f"KALSHI_PROD_API_KEY={api_key.strip()}"
                updated = True
                break

        if not updated:
            lines.append(f"KALSHI_PROD_API_KEY={api_key.strip()}")

        ENV_FILE.write_text("\n".join(lines) + "\n")
        _secure_env_file()
        console.print("[green]Updated .env:[/green] KALSHI_PROD_API_KEY set")


async def _verify_connection() -> bool:
    """Run a quick API health check to verify credentials."""
    from dotenv import load_dotenv

    from gimmes.config import load_config

    # Re-read .env since we may have just written new credentials
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    config = load_config()

    if not config.api_key or config.api_key == "your-prod-api-key-uuid":
        console.print("[yellow]API key not set yet — skipping connection check.[/yellow]")
        return False

    if not config.private_key_path.exists():
        console.print("[yellow]Private key not found — skipping connection check.[/yellow]")
        return False

    try:
        from gimmes.kalshi.client import KalshiClient

        async with KalshiClient(config) as client:
            # Hit a lightweight endpoint to verify auth works
            await client.get("/exchange/status")
        console.print("[green bold]Connection verified — credentials are working.[/green bold]")
        return True
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        console.print("Check your API key and private key, then try [bold]gimmes mode[/bold].")
        return False


def run_init() -> None:
    """Run the full interactive init flow."""
    console.print("\n[bold cyan]GIMMES Setup[/bold cyan]\n")

    # Step 1: Create default config files
    console.print("[bold]Step 1: Configuration files[/bold]\n")

    _write_default_file(ENV_FILE, _DEFAULT_ENV, ".env")
    _write_default_file(TOML_FILE, _DEFAULT_TOML, "config/gimmes.toml")

    # Step 2: Private key setup
    console.print("\n[bold]Step 2: Kalshi API credentials[/bold]\n")
    console.print(
        "To trade on Kalshi, you need an API key and a private key.\n"
        "\n"
        "[bold]Here's how to get them:[/bold]\n"
        "  1. Log in to your Kalshi account at [cyan]https://kalshi.com[/cyan]\n"
        "  2. Go to [bold]Account Settings → API Keys[/bold]\n"
        "  3. Click [bold]Create API Key[/bold] (select read/write access)\n"
        "  4. Kalshi will generate two things:\n"
        "     • An [bold]API key[/bold] (a UUID displayed on screen)\n"
        "     • A [bold]private key[/bold] (a .txt file that downloads automatically)\n"
        "  5. [bold yellow]Important:[/bold yellow] Name the downloaded file"
        " [bold]Gimmes[/bold] or [bold]gimmes[/bold]\n"
        "     so this tool can find it in your Downloads folder.\n"
    )

    ready = typer.confirm("Have you created the API key and downloaded the private key?")
    if not ready:
        console.print(
            "\n[dim]No problem. Run [bold]gimmes init[/bold] again when you're ready.[/dim]"
        )
        raise typer.Exit(0)

    # Search for the downloaded key
    console.print("\n[cyan]Searching for private key in ~/Downloads...[/cyan]")
    key_path = _find_downloaded_key()

    if key_path:
        console.print(f"[green]Found:[/green] {key_path}")
        pem_path = _install_private_key(key_path)
        if pem_path:
            _update_env_key_path(pem_path)
    else:
        console.print(
            "[yellow]Could not find a file matching gimmes*.txt in ~/Downloads.[/yellow]\n"
            "You can:\n"
            "  • Rename your downloaded key file to [bold]gimmes.txt[/bold] and run"
            " [bold]gimmes init[/bold] again\n"
            "  • Or manually copy the key file and set"
            " KALSHI_PROD_PRIVATE_KEY_PATH in .env\n"
        )

    # Step 3: API key
    console.print("\n[bold]Step 3: API key[/bold]")
    _offer_api_key_paste()

    # Step 4: Verify connection
    console.print("\n[bold]Step 4: Verify connection[/bold]\n")

    import asyncio

    asyncio.run(_verify_connection())

    # Done
    console.print(
        "\n[bold green]Setup complete.[/bold green]\n"
        "\nNext steps:\n"
        "  • Run [bold]gimmes mode[/bold] to check your connection status\n"
        "  • Run [bold]gimmes scan[/bold] to find your first gimme candidates\n"
        "  • Run [bold]gimmes config[/bold] to tune strategy parameters\n"
    )
