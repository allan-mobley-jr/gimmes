#!/usr/bin/env bash
set -euo pipefail

GIMMES_HOME="${GIMMES_HOME:-$HOME/.gimmes}"
REPO="$GIMMES_HOME/repo"
PYTHON="$REPO/.venv/bin/python"

gimmes_version() {
    git -C "$REPO" describe --tags 2>/dev/null || git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "dev"
}

show_banner() {
    local variant="${1:-main}"
    local version
    version=$(gimmes_version)

    local green='\033[0;32m'
    local cyan='\033[0;36m'
    local yellow='\033[0;33m'
    local reset='\033[0m'

    printf "${green}"
    cat <<'BANNER'

  ██████╗ ██╗███╗   ███╗███╗   ███╗███████╗███████╗
 ██╔════╝ ██║████╗ ████║████╗ ████║██╔════╝██╔════╝
 ██║  ███╗██║██╔████╔██║██╔████╔██║█████╗  ███████╗
 ██║   ██║██║██║╚██╔╝██║██║╚██╔╝██║██╔══╝  ╚════██║
 ╚██████╔╝██║██║ ╚═╝ ██║██║ ╚═╝ ██║███████╗███████║
  ╚═════╝ ╚═╝╚═╝     ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝
BANNER
    printf "${reset}"

    case "$variant" in
        driving_range)
            printf "\n  ${cyan}DRIVING RANGE${reset}  Paper trading — real data, simulated orders\n"
            ;;
        championship)
            printf "\n  ${yellow}CHAMPIONSHIP${reset}  Real money. Real markets. No mulligans.\n"
            ;;
        *)
            printf "\n  We only play the gimmes.\n"
            ;;
    esac

    printf "  %-47s %s\n" "MIT License" "$version"
    echo ""
}

# Verify installation
if [ ! -d "$REPO" ]; then
    echo "Error: gimmes is not installed. Run the install script first:"
    echo "  curl -fsSL https://raw.githubusercontent.com/allan-mobley-jr/gimmes/main/install.sh | bash"
    exit 1
fi

if [ ! -f "$PYTHON" ]; then
    echo "Error: Python virtual environment not found at $REPO/.venv"
    echo "Try running: gimmes update"
    exit 1
fi

case "${1:-}" in
    update)
        echo "Updating gimmes..."
        cd "$REPO"
        git pull --ff-only origin main
        if command -v uv &>/dev/null; then
            uv sync --quiet
        else
            "$PYTHON" -m pip install -e . --quiet
        fi
        show_banner
        echo "Updated to $(git rev-parse --short HEAD)"
        ;;
    help)
        show_banner
        cat <<'HELP'
Setup:
  gimmes init              First-time setup (API credentials, config)
  gimmes config            Interactive configuration wizard
  gimmes mode              Show current mode and connection status
  gimmes tour_guide        Interactive product tour (The Starter)
  gimmes update            Pull latest code and reinstall

Market Research:
  gimmes discover CAT      Explore series in a Kalshi category
  gimmes scan              Scan markets for gimme candidates
  gimmes score TICKER      Score a specific market
  gimmes market-info TICKER  Detailed market info + orderbook

Trading:
  gimmes size TICKER -p P  Calculate position size
  gimmes validate TICKER -p P  Pre-trade validation
  gimmes order TICKER      Place an order
  gimmes cancel ORDER_ID   Cancel a resting order
  gimmes log-trade TICKER  Log a trade decision
  gimmes trades            List trade records (--ticker, --action)
  gimmes log-outcome TICKER  Record a market resolution (--outcome yes/no)

Portfolio:
  gimmes positions         List open positions
  gimmes reconcile         Sync positions with broker/API
  gimmes risk-check        Check risk limits and daily P&L
  gimmes report            Performance scorecard

Diagnostics:
  gimmes log-activity      Log agent activity to the database
  gimmes log-error         Log a structured error
  gimmes errors            View error logs (--severity, --category, --unresolved)
  gimmes resolve-error ID  Mark an error as resolved

Strategy:
  gimmes lesson            Run strategy analysis and recommendations
  gimmes recommendations   View past strategy recommendations
  gimmes tune              Apply pending recommendations to gimmes.toml

Dashboard:
  gimmes clubhouse         Launch web dashboard (http://127.0.0.1:1919)

Autonomous:
  gimmes driving_range     Autonomous loop -- paper trading (auto-starts dashboard)
  gimmes championship      Autonomous loop -- real money (auto-starts dashboard)

https://github.com/allan-mobley-jr/gimmes
HELP
        ;;
    init)
        show_banner
        shift
        exec "$PYTHON" -m gimmes init "$@"
        ;;
    driving_range)
        show_banner driving_range
        shift
        exec "$PYTHON" -m gimmes driving_range "$@"
        ;;
    championship)
        show_banner championship
        shift
        exec "$PYTHON" -m gimmes championship "$@"
        ;;
    "")
        exec "$PYTHON" -m gimmes --help
        ;;
    *)
        exec "$PYTHON" -m gimmes "$@"
        ;;
esac
