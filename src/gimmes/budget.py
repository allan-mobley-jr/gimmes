"""Daily Claude API budget guardrail for the autonomous trading loop.

See GitHub issue #545. The autonomous loop spawns one ``claude`` subprocess
per cycle; without a guardrail, a single multi-day run can burn through
the Anthropic Max plan cap silently. This module:

- Persists per-UTC-day token + cost totals to ``${GIMMES_HOME}/budget.json``.
- Parses the ``usage`` block from each cycle's ``stream-json`` stdout.
- Exposes ``BudgetTracker.should_block()`` so the loop can sleep until the
  next UTC midnight when either the session-count cap or the dollar cap is
  reached for the current day.

The module is intentionally fail-open on input parsing (a malformed cycle
returns ``None`` from :func:`parse_usage_from_stream_json` rather than
raising — the loop should treat this as "skip accounting" and keep going)
but fail-closed on the cap (an unknown model falls back to the Sonnet
pricing rather than recording $0). Both policies preserve the safety-net
goal: the cap should be conservative, not an additional crash surface.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

logger = logging.getLogger("gimmes.budget")

# USD per 1 million tokens. Anthropic public list pricing.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_creation": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_creation": 18.75,
        "cache_read": 1.50,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_creation": 1.25,
        "cache_read": 0.10,
    },
}

DEFAULT_MAX_SESSIONS = 80
DEFAULT_MAX_USD = 25.0
SCHEMA_VERSION = 1
RETAIN_DAYS = 30

# Models the tracker has already complained about — keeps the WARNING log
# from spamming once Claude rotates to a new model id we don't have rates
# for. Reset between processes (cleared on import).
_warned_models: set[str] = set()


@dataclass(frozen=True)
class DaySummary:
    """A single UTC-day's accumulated Claude-API consumption."""

    date: str
    sessions: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


def cost_from_usage(usage: dict, model_id: str) -> float:
    """Compute USD cost for a single cycle's usage block.

    Falls back to Sonnet rates if ``model_id`` is unknown — the cap is a
    budget guardrail, so it must keep producing a number even when models
    rotate. Logs a one-time **error** per unknown id (not warning — silent
    underestimation of a 5×-cost model would be the worst-case regression
    of this safety net).
    """
    rates = PRICING.get(model_id)
    if rates is None:
        if model_id not in _warned_models:
            logger.error(
                "budget: unknown model %r — falling back to Sonnet pricing."
                " Update PRICING in src/gimmes/budget.py to track real cost.",
                model_id,
            )
            _warned_models.add(model_id)
        rates = PRICING["claude-sonnet-4-6"]
    return (
        rates["input"] * usage.get("input_tokens", 0)
        + rates["output"] * usage.get("output_tokens", 0)
        + rates["cache_creation"] * usage.get("cache_creation_input_tokens", 0)
        + rates["cache_read"] * usage.get("cache_read_input_tokens", 0)
    ) / 1_000_000


def parse_usage_from_stream_json(stdout: bytes) -> dict | None:
    """Extract the ``usage`` block from a Claude Code stream-json stdout.

    Iterates events in reverse and returns the first parseable ``usage``
    dict found on a ``type: result`` (or ``type: assistant`` with a
    message-level usage) event. Fail-open: returns ``None`` on empty
    stdout, malformed JSON, or missing fields.
    """
    if not stdout:
        return None
    for raw in reversed(stdout.strip().split(b"\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # Direct usage on result envelope
        usage = event.get("usage")
        if isinstance(usage, dict) and "input_tokens" in usage:
            return usage
        # Nested usage on assistant message envelope
        msg = event.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict) and "input_tokens" in usage:
                return usage
    return None


def _default_clock() -> datetime:
    """Module-level default clock — extracted from BudgetTracker so the
    dataclass field can use a non-lambda default for clarity."""
    return datetime.now(UTC)


@dataclass
class BudgetTracker:
    """Per-UTC-day Claude API budget tracker with atomic persistence.

    Single-writer safe: the in-process lock + atomic file rename guarantee
    that concurrent ``record_cycle`` calls from threads in the same process
    never produce lost updates. **Multi-process safety is not provided** —
    if two autonomous loops run concurrently against the same
    ``budget.json``, last-writer-wins on the read-modify-write may drop
    counter increments. The intended deployment is one loop per host.
    """

    path: Path
    max_sessions: int = DEFAULT_MAX_SESSIONS
    max_cost_usd: float = DEFAULT_MAX_USD
    clock: Callable[[], datetime] = _default_clock
    _write_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )

    def _today_key(self) -> str:
        return self.clock().date().isoformat()

    def _read_state(self) -> dict:
        """Read budget.json, recovering gracefully from missing/corrupt."""
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "days": {}}
        try:
            text = self.path.read_text()
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            archive = self.path.with_suffix(
                f".corrupt.{int(self.clock().timestamp())}",
            )
            with contextlib.suppress(OSError):
                self.path.replace(archive)
            logger.warning(
                "budget: %s unreadable (%s) — archived to %s, starting fresh",
                self.path, exc, archive,
            )
            return {"version": SCHEMA_VERSION, "days": {}}
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            logger.warning(
                "budget: %s has unknown schema version %r — treating as fresh",
                self.path, data.get("version") if isinstance(data, dict) else None,
            )
            return {"version": SCHEMA_VERSION, "days": {}}
        if not isinstance(data.get("days"), dict):
            data["days"] = {}
        return data

    def _atomic_write(self, data: dict) -> None:
        """Atomic write via unique tempfile + os.replace; fsync before rename.

        Uses :func:`tempfile.mkstemp` so concurrent writers get distinct
        temp paths (fixed-clock test harnesses or rapid successive writes
        within the same microsecond would otherwise collide).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".tmp.",
            dir=str(self.path.parent),
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _prune(self, data: dict) -> None:
        """Drop day entries older than RETAIN_DAYS days."""
        cutoff = (self.clock().date() - timedelta(days=RETAIN_DAYS)).isoformat()
        data["days"] = {
            k: v for k, v in data["days"].items() if k >= cutoff
        }

    def today(self) -> DaySummary:
        """Return today's accumulated summary (zero-valued if no entries)."""
        return self.summary_for_date(self._today_key())

    def summary_for_date(self, date_str: str) -> DaySummary:
        """Return the accumulated summary for an arbitrary UTC date.

        Used by ``gimmes budget --days N`` to render trailing days without
        having to swap the tracker's ``clock`` (which is reserved for
        current-time semantics like ``secs_until_reset`` / pruning).
        """
        data = self._read_state()
        entry = data["days"].get(date_str, {})
        return DaySummary(
            date=date_str,
            sessions=int(entry.get("sessions", 0)),
            cost_usd=float(entry.get("cost_usd", 0.0)),
            input_tokens=int(entry.get("input_tokens", 0)),
            output_tokens=int(entry.get("output_tokens", 0)),
            cache_creation_tokens=int(entry.get("cache_creation_tokens", 0)),
            cache_read_tokens=int(entry.get("cache_read_tokens", 0)),
        )

    def caps_in_effect(self) -> tuple[int, float]:
        """Return the caps that the loop most recently persisted.

        Falls back to this tracker's own ``max_sessions`` / ``max_cost_usd``
        when ``budget.json`` does not yet record them (first run).
        """
        data = self._read_state()
        caps = data.get("caps", {})
        return (
            int(caps.get("max_sessions", self.max_sessions)),
            float(caps.get("max_cost_usd", self.max_cost_usd)),
        )

    def should_block(self) -> tuple[bool, str | None]:
        """Return ``(blocked, reason)`` where reason ∈ {'sessions', 'cost', None}.

        A cap of ``0`` is treated as "unlimited" — matches ``--cycles 0``
        semantics from #543.
        """
        summary = self.today()
        if self.max_sessions > 0 and summary.sessions >= self.max_sessions:
            return True, "sessions"
        if self.max_cost_usd > 0 and summary.cost_usd >= self.max_cost_usd:
            return True, "cost"
        return False, None

    def secs_until_reset(self) -> int:
        """Seconds until the next UTC midnight (when day key rolls over)."""
        now = self.clock()
        tomorrow = (now + timedelta(days=1)).date()
        midnight = datetime.combine(tomorrow, time.min, tzinfo=UTC)
        return max(0, int((midnight - now).total_seconds()))

    def record_cycle(self, usage: dict, model_id: str) -> DaySummary:
        """Add one cycle's usage to today's totals; persists atomically.

        The in-process lock prevents lost updates from concurrent threads
        in the same process — important for tests and any future
        concurrency. See class docstring for multi-process caveats.
        """
        cost = cost_from_usage(usage, model_id)
        with self._write_lock:
            data = self._read_state()
            self._prune(data)
            data["caps"] = {
                "max_sessions": self.max_sessions,
                "max_cost_usd": self.max_cost_usd,
            }
            key = self._today_key()
            entry = data["days"].setdefault(key, {})
            entry["sessions"] = int(entry.get("sessions", 0)) + 1
            entry["cost_usd"] = round(
                float(entry.get("cost_usd", 0.0)) + cost, 6,
            )
            entry["input_tokens"] = int(entry.get("input_tokens", 0)) + int(
                usage.get("input_tokens", 0),
            )
            entry["output_tokens"] = int(entry.get("output_tokens", 0)) + int(
                usage.get("output_tokens", 0),
            )
            entry["cache_creation_tokens"] = int(
                entry.get("cache_creation_tokens", 0),
            ) + int(usage.get("cache_creation_input_tokens", 0))
            entry["cache_read_tokens"] = int(
                entry.get("cache_read_tokens", 0),
            ) + int(usage.get("cache_read_input_tokens", 0))
            self._atomic_write(data)
        return self.today()

    def format_status_line(self) -> str:
        """One-line status for the loop's startup banner / periodic prints."""
        s = self.today()
        s_cap = "∞" if self.max_sessions == 0 else str(self.max_sessions)
        c_cap = "∞" if self.max_cost_usd == 0 else f"${self.max_cost_usd:.2f}"
        return (
            f"Budget {s.date}: {s.sessions}/{s_cap} sessions, "
            f"${s.cost_usd:.2f}/{c_cap}"
        )

    def alert_message(self, reason: str) -> str:
        """Human-readable cap-hit message for console + iMessage alerts."""
        s = self.today()
        if reason == "sessions":
            return (
                f"Daily session cap reached: {s.sessions}/{self.max_sessions}"
                f" (${s.cost_usd:.2f}). Sleeping until UTC midnight."
            )
        if reason == "cost":
            return (
                f"Daily cost cap reached: ${s.cost_usd:.2f}/${self.max_cost_usd:.2f}"
                f" ({s.sessions} sessions). Sleeping until UTC midnight."
            )
        return f"Budget cap reached: {reason}. Sleeping until UTC midnight."
