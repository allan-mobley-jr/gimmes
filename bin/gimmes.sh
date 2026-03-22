#!/usr/bin/env bash
set -euo pipefail

GIMMES_HOME="${GIMMES_HOME:-$HOME/.gimmes}"
REPO="$GIMMES_HOME/repo"
PYTHON="$REPO/.venv/bin/python"

gimmes_version() {
    git -C "$REPO" describe --tags 2>/dev/null || git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "dev"
}

# Get the latest semver tag from the remote (after fetch).
# Returns empty string if no tags exist.
latest_remote_tag() {
    git -C "$REPO" tag -l 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -1
}

# Compare two semver strings (vMAJOR.MINOR.PATCH format).
# Exit code: 0 = equal, 1 = first > second, 2 = first < second
semver_compare() {
    local a="${1#v}" b="${2#v}"
    local a_major a_minor a_patch b_major b_minor b_patch

    IFS='.' read -r a_major a_minor a_patch <<< "$a"
    IFS='.' read -r b_major b_minor b_patch <<< "$b"

    # Strip pre-release suffixes (e.g. 1.2.3-rc1 -> 1.2.3), default missing parts to 0
    a_major="${a_major:-0}"; a_minor="${a_minor:-0}"; a_patch="${a_patch%%-*}"; a_patch="${a_patch:-0}"
    b_major="${b_major:-0}"; b_minor="${b_minor:-0}"; b_patch="${b_patch%%-*}"; b_patch="${b_patch:-0}"

    if [ "$a_major" -lt "$b_major" ]; then return 2; fi
    if [ "$a_major" -gt "$b_major" ]; then return 1; fi
    if [ "$a_minor" -lt "$b_minor" ]; then return 2; fi
    if [ "$a_minor" -gt "$b_minor" ]; then return 1; fi
    if [ "$a_patch" -lt "$b_patch" ]; then return 2; fi
    if [ "$a_patch" -gt "$b_patch" ]; then return 1; fi
    return 0
}

suggest_update() {
    local yellow='\033[0;33m' reset='\033[0m'
    printf "${yellow}⚠ Update available — %s${reset}\n" "$1"
    echo "  Run: gimmes update"
}

# Release mode: compare local tag against latest remote tag
check_update_release() {
    local tag="$1" latest_tag="$2"
    local green='\033[0;32m' reset='\033[0m'

    # Local version is not semver (e.g. "dev") — always suggest update
    if [[ ! "$tag" =~ ^v?[0-9]+\.[0-9]+ ]]; then
        suggest_update "$latest_tag"
        return
    fi

    local rc
    semver_compare "$tag" "$latest_tag" && rc=0 || rc=$?
    if [ "$rc" -eq 2 ]; then
        suggest_update "$tag → $latest_tag"
    else
        printf "${green}✓ Up to date${reset}\n"
    fi
}

# Dev mode: compare commit counts when no remote tags exist
check_update_dev() {
    local green='\033[0;32m' reset='\033[0m'
    local behind
    behind=$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo "0")

    if [ "$behind" -eq 0 ]; then
        printf "${green}✓ Up to date${reset}\n"
        return
    fi

    local label="commit"
    [ "$behind" -gt 1 ] && label="commits"
    suggest_update "remote is $behind $label ahead"
}

show_version() {
    local tag sha
    local dim='\033[0;90m' reset='\033[0m'

    sha=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")

    # Try git tag first, fall back to pyproject.toml version
    tag=$(git -C "$REPO" describe --tags --abbrev=0 2>/dev/null || true)
    if [ -z "$tag" ]; then
        tag=$(sed -n 's/^version = "\(.*\)"/v\1/p' "$REPO/pyproject.toml" 2>/dev/null)
    fi
    tag="${tag:-dev}"

    echo "gimmes $tag ($sha)"

    # Update check: fetch quietly, then compare tags or commits
    if ! git -C "$REPO" fetch origin main --tags --quiet 2>/dev/null; then
        printf "${dim}(update check skipped — could not reach remote)${reset}\n"
        return
    fi

    local latest_tag
    latest_tag=$(latest_remote_tag)

    if [ -n "$latest_tag" ]; then
        check_update_release "$tag" "$latest_tag"
    else
        check_update_dev
    fi
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

if [ ! -f "$PYTHON" ] && [ "${1:-}" != "_post-update" ]; then
    echo "Error: Python virtual environment not found at $REPO/.venv"
    echo "Try running: gimmes update"
    exit 1
fi

case "${1:-}" in
    version|--version)
        show_version
        ;;
    update)
        echo "Updating gimmes..."
        cd "$REPO"
        if ! git fetch origin main --tags --quiet 2>/dev/null; then
            echo "Error: could not reach the remote repository."
            echo "Check your network connection and try again."
            exit 1
        fi

        if ! git checkout . --quiet 2>/dev/null; then
            echo "Warning: could not clean local changes; update may fail."
        fi

        latest_tag=$(latest_remote_tag)

        if [ -n "$latest_tag" ]; then
            # Release mode: short-circuit if HEAD is already at the latest tag
            current_tag=$(git describe --tags --exact-match HEAD 2>/dev/null || true)
            if [ "$current_tag" = "$latest_tag" ]; then
                echo "Already on latest version $latest_tag"
                exit 0
            fi
            if ! git checkout "$latest_tag" --quiet 2>/dev/null; then
                echo "Error: could not checkout $latest_tag."
                echo "Try: cd $REPO && git status"
                exit 1
            fi
            update_label="$latest_tag"
        else
            # Dev mode: short-circuit if HEAD already matches origin/main
            local_head=$(git rev-parse HEAD 2>/dev/null) || {
                echo "Error: could not determine current HEAD."
                echo "Try: cd $REPO && git status"
                exit 1
            }
            remote_head=$(git rev-parse origin/main 2>/dev/null) || {
                echo "Error: could not determine origin/main."
                echo "Try: cd $REPO && git fetch origin main"
                exit 1
            }
            if [ "$local_head" = "$remote_head" ]; then
                echo "Already on latest version (${local_head:0:7})"
                exit 0
            fi
            if ! git pull --ff-only origin main; then
                echo "Error: could not fast-forward to latest main."
                echo "Try: cd $REPO && git status"
                exit 1
            fi
            update_label=$(git rev-parse --short HEAD)
        fi

        # Re-exec into the updated script for post-update steps.
        # This ensures uv sync and banner use the NEW code.
        exec "$REPO/bin/gimmes.sh" _post-update "$update_label"
        ;;
    _post-update)
        update_label="${2:-unknown}"
        cd "$REPO"
        if command -v uv &>/dev/null; then
            uv sync --quiet
        else
            "$PYTHON" -m pip install -e . --quiet
        fi
        show_banner
        echo "Updated to $update_label"
        ;;
    help)
        show_banner
        cat <<'HELP'
Setup & Config:
  gimmes init              First-time setup (API credentials, config)
  gimmes init --headless   Non-interactive setup (requires env vars)
  gimmes config            Interactive configuration wizard
  gimmes config set K V    Set a single config value directly
  gimmes config get [K]    Show config value(s)
  gimmes tour_guide        Interactive product tour (The Starter)
  gimmes caddie_shop       Conversational config advisor (The Caddie Shop)
  gimmes update            Pull latest code and reinstall
  gimmes version           Show version and check for updates

Mode & Status:
  gimmes mode              Show current mode and connection status
  gimmes switch [MODE]     Switch trading mode (omit MODE to toggle)

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
  gimmes trades            List trade records (--ticker, --action)
  gimmes candidates        List scored candidates (--ticker)

Portfolio:
  gimmes positions         List open positions
  gimmes reconcile         Sync positions with broker/API
  gimmes risk-check        Check risk limits and daily P&L
  gimmes report            Performance scorecard
  gimmes position-context TICKER  Full thesis + note history for a position
  gimmes position-notes TICKER    Position journal entries

Diagnostics:
  gimmes errors            View error logs (--severity, --category, --unresolved)

Strategy:
  gimmes lesson            Run strategy analysis and recommendations
  gimmes recommendations   View past strategy recommendations
  gimmes tune              Apply pending strategy recommendations

Dashboard:
  gimmes clubhouse         Launch web dashboard (http://127.0.0.1:1919)

Autonomous Trading:
  gimmes start             Autonomous loop using current mode from .env
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
