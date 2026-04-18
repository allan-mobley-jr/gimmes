#!/usr/bin/env bash
# GIMMES Driving Range Monitor
# Runs periodic health checks and sends iMessage alerts for problems.
#
# Usage (managed by `gimmes monitor`):
#   monitor.sh [--quiet]

set -uo pipefail

GIMMES_HOME="${GIMMES_HOME:-$HOME/.gimmes}"
REPO="${GIMMES_HOME}/repo"
PYTHON="${REPO}/.venv/bin/python"
GIMMES="${PYTHON} -m gimmes"
LOG_DIR="${GIMMES_HOME}/logs"
MONITOR_LOG="${LOG_DIR}/monitor.log"
CONFIG_FILE="${GIMMES_HOME}/monitor.conf"
NOTIFY_PHONE="+17706162336"
STALENESS_THRESHOLD="${GIMMES_STALENESS_THRESHOLD:-10800}"

QUIET=false
[[ "${1:-}" == "--quiet" ]] && QUIET=true
if [[ -f "${CONFIG_FILE}" ]] && grep -q "quiet" "${CONFIG_FILE}" 2>/dev/null; then
    QUIET=true
fi

mkdir -p "$LOG_DIR"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) $1" >> "$MONITOR_LOG"; }

alert() {
    local msg="$1"
    log "ALERT: $msg"
    if [[ "$QUIET" == "false" ]]; then
        osascript -e "tell application \"Messages\" to send \"GIMMES ALERT: $msg\" to participant \"$NOTIFY_PHONE\"" 2>/dev/null || true
    fi
}

issues=()

# 1. Risk check (uses exit code — non-zero means limit breached)
if ! $GIMMES risk-check > /dev/null 2>&1; then
    issues+=("Risk check FAILED — limit may be breached")
fi

# 2. Check for unresolved errors
error_output=$($GIMMES errors --unresolved --summary 2>&1) || true
if echo "$error_output" | grep -qE "[5-9][0-9]+ unresolved|[0-9]{2,} unresolved"; then
    issues+=("Many unresolved errors: $error_output")
fi

# 3. Check latest cycle for failures
latest_log=$(ls -t "$LOG_DIR"/cycle-*.json 2>/dev/null | head -1)
if [[ -n "$latest_log" ]]; then
    # Check if last 3 cycles all failed
    fail_count=0
    for log_file in $(ls -t "$LOG_DIR"/cycle-*.json 2>/dev/null | head -3); do
        if grep -q "hit your limit\|crashed\|circuit.breaker\|exited with an error" "$log_file" 2>/dev/null; then
            ((fail_count++)) || true
        fi
    done
    if [[ "$fail_count" -ge 3 ]]; then
        issues+=("Last $fail_count cycles failed")
    fi

    # Check cycle age — alert if no cycle in >2 hours
    if [[ "$(uname)" == "Darwin" ]]; then
        log_age=$(( $(date +%s) - $(stat -f %m "$latest_log") ))
    else
        log_age=$(( $(date +%s) - $(stat -c %Y "$latest_log") ))
    fi
    hours_ago=$((log_age / 3600))
    if [[ "$log_age" -gt "$STALENESS_THRESHOLD" ]]; then
        issues+=("No cycle in ${hours_ago}h — driving range may have stopped (threshold: $((STALENESS_THRESHOLD/3600))h)")
    fi
fi

# --- Report ---

if [[ ${#issues[@]} -eq 0 ]]; then
    log "OK: All checks passed"
    exit 0
else
    summary=""
    for issue in "${issues[@]}"; do
        summary="${summary}- ${issue}\n"
    done
    alert "$(echo -e "$summary")"

    # Create GitHub issue for critical problems (>= 3 issues)
    if [[ ${#issues[@]} -ge 3 ]]; then
        cd "$REPO" 2>/dev/null || true
        existing=$(gh issue list --state open --search "Monitor alert" --limit 1 --json number --jq '.[0].number' 2>/dev/null || echo "")
        if [[ -z "$existing" || "$existing" == "null" ]]; then
            gh issue create \
                --title "Monitor alert: ${#issues[@]} issues detected" \
                --body "$(echo -e "## Monitor Alert — $(ts)\n\n$(echo -e "$summary")\n\nFiled automatically by \`gimmes monitor\`.")" \
                --label "bug" 2>/dev/null || true
        fi
    fi

    exit 1
fi
