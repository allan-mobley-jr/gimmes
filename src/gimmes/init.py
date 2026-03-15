"""gimmes init — interactive onboarding setup."""

from __future__ import annotations

import glob
import os
import stat
import sys
from pathlib import Path

import typer
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
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

# Password for encrypted private key (set automatically by gimmes init)
# KALSHI_PRIVATE_KEY_PASSWORD=
"""

_DEFAULT_TOML = """\
[strategy]
gimme_threshold = 75          # Minimum GimmeScore to execute (0-100)
min_market_price = 0.55       # Only scan markets above this price
max_market_price = 0.85       # Only scan markets below this price
min_true_probability = 0.90   # Model must see >=90% to qualify
min_edge_after_fees = 0.05    # 5pp minimum edge after fee math

[sizing]
kelly_fraction = 0.25         # Conservative quarter-Kelly
max_position_pct = 0.05       # Max 5% of bankroll per position

[risk]
max_open_positions = 15       # Concurrent position limit
daily_loss_limit_pct = 0.15   # Auto-stop at 15% daily drawdown
session_spending_cap = 500.00 # Max dollars committed per autonomous session

[orders]
preferred_order_type = "maker" # Limit orders; no takers by default

[scanner]
min_volume = 100              # Minimum 24h volume to consider
min_open_interest = 50        # Minimum open interest
max_days_to_resolution = 90   # Skip markets resolving too far out
min_days_to_resolution = 0.5  # Skip markets resolving too soon (12h)
# Curated series with informational edge — use `gimmes discover CAT` to find more
series = [
    # Inflation & CPI
    "KXCPI", "KXCPICORE", "KXCPIYOY", "KXCPICOREYOY",
    "KXECONSTATCPI", "KXECONSTATCPICORE", "KXECONSTATCPIYOY", "KXECONSTATCORECPIYOY",
    "KXPCECORE",
    # GDP & Growth
    "KXGDP", "KXGDPNOM", "KXGDPUSMAX",
    # Fed & Rates
    "KXFED", "KXFEDDECISION", "KXFEDCOMBO", "KXRATECUTCOUNT", "KXFEDCHGCOUNT",
    "KXFEDMEET", "KXEMERCUTS", "KXFEDDISSENT",
    # Employment
    "KXJOBLESSCLAIMS", "KXUE", "KXU3", "KXPAYROLLS", "KXADP",
    # Housing & Mortgage
    "KXMORTGAGERATE", "KXHOUSINGSTART", "KXEHSALES", "KXNHSALES",
    # Other Econ
    "KXISMPMI", "KXRECSSNBER", "KXEFFTARIFF", "KXTARIFFREVENUE",
    # Financials — S&P, Nasdaq, Treasuries
    "KXINX", "KXINXU", "KXINXMAXY", "KXINXMINY",
    "KXNASDAQ100", "KXNASDAQ100U", "KXNASDAQ100Y",
    "KXUSTYLD", "KXTNOTEW", "KX10Y2Y", "KX10Y3M", "KX3MTBILL",
    "KXGOLDW", "KXSILVERW", "KXWTI", "KXWTIMAX",
    # Politics — high-level
    "CONTROLH", "CONTROLS",
]

[paper]
starting_balance = 10000.00    # Virtual bankroll for driving range mode

[scoring.weights]
edge_size = 0.30              # Larger edge = higher score
signal_strength = 0.25        # More/stronger confirming signals
liquidity_depth = 0.15        # Can we actually fill?
settlement_clarity = 0.15     # Red flags penalize heavily
time_to_resolution = 0.15     # Sweet spot preferred
"""
KEYS_DIR = GIMMES_HOME / "keys"
PEM_FILENAME = "kalshi_private.pem"

_HEADLESS_REQUIRED_VARS = (
    "KALSHI_PROD_API_KEY",
    "KALSHI_PROD_PRIVATE_KEY_PATH",
    "KALSHI_PRIVATE_KEY_PASSWORD",
)


def _is_headless(flag: bool) -> bool:
    """Return True when init should run non-interactively."""
    if flag:
        return True
    return not sys.stdin.isatty()


def _secure_env_file() -> None:
    """Set .env file permissions to 0600 (owner read/write only)."""
    if ENV_FILE.exists():
        ENV_FILE.chmod(0o600)


def _write_default_file(
    target: Path, content: str, label: str, *, headless: bool = False
) -> bool:
    """Write default content to target file. Returns True if written."""
    if target.exists():
        if headless:
            console.print(f"[yellow]Overwriting existing {label} at {target}[/yellow]")
            overwrite = True
        else:
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


def _validate_pem_content(
    content: bytes, password: bytes | None = None
) -> bool:
    """Validate that the content is a valid RSA private key in PEM format."""
    try:
        key = serialization.load_pem_private_key(content, password=password)
        return isinstance(key, RSAPrivateKey)
    except TypeError:
        # Encrypted key, no password provided — structurally valid if header present
        if password is None and b"ENCRYPTED" in content:
            return True
        return False
    except (ValueError, UnsupportedAlgorithm):
        return False


def _encrypt_private_key(content: bytes, password: bytes) -> bytes:
    """Encrypt an unencrypted RSA private key PEM with a password."""
    key = serialization.load_pem_private_key(content, password=None)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    )


def _install_private_key(
    source: Path, password: bytes, *, headless: bool = False
) -> Path | None:
    """Validate, encrypt, and install the private key. Returns the PEM path or None."""
    content = source.read_bytes()

    if not _validate_pem_content(content):
        console.print(
            f"[red]The file {source.name} does not contain "
            "a valid RSA private key.[/red]"
        )
        console.print(
            "Make sure you downloaded the private key file "
            "from Kalshi, not the API key."
        )
        return None

    # Detect already-encrypted keys (e.g., user re-running init)
    if b"ENCRYPTED" in content:
        console.print(
            f"[red]The file {source.name} is already encrypted.[/red]\n"
            "Please use the original unencrypted key file "
            "downloaded from Kalshi."
        )
        return None

    # Encrypt the key before writing to disk
    try:
        encrypted = _encrypt_private_key(content, password)
    except Exception as e:
        console.print(f"[red]Failed to encrypt private key:[/red] {e}")
        return None

    KEYS_DIR.mkdir(exist_ok=True)
    pem_path = KEYS_DIR / PEM_FILENAME

    if pem_path.exists():
        if headless:
            overwrite = True
        else:
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
    pem_path.write_bytes(encrypted)
    pem_path.chmod(stat.S_IRUSR)

    console.print(f"[green]Private key encrypted and installed:[/green] {pem_path}")
    console.print("[dim]Permissions set to 0400 (owner read-only)[/dim]")
    return pem_path


def _update_env_var(
    var_name: str, value: str, *, sensitive: bool = False
) -> None:
    """Update or append a variable in the .env file."""
    if not ENV_FILE.exists():
        display = "****" if sensitive else value
        console.print(
            f"[yellow]Warning: .env not found — set {var_name}"
            f"={display} manually.[/yellow]"
        )
        return

    content = ENV_FILE.read_text()
    lines = content.splitlines()
    updated = False
    # Quote value to handle special chars (#, spaces)
    quoted = f'"{value}"' if sensitive else value

    for i, line in enumerate(lines):
        # Strip leading comment markers: "# ", "#", or just whitespace
        stripped = line.lstrip()
        if stripped.startswith("#"):
            stripped = stripped[1:].lstrip()
        if stripped.startswith(f"{var_name}=") or stripped.startswith(
            f"{var_name} ="
        ):
            lines[i] = f"{var_name}={quoted}"
            updated = True
            break

    if not updated:
        lines.append(f"{var_name}={quoted}")

    ENV_FILE.write_text("\n".join(lines) + "\n")
    _secure_env_file()


def _prompt_password() -> str:
    """Prompt the user to create a password for encrypting the private key."""
    console.print(
        "\n[bold]Private Key Encryption[/bold]\n"
        "\nYour private key will be encrypted at rest with a password."
        "\nChoose a strong password — it will be stored in your .env file"
        " (which is secured with 0600 permissions).\n"
    )
    while True:
        password = typer.prompt("Create a password for your private key", hide_input=True)
        if not password.strip():
            console.print("[red]Password cannot be empty.[/red]")
            continue
        confirm = typer.prompt("Confirm password", hide_input=True)
        if password != confirm:
            console.print("[red]Passwords do not match. Try again.[/red]")
            continue
        return password


def _prompt_api_key() -> str | None:
    """Prompt the user to paste their API key."""
    console.print("\n[bold]API Key[/bold]\n")
    api_key = typer.prompt("Paste your Kalshi API key", hide_input=True)
    if not api_key.strip():
        console.print("[red]No API key provided.[/red]")
        return None
    return api_key.strip()


def _clear_shell_history(*, headless: bool = False) -> None:
    """Clear shell history to remove any pasted secrets.

    Prompts the user before truncating history files on disk.
    Note: typer.prompt(hide_input=True) prevents most shells from
    recording the pasted values, but this provides defense in depth.
    In headless mode, no secrets are pasted so history clearing is skipped.
    """
    if headless:
        return

    shell = os.environ.get("SHELL", "")
    home = Path.home()
    history_files: list[Path] = []

    if "zsh" in shell:
        history_files.append(home / ".zsh_history")
    elif "bash" in shell:
        history_files.append(home / ".bash_history")
    else:
        history_files.extend([home / ".bash_history", home / ".zsh_history"])

    # Only target files that actually exist
    targets = [hf for hf in history_files if hf.exists()]
    if not targets:
        return

    console.print(
        "\n[bold]Shell History[/bold]\n"
        "\nAs a security precaution, shell history can be cleared "
        "to ensure no credentials remain on disk."
    )
    if not typer.confirm("Clear shell history?", default=True):
        console.print(
            "[dim]Skipped. You can clear history manually later.[/dim]"
        )
        return

    cleared: list[str] = []
    failed: list[str] = []
    for hf in targets:
        try:
            with open(hf, "w") as f:
                f.truncate(0)
            cleared.append(str(hf))
        except OSError:
            failed.append(str(hf))

    if cleared:
        console.print(
            "\n[yellow]Shell history cleared:[/yellow] "
            + ", ".join(cleared)
        )
        console.print(
            "[dim]Start a new shell session to ensure in-memory "
            "history is also cleared.[/dim]"
        )
    if failed:
        console.print(
            "\n[red]Failed to clear:[/red] " + ", ".join(failed)
        )
        console.print(
            "Clear these files manually to remove "
            "pasted credentials."
        )


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


def run_init(*, headless: bool = False) -> None:
    """Run the init flow. Interactive by default; headless when flag or no TTY."""
    import asyncio

    headless = _is_headless(headless)

    if headless:
        # Validate all required env vars are present
        env_vals: dict[str, str] = {}
        missing: list[str] = []
        for var in _HEADLESS_REQUIRED_VARS:
            val = os.environ.get(var, "").strip()
            if not val:
                missing.append(var)
            else:
                env_vals[var] = val
        if missing:
            console.print(
                f"[red]Headless init requires these env vars: "
                f"{', '.join(missing)}[/red]"
            )
            raise typer.Exit(1)

    console.print("\n[bold cyan]GIMMES Setup[/bold cyan]")
    if headless:
        console.print("[dim](headless mode)[/dim]")
    console.print()

    # Step 1: Create default config files
    console.print("[bold]Step 1: Configuration files[/bold]\n")
    _write_default_file(ENV_FILE, _DEFAULT_ENV, ".env", headless=headless)
    _write_default_file(TOML_FILE, _DEFAULT_TOML, "config/gimmes.toml", headless=headless)

    if not headless:
        # --- Interactive path ---

        # Step 2: Credential readiness gate
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

        # Step 3: Locate and install the private key
        console.print("\n[bold]Step 3: Private key[/bold]\n")
        console.print("[cyan]Searching for private key in ~/Downloads...[/cyan]")
        key_path = _find_downloaded_key()

        if key_path:
            console.print(f"[green]Found:[/green] {key_path}")
            password = _prompt_password()
            pem_path = _install_private_key(key_path, password.encode())
            if pem_path:
                _update_env_var("KALSHI_PROD_PRIVATE_KEY_PATH", str(pem_path))
                _update_env_var(
                    "KALSHI_PRIVATE_KEY_PASSWORD", password, sensitive=True
                )
                console.print(
                    "[green]Updated .env:[/green] KALSHI_PRIVATE_KEY_PASSWORD set"
                )
                console.print(
                    f"\n[yellow bold]Security reminder:[/yellow bold] "
                    f"The original unencrypted key file is still at:\n"
                    f"  {key_path}\n"
                    f"Delete it now that the encrypted copy is installed."
                )
        else:
            console.print(
                "[yellow]Could not find a file matching gimmes*.txt in ~/Downloads.[/yellow]\n"
                "You can:\n"
                "  • Rename your downloaded key file to [bold]gimmes.txt[/bold] and run"
                " [bold]gimmes init[/bold] again\n"
                "  • Or manually copy the key file and set"
                " KALSHI_PROD_PRIVATE_KEY_PATH in .env\n"
            )

        # Step 4: API key
        console.print("\n[bold]Step 4: API key[/bold]")
        api_key = _prompt_api_key()
        if api_key:
            _update_env_var("KALSHI_PROD_API_KEY", api_key, sensitive=True)
            console.print("[green]Updated .env:[/green] KALSHI_PROD_API_KEY set")
    else:
        # --- Headless path ---
        api_key = env_vals["KALSHI_PROD_API_KEY"]
        key_path_str = env_vals["KALSHI_PROD_PRIVATE_KEY_PATH"]
        password = env_vals["KALSHI_PRIVATE_KEY_PASSWORD"]

        # Step 2: Install private key from env var path
        console.print("\n[bold]Step 2: Private key (from env)[/bold]\n")
        source = Path(key_path_str).expanduser()
        if not source.is_file():
            console.print(f"[red]Private key file not found: {source}[/red]")
            raise typer.Exit(1)

        pem_path = _install_private_key(source, password.encode(), headless=True)
        if not pem_path:
            raise typer.Exit(1)

        _update_env_var("KALSHI_PROD_PRIVATE_KEY_PATH", str(pem_path))
        _update_env_var("KALSHI_PRIVATE_KEY_PASSWORD", password, sensitive=True)
        console.print("[green]Updated .env:[/green] private key configured")

        # Step 3: API key from env var
        console.print("\n[bold]Step 3: API key (from env)[/bold]")
        _update_env_var("KALSHI_PROD_API_KEY", api_key, sensitive=True)
        console.print("[green]Updated .env:[/green] KALSHI_PROD_API_KEY set")

    # Verify connection (same in both modes)
    step_num = 5 if not headless else 4
    console.print(f"\n[bold]Step {step_num}: Verify connection[/bold]\n")
    connected = asyncio.run(_verify_connection())
    if headless and not connected:
        console.print(
            "[red]Connection verification failed. "
            "Check your credentials and retry.[/red]"
        )
        raise typer.Exit(1)

    # Clear shell history (skipped in headless — no secrets pasted)
    _clear_shell_history(headless=headless)

    # Done
    console.print(
        "\n[bold green]Setup complete.[/bold green]\n"
        "\nNext steps:\n"
        "  • Run [bold]gimmes mode[/bold] to check your connection status\n"
        "  • Run [bold]gimmes scan[/bold] to find your first gimme candidates\n"
        "  • Run [bold]gimmes config[/bold] to tune strategy parameters\n"
    )
