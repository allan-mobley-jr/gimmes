#!/usr/bin/env bash
set -euo pipefail

GIMMES_HOME="${GIMMES_HOME:-$HOME/.gimmes}"
REPO="$GIMMES_HOME/repo"
PYTHON="$REPO/.venv/bin/python"

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
        git fetch origin main
        git reset --hard origin/main
        if command -v uv &>/dev/null; then
            uv sync --quiet
        else
            "$PYTHON" -m pip install -e . --quiet
        fi
        echo "Updated to $(git rev-parse --short HEAD)"
        ;;
    help|--help|-h)
        cat <<'HELP'
gimmes -- We only play the gimmes.

Setup:
  gimmes init              First-time setup (API credentials, config)
  gimmes config            Interactive configuration wizard
  gimmes mode              Show current mode and connection status
  gimmes update            Pull latest code and reinstall

Market Research:
  gimmes discover CAT      Explore series in a Kalshi category
  gimmes scan              Scan markets for gimme candidates
  gimmes score TICKER      Score a specific market
  gimmes market-info TICK  Detailed market info + orderbook

Trading:
  gimmes size TICKER -p P  Calculate position size
  gimmes validate TICK -p P  Pre-trade validation
  gimmes order TICKER      Place an order
  gimmes cancel ORDER_ID   Cancel a resting order
  gimmes log-trade TICKER  Log a trade decision

Portfolio:
  gimmes positions         List open positions
  gimmes risk-check        Check risk limits and daily P&L
  gimmes report            Performance scorecard

Autonomous:
  gimmes driving_range     Autonomous loop -- paper trading
  gimmes championship      Autonomous loop -- real money

https://github.com/allan-mobley-jr/gimmes
HELP
        ;;
    "")
        exec "$PYTHON" -m gimmes --help
        ;;
    *)
        exec "$PYTHON" -m gimmes "$@"
        ;;
esac
