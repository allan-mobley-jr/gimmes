"""Managed launchd schedule for the autonomous loop (#843, #827).

``gimmes schedule install`` renders the trading-hours wrapper and a
LaunchAgent plist. Safety rules:

- Never overwrite a file the user changed without ``--force``; the
  originals are backed up under ``GIMMES_HOME/backups/schedule/<ts>/``.
- Never run launchctl while a loop is running — a bootout SIGKILLs the
  job's process group (#638). The wrapper itself is always replaceable:
  it is written to a temp file and renamed, so a running bash keeps
  reading the old inode.
- Installing never starts trading: no RunAtLoad, KeepAlive, or kickstart.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

LABEL = "com.gimmes.trading-hours"
WRAPPER_NAME = "gimmes-trading-hours.sh"
START = (8, 0)
RETRY_SLOTS = ((8, 15), (8, 30), (9, 0))
END = (18, 0)
WEEKDAYS = (1, 2, 3, 4, 5)
# Anchored so a stray `grep "gimmes driving_range"` can't pass for the loop.
LOOP_PATTERN = "(^|/| -m )gimmes (driving_range|championship|start)( |$)"

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Out = Callable[[str], None]


@dataclass(frozen=True)
class SchedulePaths:
    gimmes_home: Path
    wrapper: Path
    gimmes_bin: Path
    launch_agents: Path
    plist: Path
    logs: Path
    manifest: Path
    backups: Path
    db: Path

    @classmethod
    def under(cls, home: Path, gimmes_home: Path) -> SchedulePaths:
        launch_agents = home / "Library" / "LaunchAgents"
        return cls(
            gimmes_home=gimmes_home,
            wrapper=gimmes_home / "bin" / WRAPPER_NAME,
            gimmes_bin=gimmes_home / "bin" / "gimmes",
            launch_agents=launch_agents,
            plist=launch_agents / f"{LABEL}.plist",
            logs=gimmes_home / "logs",
            manifest=gimmes_home / "schedule.json",
            backups=gimmes_home / "backups" / "schedule",
            db=gimmes_home / "gimmes.db",
        )

    @classmethod
    def default(cls) -> SchedulePaths:
        from gimmes.config import GIMMES_HOME

        return cls.under(Path.home(), GIMMES_HOME)


class FileState(StrEnum):
    MISSING = "missing"
    CURRENT = "current"  # identical to the new render
    MANAGED = "managed"  # installed by gimmes, unmodified since
    FOREIGN = "foreign"  # hand-made or locally modified


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _sec(hm: tuple[int, int]) -> int:
    return hm[0] * 3600 + hm[1] * 60


def render_wrapper(
    *,
    gimmes_home: Path,
    gimmes_bin: Path,
    min_free_gb: float,
    version: str,
    start: tuple[int, int] = START,
    end: tuple[int, int] = END,
    weekdays: Sequence[int] = WEEKDAYS,
    loop_pattern: str = LOOP_PATTERN,
    path: str | None = None,
) -> str:
    """Render the wrapper. ``@@NAME@@`` placeholders, because bash's own
    ``$VAR`` / ``${#…}`` syntax collides with string.Template and Jinja."""
    text = (
        resources.files("gimmes")
        .joinpath("templates/launchd/trading-hours.sh.tmpl")
        .read_text()
    )
    values = {
        "VERSION": version,
        "GIMMES_HOME": str(gimmes_home),
        "GIMMES_BIN": str(gimmes_bin),
        "MIN_FREE_KB": str(int(max(0.0, min_free_gb) * 1024 * 1024)),
        "START_SEC": str(_sec(start)),
        "END_SEC": str(_sec(end)),
        "WEEKDAYS": " ".join(str(d) for d in weekdays),
        "LOOP_PATTERN": loop_pattern,
        # launchd's env is minimal; bake the installing shell's PATH.
        "PATH": (path if path is not None else os.environ.get("PATH", "")
                 ).replace("'", ""),
    }
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    return text


def build_plist(
    paths: SchedulePaths,
    *,
    label: str = LABEL,
    slots: Sequence[tuple[int, int]] = (START, *RETRY_SLOTS),
    weekdays: Sequence[int] = WEEKDAYS,
) -> bytes:
    """The LaunchAgent: weekday fires at the start slot plus retries.

    A retry while the 08:00 run is alive is skipped by launchd (one
    instance per label) and no-ops in the wrapper's pgrep gate; a retry
    after a failed start is the #827 recovery.
    """
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(paths.wrapper)],
        "StartCalendarInterval": [
            {"Weekday": d, "Hour": h, "Minute": m}
            for d in weekdays for (h, m) in slots
        ],
        "StandardOutPath": str(paths.logs / "launchd.out.log"),
        "StandardErrorPath": str(paths.logs / "launchd.err.log"),
        "AbandonProcessGroup": False,
        # The wrapper's TERM trap waits for the loop's agent cleanup.
        "ExitTimeOut": 60,
    })


# ---------------------------------------------------------------------------
# Files, manifest, classification
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, indent=2).encode(), 0o644)


def classify(path: Path, rendered: bytes, manifest_sha: str | None) -> FileState:
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return FileState.MISSING
    if current == rendered:
        return FileState.CURRENT
    if manifest_sha and sha256_bytes(current) == manifest_sha:
        return FileState.MANAGED
    return FileState.FOREIGN


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the same directory + rename: a running bash keeps
    reading the old inode instead of a half-rewritten script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup(paths: SchedulePaths, files: Sequence[Path], ts: str, *, move: bool) -> Path:
    dest = paths.backups / ts
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = dest / f.name
        if move:
            os.replace(f, target)
        else:
            target.write_bytes(f.read_bytes())
    return dest


def find_legacy_plists(
    paths: SchedulePaths, label: str = LABEL,
) -> list[tuple[Path, str]]:
    """Hand-made LaunchAgents that run this wrapper under another label."""
    found: list[tuple[Path, str]] = []
    if not paths.launch_agents.is_dir():
        return found
    for plist in sorted(paths.launch_agents.glob("*.plist")):
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        args = data.get("ProgramArguments") or [data.get("Program", "")]
        if str(paths.wrapper) in [str(a) for a in args] and data.get("Label") != label:
            found.append((plist, str(data.get("Label", ""))))
    return found


def _diff(path: Path, rendered: bytes, limit: int = 40) -> str:
    try:
        old = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    lines = list(difflib.unified_diff(
        old, rendered.decode(errors="replace").splitlines(),
        fromfile=str(path), tofile="(gimmes render)", lineterm="",
    ))
    if len(lines) > limit:
        lines = [*lines[:limit], f"... ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# launchctl
# ---------------------------------------------------------------------------


@dataclass
class JobInfo:
    loaded: bool
    state: str | None = None
    pid: int | None = None
    last_exit: int | None = None
    runs: int | None = None


def run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The only real subprocess call in this module."""
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False,
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def parse_launchctl_print(text: str) -> JobInfo:
    def grab(key: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(key)} = (.+)$", text, re.M)
        return m.group(1).strip() if m else None

    def num(key: str) -> int | None:
        raw = grab(key)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    return JobInfo(
        loaded=True, state=grab("state"), pid=num("pid"),
        last_exit=num("last exit code"), runs=num("runs"),
    )


def _runner(runner: Runner | None) -> Runner:
    """Resolve at call time (not as a default arg) so tests can stub
    ``run_launchctl`` module-wide."""
    return runner if runner is not None else run_launchctl


def job_info(label: str, runner: Runner | None = None) -> JobInfo:
    res = _runner(runner)(["print", f"{_domain()}/{label}"])
    if res.returncode != 0:
        return JobInfo(loaded=False)
    return parse_launchctl_print(res.stdout)


def loop_busy(
    paths: SchedulePaths, labels: Sequence[str], runner: Runner,
) -> str | None:
    """Why launchctl must not run now, or None."""
    from gimmes.store.session import get_active_session

    session = get_active_session(paths.db)
    if session:
        return f"loop running (PID {session.get('pid')})"
    for label in labels:
        info = job_info(label, runner)
        if info.loaded and info.state == "running":
            return f"launchd job {label} running (PID {info.pid})"
    return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _not_darwin(out: Out) -> bool:
    if sys.platform != "darwin":
        out("gimmes schedule manages a macOS LaunchAgent; this is not macOS.")
        return True
    return False


def install(
    paths: SchedulePaths,
    *,
    force: bool,
    dry_run: bool,
    min_free_gb: float,
    version: str,
    runner: Runner | None = None,
    out: Out = print,
) -> int:
    """Install or update the managed schedule. Returns an exit code."""
    runner = _runner(runner)
    if _not_darwin(out):
        return 1
    manifest = load_manifest(paths.manifest)
    wrapper = render_wrapper(
        gimmes_home=paths.gimmes_home, gimmes_bin=paths.gimmes_bin,
        min_free_gb=min_free_gb, version=version,
    ).encode()
    plist = build_plist(paths)
    w_state = classify(paths.wrapper, wrapper, manifest.get("wrapper_sha256"))
    p_state = classify(paths.plist, plist, manifest.get("plist_sha256"))
    legacy = find_legacy_plists(paths)

    foreign = [
        p for p, s in ((paths.wrapper, w_state), (paths.plist, p_state))
        if s is FileState.FOREIGN
    ]
    if (foreign or legacy) and not force:
        for p in foreign:
            out(f"{p} differs from the managed version:")
            out(_diff(p, wrapper if p == paths.wrapper else plist))
        for p, label in legacy:
            out(f"Hand-made LaunchAgent {label} ({p}) runs this wrapper.")
        out(
            "Not changing anything. Rerun with --force to replace them;"
            " originals are backed up under"
            f" {paths.backups}/<timestamp>/."
        )
        return 1

    labels = [LABEL, *(label for _, label in legacy)]
    busy = loop_busy(paths, labels, runner)
    if dry_run:
        out(f"wrapper: {w_state}; plist: {p_state}; legacy jobs: "
            f"{[label for _, label in legacy] or 'none'}")
        out(f"launchd changes: {'deferred — ' + busy if busy else 'would apply'}")
        out("Dry run: nothing written.")
        return 0

    ts = _ts()
    to_backup = [p for p in foreign if p == paths.wrapper or not busy]
    if to_backup:
        dest = backup(paths, to_backup, ts, move=False)
        out(f"Backed up {len(to_backup)} file(s) to {dest}")

    if w_state is not FileState.CURRENT:
        atomic_write(paths.wrapper, wrapper, 0o755)
        out(f"Wrapper written: {paths.wrapper}")
    new_manifest = {
        **manifest,
        "version": version,
        "label": LABEL,
        "wrapper_sha256": sha256_bytes(wrapper),
        "min_free_gb": min_free_gb,
        "installed_at": ts,
    }
    new_manifest.pop("uninstalled_at", None)

    if busy:
        # Keep plist_sha256: a later install must still recognise our own
        # older plist as MANAGED, not FOREIGN (#843 review).
        new_manifest["launchd_pending"] = True
        save_manifest(paths.manifest, new_manifest)
        out(
            f"{busy}: wrapper updated (takes effect on next start);"
            " launchd changes deferred — rerun `gimmes schedule install`"
            " after the loop stops."
        )
        return 0

    # Load ours BEFORE retiring legacy jobs: a failed bootstrap must
    # never leave the machine with no schedule at all.
    loaded = job_info(LABEL, runner).loaded
    if p_state is not FileState.CURRENT or not loaded:
        if loaded and not _bootout(LABEL, runner, out):
            save_manifest(paths.manifest, new_manifest)
            return 1
        atomic_write(paths.plist, plist, 0o644)
        res = _bootstrap(paths.plist, runner)
        if res.returncode != 0:
            new_manifest["launchd_pending"] = True
            out(f"launchctl bootstrap failed: {res.stderr.strip()} — {LABEL}"
                " is NOT loaded (NO managed schedule loaded)"
                + ("; legacy jobs left in place." if legacy else
                   "; trading will not auto-start."))
            save_manifest(paths.manifest, new_manifest)
            return 1
        out(f"Loaded {LABEL} (weekdays {START[0]:02d}:{START[1]:02d},"
            f" retries {', '.join(f'{h:02d}:{m:02d}' for h, m in RETRY_SLOTS)};"
            f" stops {END[0]:02d}:{END[1]:02d} local)")
    new_manifest["plist_sha256"] = sha256_bytes(plist)
    new_manifest.pop("launchd_pending", None)
    save_manifest(paths.manifest, new_manifest)

    for plist_path, label in legacy:
        if not _bootout(label, runner, out):
            out(f"{plist_path} left in place; rerun after fixing launchd.")
            return 1
        backup(paths, [plist_path], ts, move=True)
        out(f"Unloaded {label}; its plist moved to {paths.backups / ts}")
    return 0


def _bootstrap(
    plist: Path, runner: Runner, attempts: int = 3,
) -> subprocess.CompletedProcess[str]:
    """launchd often rejects a bootstrap right after a bootout ("5:
    Input/output error") while it finishes tearing the job down."""
    import time

    res = runner(["bootstrap", _domain(), str(plist)])
    for _ in range(attempts - 1):
        if res.returncode == 0:
            break
        time.sleep(1)
        res = runner(["bootstrap", _domain(), str(plist)])
    return res


def _bootout(label: str, runner: Runner, out: Out) -> bool:
    """Unload ``label`` and verify it is gone (two loaded jobs would race
    the same wrapper every morning)."""
    res = runner(["bootout", f"{_domain()}/{label}"])
    if job_info(label, runner).loaded:
        out(f"launchctl bootout {label} failed (rc {res.returncode}):"
            f" {res.stderr.strip()} — it is still loaded.")
        return False
    return True


def refresh(
    paths: SchedulePaths,
    *,
    min_free_gb: float,
    version: str,
    runner: Runner | None = None,
) -> list[str]:
    """`gimmes update` hook: re-render managed files only. Never raises."""
    msgs: list[str] = []
    try:
        manifest = load_manifest(paths.manifest)
        if not manifest:
            return msgs  # not installed via gimmes — leave hand-made files alone
        wrapper = render_wrapper(
            gimmes_home=paths.gimmes_home, gimmes_bin=paths.gimmes_bin,
            min_free_gb=min_free_gb, version=version,
        ).encode()
        state = classify(paths.wrapper, wrapper, manifest.get("wrapper_sha256"))
        if state is FileState.FOREIGN:
            msgs.append(
                f"{paths.wrapper} is locally modified; not updating it"
                " (`gimmes schedule install --force` replaces it)."
            )
        elif state is not FileState.CURRENT:
            atomic_write(paths.wrapper, wrapper, 0o755)
            manifest.update(
                wrapper_sha256=sha256_bytes(wrapper), version=version,
                min_free_gb=min_free_gb,
            )
            save_manifest(paths.manifest, manifest)
            msgs.append(f"Schedule wrapper refreshed to v{version}.")
        plist = build_plist(paths)
        p_state = classify(paths.plist, plist, manifest.get("plist_sha256"))
        if p_state is not FileState.CURRENT and "uninstalled_at" not in manifest:
            msgs.append(
                "LaunchAgent differs from this version — run"
                " `gimmes schedule install` when no loop is running."
            )
    except Exception as exc:  # update must never fail on this
        msgs.append(f"schedule refresh skipped: {exc}")
    return msgs


def uninstall(
    paths: SchedulePaths, *, runner: Runner | None = None, out: Out = print,
) -> int:
    runner = _runner(runner)
    if _not_darwin(out):
        return 1
    busy = loop_busy(paths, [LABEL], runner)
    if busy:
        out(f"{busy}: refusing to unload the schedule now (#638). Stop the"
            " loop first.")
        return 1
    if job_info(LABEL, runner).loaded and not _bootout(LABEL, runner, out):
        return 1
    if paths.plist.exists():
        dest = backup(paths, [paths.plist], _ts(), move=True)
        out(f"Unloaded {LABEL}; plist moved to {dest}")
    else:
        out("No managed LaunchAgent installed.")
    manifest = load_manifest(paths.manifest)
    if manifest:
        manifest.pop("launchd_pending", None)
        manifest["uninstalled_at"] = _ts()
        save_manifest(paths.manifest, manifest)
    return 0


def status(
    paths: SchedulePaths, *, min_free_gb: float, version: str,
    runner: Runner | None = None,
) -> list[str]:
    runner = _runner(runner)
    manifest = load_manifest(paths.manifest)
    wrapper = render_wrapper(
        gimmes_home=paths.gimmes_home, gimmes_bin=paths.gimmes_bin,
        min_free_gb=min_free_gb, version=version,
    ).encode()
    lines = [
        f"Wrapper: {paths.wrapper} — "
        f"{classify(paths.wrapper, wrapper, manifest.get('wrapper_sha256'))}"
        f" (installed v{manifest.get('version', '?')})",
        f"LaunchAgent: {paths.plist} — "
        f"{classify(paths.plist, build_plist(paths), manifest.get('plist_sha256'))}",
    ]
    if manifest.get("launchd_pending"):
        lines.append("launchd changes pending — rerun `gimmes schedule install`"
                     " when no loop is running.")
    if sys.platform == "darwin":
        info = job_info(LABEL, runner)
        lines.append(
            f"launchd {LABEL}: "
            + (f"loaded, state={info.state}, pid={info.pid}, runs={info.runs},"
               f" last exit={info.last_exit}" if info.loaded else "not loaded")
        )
    for plist, label in find_legacy_plists(paths):
        lines.append(f"Legacy LaunchAgent: {label} ({plist})")
    for marker in _startup_markers(paths.logs):
        lines.append(f"Unrecorded startup failure: {marker}")
    return lines


# ---------------------------------------------------------------------------
# Startup-failure markers (#827): the wrapper writes them, the loop records
# them as error rows on its next start.
# ---------------------------------------------------------------------------


def _marker_tmp_dirs() -> list[Path]:
    """Where the wrapper's fallback markers can land (a seam for tests,
    which must never consume a real marker)."""
    return [Path(os.environ.get("TMPDIR", "/tmp")), Path("/tmp")]


def _startup_markers(logs_dir: Path, tmp_dir: Path | None = None) -> list[Path]:
    found = sorted(logs_dir.glob("STARTUP-FAILED-*")) if logs_dir.is_dir() else []
    # launchd may not pass TMPDIR, so the wrapper can fall back to /tmp
    # while a Terminal-started loop sees /var/folders/… — check both.
    tmps = [tmp_dir] if tmp_dir else _marker_tmp_dirs()
    seen: set[Path] = set()
    for tmp in tmps:
        if tmp.is_dir() and tmp.resolve() not in seen:
            seen.add(tmp.resolve())
            found += sorted(tmp.glob("gimmes-STARTUP-FAILED-*"))
    return [p for p in found if not p.name.endswith(".recorded")]
