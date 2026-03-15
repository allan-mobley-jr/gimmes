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
    help)
        cat <<'HELP'
gimmes -- We only play the gimmes.

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
    "")
        exec "$PYTHON" -m gimmes --help
        ;;
    *)
        exec "$PYTHON" -m gimmes "$@"
        ;;
esac
