#!/usr/bin/env bash
#
# GIMMES installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/allan-mobley-jr/gimmes/main/install.sh | bash
#
set -euo pipefail

GIMMES_HOME="${GIMMES_HOME:-$HOME/.gimmes}"
REPO_URL="https://github.com/allan-mobley-jr/gimmes.git"
REPO_DIR="$GIMMES_HOME/repo"
BIN_DIR="$GIMMES_HOME/bin"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }
err()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }

check_command() {
    if ! command -v "$1" &>/dev/null; then
        return 1
    fi
    return 0
}

show_banner() {
    local version
    version=$(git -C "$REPO_DIR" describe --tags 2>/dev/null || git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo "dev")

    printf '\033[0;32m'
    cat <<'BANNER'

  ██████╗ ██╗███╗   ███╗███╗   ███╗███████╗███████╗
 ██╔════╝ ██║████╗ ████║████╗ ████║██╔════╝██╔════╝
 ██║  ███╗██║██╔████╔██║██╔████╔██║█████╗  ███████╗
 ██║   ██║██║██║╚██╔╝██║██║╚██╔╝██║██╔══╝  ╚════██║
 ╚██████╔╝██║██║ ╚═╝ ██║██║ ╚═╝ ██║███████╗███████║
  ╚═════╝ ╚═╝╚═╝     ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝
BANNER
    printf '\033[0m'
    printf "\n  We only play the gimmes.\n"
    printf "  %-47s %s\n\n" "MIT License" "$version"
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

info "Checking prerequisites..."

# Git
if ! check_command git; then
    err "Git is required but not installed."
    if [ "$(uname)" = "Darwin" ]; then
        err "Install Xcode Command Line Tools: xcode-select --install"
    else
        err "Install git using your system package manager."
    fi
    exit 1
fi
ok "  git: $(git --version | head -1)"

# Python 3.11+
PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    if check_command "$cmd"; then
        version=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    err "Python 3.11+ is required but not found."
    err "Install Python from https://python.org or via your package manager."
    exit 1
fi
ok "  python: $($PYTHON_CMD --version)"

# uv (install if missing)
if ! check_command uv; then
    info "  uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! check_command uv; then
        err "Failed to install uv. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
fi
ok "  uv: $(uv --version)"

# Claude CLI (optional — only needed for autonomous mode)
if ! check_command claude; then
    warn "  claude CLI not found (optional — needed for autonomous trading mode)"
    warn "  Install: https://docs.anthropic.com/en/docs/claude-code/overview"
fi

# ---------------------------------------------------------------------------
# Clone or update repository
# ---------------------------------------------------------------------------

mkdir -p "$GIMMES_HOME"

if [ -d "$REPO_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$REPO_DIR"
    git pull --ff-only origin main
    ok "Updated to $(git rev-parse --short HEAD)"
else
    info "Cloning gimmes..."
    MAX_RETRIES=3
    RETRY=0
    while [ $RETRY -lt $MAX_RETRIES ]; do
        if git clone "$REPO_URL" "$REPO_DIR"; then
            break
        fi
        RETRY=$((RETRY + 1))
        if [ $RETRY -lt $MAX_RETRIES ]; then
            DELAY=$((RETRY * 2))
            warn "Clone failed, retrying in ${DELAY}s... (attempt $((RETRY + 1))/$MAX_RETRIES)"
            sleep "$DELAY"
        else
            err "Failed to clone repository after $MAX_RETRIES attempts."
            exit 1
        fi
    done
    ok "Cloned to $REPO_DIR"
fi

# ---------------------------------------------------------------------------
# Set up Python environment
# ---------------------------------------------------------------------------

info "Setting up Python environment..."
cd "$REPO_DIR"
uv sync --quiet
ok "Python environment ready"

# ---------------------------------------------------------------------------
# Create global command
# ---------------------------------------------------------------------------

mkdir -p "$BIN_DIR"

# Remove old wrapper/symlink if present
rm -f "$BIN_DIR/gimmes"

# Create symlink to wrapper script in the repo
ln -sf "$REPO_DIR/bin/gimmes.sh" "$BIN_DIR/gimmes"
ok "Created command: $BIN_DIR/gimmes"

# ---------------------------------------------------------------------------
# Add to PATH
# ---------------------------------------------------------------------------

add_to_path() {
    local rc_file="$1"
    local path_line="export PATH=\"$BIN_DIR:\$PATH\""

    if [ -f "$rc_file" ] && grep -qF "$BIN_DIR" "$rc_file" 2>/dev/null; then
        return 0  # Already present
    fi

    {
        echo ""
        echo "# gimmes"
        echo "$path_line"
    } >> "$rc_file"
    ok "Added to PATH in $rc_file"
}

SHELL_NAME="$(basename "${SHELL:-/bin/bash}")"
RC_FILE=""

case "$SHELL_NAME" in
    zsh)
        RC_FILE="$HOME/.zshrc"
        add_to_path "$RC_FILE"
        ;;
    bash)
        if [ -f "$HOME/.bash_profile" ]; then
            RC_FILE="$HOME/.bash_profile"
        else
            RC_FILE="$HOME/.bashrc"
        fi
        add_to_path "$RC_FILE"
        ;;
    fish)
        FISH_CONFIG="$HOME/.config/fish/conf.d/gimmes.fish"
        mkdir -p "$(dirname "$FISH_CONFIG")"
        if [ ! -f "$FISH_CONFIG" ]; then
            echo "fish_add_path $BIN_DIR" > "$FISH_CONFIG"
            ok "Added to PATH in $FISH_CONFIG"
        fi
        ;;
    *)
        warn "Could not detect shell. Add this to your shell config manually:"
        warn "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

show_banner
ok "gimmes installed successfully!"
echo ""
info "Next steps:"
if [ -n "$RC_FILE" ]; then
    echo "  1. Restart your terminal (or run: source $RC_FILE)"
else
    echo "  1. Restart your terminal"
fi
if [ -f "$GIMMES_HOME/.env" ] && [ -f "$GIMMES_HOME/config/gimmes.toml" ]; then
    echo "  2. Run: gimmes mode   (verify your connection)"
else
    echo "  2. Run: gimmes init"
fi
echo "  3. Run: gimmes help"
echo ""
