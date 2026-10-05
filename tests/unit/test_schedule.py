"""Tests for the managed launchd schedule (#843, #827).

These run next to a live loop on the dev machine, so every test uses a
temp HOME/GIMMES_HOME, a fake launchctl, and a unique process pattern —
nothing here may touch the real LaunchAgents or the running loop.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from gimmes import schedule
from gimmes.schedule import SchedulePaths

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash/launchd only",
)


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch):  # type: ignore[no-untyped-def]
    def _boom(args):  # type: ignore[no-untyped-def]
        raise AssertionError(f"real launchctl called: {args}")

    monkeypatch.setattr(schedule, "run_launchctl", _boom)
    # Belt-and-braces: no code path may shell out to the real launchctl.
    real_run = subprocess.run

    def _guard(cmd, *a, **k):  # type: ignore[no-untyped-def]
        if cmd and os.path.basename(str(cmd[0])) == "launchctl":
            raise AssertionError(f"real launchctl: {cmd}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(schedule.subprocess, "run", _guard)
    monkeypatch.setattr(
        "gimmes.store.session.get_active_session", lambda db: None,
    )


@pytest.fixture
def paths(tmp_path: Path) -> SchedulePaths:
    home = tmp_path / "home"
    return SchedulePaths.under(home, home / ".gimmes")


class FakeLaunchctl:
    """Records calls; `print` reports loaded/state from its fields."""

    def __init__(self, *, loaded: bool = False, state: str = "waiting"):
        self.calls: list[list[str]] = []
        self.loaded = loaded
        self.state = state

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "not found")
            return subprocess.CompletedProcess(
                args, 0, f"\tstate = {self.state}\n\tpid = 99\n\truns = 3\n"
                "\tlast exit code = 0\n", "",
            )
        if args[0] == "bootstrap":
            self.loaded = True
        if args[0] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(args, 0, "", "")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls if c[0] != "print"]


def _install(paths, runner, **kw):  # type: ignore[no-untyped-def]
    out: list[str] = []
    with patch.object(sys, "platform", "darwin"):
        rc = schedule.install(
            paths, force=kw.get("force", False),
            dry_run=kw.get("dry_run", False), min_free_gb=1.0,
            version="9.9.9", runner=runner, out=out.append,
        )
    return rc, "\n".join(out)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRender:
    def _render(self, paths: SchedulePaths, **kw) -> str:  # type: ignore[no-untyped-def]
        return schedule.render_wrapper(
            gimmes_home=paths.gimmes_home, gimmes_bin=paths.gimmes_bin,
            min_free_gb=kw.pop("min_free_gb", 1.0), version="9.9.9", **kw,
        )

    def test_no_unrendered_placeholders(self, paths) -> None:  # type: ignore[no-untyped-def]
        assert "@@" not in self._render(paths)

    def test_bash_syntax_ok(self, paths, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script = tmp_path / "w.sh"
        script.write_text(self._render(paths))
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True)
        assert res.returncode == 0, res.stderr

    def test_managed_header_and_version(self, paths) -> None:  # type: ignore[no-untyped-def]
        text = self._render(paths)
        assert text.startswith("#!/bin/bash\n# gimmes-managed: trading-hours")
        assert "v9.9.9" in text

    @pytest.mark.parametrize(("gb", "kb"), [(1.0, 1048576), (0, 0)])
    def test_min_free_kb(self, paths, gb, kb) -> None:  # type: ignore[no-untyped-def]
        assert f"MIN_FREE_KB={kb}\n" in self._render(paths, min_free_gb=gb)

    def test_plist_shape(self, paths) -> None:  # type: ignore[no-untyped-def]
        data = plistlib.loads(schedule.build_plist(paths))
        assert data["Label"] == schedule.LABEL
        assert data["ProgramArguments"] == ["/bin/bash", str(paths.wrapper)]
        assert len(data["StartCalendarInterval"]) == 20  # 5 days × 4 slots
        for banned in ("RunAtLoad", "KeepAlive", "StartCalendarIntervalLaunchMissedRun"):
            assert banned not in data
        assert data["ExitTimeOut"] == 60

    @pytest.mark.skipif(sys.platform != "darwin", reason="plutil is macOS")
    def test_plist_lints(self, paths, tmp_path) -> None:  # type: ignore[no-untyped-def]
        p = tmp_path / "x.plist"
        p.write_bytes(schedule.build_plist(paths))
        assert subprocess.run(["plutil", "-lint", str(p)], capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Wrapper behavior (real bash, fake gimmes binary)
# ---------------------------------------------------------------------------


class TestWrapper:
    def _setup(self, tmp_path: Path, *, min_free_gb=0.0, weekdays=range(1, 8),  # type: ignore[no-untyped-def]
               start=(0, 0), end=(24, 0), binary=True, claude=True,
               clock=None):
        gh = tmp_path / "gh"
        (gh / "bin").mkdir(parents=True)
        fake = gh / "bin" / "gimmes"
        if binary:
            fake.write_text(
                "#!/bin/bash\n"
                'echo "$@" > "$FAKE_DIR/args"\n'
                "trap 'touch \"$FAKE_DIR/got-term\"; exit 0' TERM\n"
                'touch "$FAKE_DIR/started"\n'
                'sleep "${FAKE_SLEEP:-0}" & wait $!\n'
                'exit "${FAKE_RC:-0}"\n'
            )
            fake.chmod(0o755)
        pattern = f"fake-gimmes-{uuid.uuid4().hex}"
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        if claude:
            (fakebin / "claude").write_text("#!/bin/bash\n")
            (fakebin / "claude").chmod(0o755)
        if clock is not None:  # (H, M, S, weekday) — a deterministic `date`
            h, m, sec, dow = clock
            (fakebin / "date").write_text(
                "#!/bin/bash\n"
                f'case "$1" in +%H) echo {h:02d};; +%M) echo {m:02d};;'
                f' +%S) echo {sec:02d};; +%u) echo {dow};;'
                ' *) exec /bin/date "$@";; esac\n'
            )
            (fakebin / "date").chmod(0o755)
        script = tmp_path / "wrapper.sh"
        script.write_text(schedule.render_wrapper(
            gimmes_home=gh, gimmes_bin=fake, min_free_gb=min_free_gb,
            version="t", start=start, end=end, weekdays=list(weekdays),
            loop_pattern=pattern,
            path=f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        ))
        (fakebin / "logger").write_text(
            f'#!/bin/bash\necho "$@" >> "{tmp_path}/logger.calls"\n'
        )
        (fakebin / "logger").chmod(0o755)
        tmpdir = tmp_path / "tmpdir"
        tmpdir.mkdir()
        env = {
            **os.environ, "HOME": str(tmp_path / "nohome"),
            "FAKE_DIR": str(tmp_path), "TMPDIR": str(tmpdir),
        }
        return script, gh, env, pattern

    def _run(self, script, env, **extra):  # type: ignore[no-untyped-def]
        # Files, not pipes: a stray child must never hang the test.
        return subprocess.run(
            ["bash", str(script)], env={**env, **extra},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ).returncode

    def test_happy_path(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path)
        assert self._run(script, env) == 0
        assert (tmp_path / "args").read_text().strip() == "driving_range --cycles 0"
        assert not list((gh / "logs").glob("STARTUP-FAILED-*"))

    def test_propagates_exit_and_marks_death_at_birth(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path)
        assert self._run(script, env, FAKE_RC="3") == 3
        [marker] = (gh / "logs").glob("STARTUP-FAILED-*")
        assert "died at birth" in marker.read_text()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
    def test_unwritable_log_dir_fails_loud(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path)
        (gh / "logs").mkdir()
        (gh / "logs").chmod(0o555)
        try:
            assert self._run(script, env) == 75
        finally:
            (gh / "logs").chmod(0o755)
        assert not (tmp_path / "started").exists()
        assert list((tmp_path / "tmpdir").glob("gimmes-STARTUP-FAILED-*"))
        assert "startup failed" in (tmp_path / "logger.calls").read_text()

    def test_low_disk_fails_loud(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path, min_free_gb=10**9)
        assert self._run(script, env) == 75
        assert not (tmp_path / "started").exists()
        [marker] = (gh / "logs").glob("STARTUP-FAILED-*")
        assert "free disk" in marker.read_text()

    def test_missing_binary_exits_78(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, _, env, _ = self._setup(tmp_path, binary=False)
        assert self._run(script, env) == 78

    def test_missing_claude_exits_78_with_marker(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path, claude=False)
        assert self._run(script, env) == 78
        assert not (tmp_path / "started").exists()
        [marker] = (gh / "logs").glob("STARTUP-FAILED-*")
        assert "claude not on PATH" in marker.read_text()

    def test_never_sources_zshrc(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """#843 review: zsh-only syntax under `set -u` killed the old
        wrapper silently. A hostile rc must not matter."""
        script, _, env, _ = self._setup(tmp_path)
        home = tmp_path / "nohome"
        home.mkdir()
        (home / ".zshrc").write_text('echo "$UNSET_VAR_XYZ"\nexit 9\n')
        assert self._run(script, env) == 0
        assert (tmp_path / "started").exists()

    def test_outside_window_is_a_noop(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(tmp_path, start=(0, 0), end=(0, 0))
        assert self._run(script, env) == 0
        assert not (tmp_path / "started").exists()
        assert not list((gh / "logs").glob("STARTUP-FAILED-*"))

    @pytest.mark.parametrize(("hms", "starts"), [
        ((7, 59, 59), False),
        ((8, 0, 0), True),
        ((8, 9, 9), True),   # 08/09 are invalid octal without 10#
        ((9, 8, 0), True),
        ((17, 59, 59), True),
        ((18, 0, 0), False),
    ])
    def test_window_edges(self, tmp_path, hms, starts) -> None:  # type: ignore[no-untyped-def]
        script, _, env, _ = self._setup(
            tmp_path, weekdays=[1, 2, 3, 4, 5], start=(8, 0), end=(18, 0),
            clock=(*hms, 2),
        )
        assert self._run(script, env) == 0
        assert (tmp_path / "started").exists() is starts

    def test_weekend_is_a_noop(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, gh, env, _ = self._setup(
            tmp_path, weekdays=[1, 2, 3, 4, 5], start=(8, 0), end=(18, 0),
            clock=(10, 0, 0, 6),
        )
        assert self._run(script, env) == 0
        assert not (tmp_path / "started").exists()
        assert not list((gh / "logs").glob("STARTUP-FAILED-*"))

    def test_deadline_stops_loop_without_marker(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The 18:00 stop TERMs the loop; a deadline stop is not a
        failure, and the wrapper doesn't linger through the grace."""
        script, gh, env, _ = self._setup(
            tmp_path, start=(0, 0), end=(18, 0),
            clock=(17, 59, 58, 2),
        )
        t0 = time.monotonic()
        assert self._run(script, env, FAKE_SLEEP="20") == 0
        assert time.monotonic() - t0 < 15
        assert (tmp_path / "got-term").exists()
        assert not list((gh / "logs").glob("STARTUP-FAILED-*"))

    def test_already_running_is_a_noop(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        script, _, env, pattern = self._setup(tmp_path)
        dummy = subprocess.Popen(
            ["bash", "-c", f"exec -a {pattern} sleep 30"],
        )
        try:
            time.sleep(0.2)
            assert self._run(script, env) == 0
            assert not (tmp_path / "started").exists()
        finally:
            dummy.kill()
            dummy.wait()

    def test_term_trap_forwards_to_loop(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """#638: stopping the wrapper must stop the loop, not orphan it."""
        script, _, env, _ = self._setup(tmp_path)
        proc = subprocess.Popen(
            ["bash", str(script)], env={**env, "FAKE_SLEEP": "30"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if (tmp_path / "started").exists():
                    break
                time.sleep(0.05)
            assert (tmp_path / "started").exists()
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0
            assert (tmp_path / "got-term").exists()
        finally:
            if proc.poll() is None:
                proc.kill()


# ---------------------------------------------------------------------------
# install / refresh / uninstall
# ---------------------------------------------------------------------------


class TestInstall:
    def test_fresh_install(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        rc, _ = _install(paths, lc)
        assert rc == 0
        assert paths.wrapper.stat().st_mode & 0o777 == 0o755
        assert paths.plist.exists()
        assert lc.verbs() == ["bootstrap"]
        manifest = schedule.load_manifest(paths.manifest)
        assert manifest["plist_sha256"] and manifest["wrapper_sha256"]

    def test_second_install_is_a_noop(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        _install(paths, lc)
        lc.calls.clear()
        rc, _ = _install(paths, lc)
        assert rc == 0
        assert lc.verbs() == []

    def test_refuses_foreign_wrapper_without_force(self, paths) -> None:  # type: ignore[no-untyped-def]
        paths.wrapper.parent.mkdir(parents=True)
        paths.wrapper.write_text("#!/bin/bash\n# hand-made\n")
        lc = FakeLaunchctl()
        rc, out = _install(paths, lc)
        assert rc == 1
        assert paths.wrapper.read_text() == "#!/bin/bash\n# hand-made\n"
        assert "--force" in out and "hand-made" in out
        assert lc.verbs() == []

    def test_force_backs_up_and_migrates_legacy(self, paths) -> None:  # type: ignore[no-untyped-def]
        paths.wrapper.parent.mkdir(parents=True)
        paths.wrapper.write_text("#!/bin/bash\n# hand-made\n")
        paths.launch_agents.mkdir(parents=True)
        legacy = paths.launch_agents / "com.someone.gimmes.plist"
        legacy.write_bytes(plistlib.dumps({
            "Label": "com.someone.gimmes",
            "ProgramArguments": [str(paths.wrapper)],
        }))
        lc = FakeLaunchctl()
        rc, _ = _install(paths, lc, force=True)
        assert rc == 0
        assert not legacy.exists()
        backups = list(paths.backups.rglob("*"))
        names = {p.name for p in backups}
        assert {"gimmes-trading-hours.sh", "com.someone.gimmes.plist"} <= names
        legacy_out = ["bootout", f"gui/{os.getuid()}/com.someone.gimmes"]
        assert legacy_out in lc.calls
        # Ours loads BEFORE the legacy job is retired: a failed load must
        # never leave no schedule at all.
        assert lc.verbs() == ["bootstrap", "bootout"]

    def test_running_loop_defers_all_launchctl(self, paths, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(
            "gimmes.store.session.get_active_session",
            lambda db: {"pid": 4242},
        )
        lc = FakeLaunchctl()
        rc, out = _install(paths, lc)
        assert rc == 0
        assert lc.calls == []
        assert paths.wrapper.exists() and not paths.plist.exists()
        assert "plist_sha256" not in schedule.load_manifest(paths.manifest)
        assert "deferred" in out

    def test_deferred_then_idle_install_needs_no_force(self, paths, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """#843 review: a deferred install must not make our own older
        plist look FOREIGN to the next install."""
        lc = FakeLaunchctl()
        _install(paths, lc)
        old = paths.plist.read_bytes()
        monkeypatch.setattr(schedule, "build_plist", lambda p, **kw: old + b"<!-- v2 -->")
        monkeypatch.setattr(
            "gimmes.store.session.get_active_session", lambda db: {"pid": 1},
        )
        rc, _ = _install(paths, lc)
        assert rc == 0
        assert schedule.load_manifest(paths.manifest)["launchd_pending"] is True
        monkeypatch.setattr(
            "gimmes.store.session.get_active_session", lambda db: None,
        )
        rc, out = _install(paths, lc)
        assert rc == 0, out
        assert paths.plist.read_bytes().endswith(b"<!-- v2 -->")
        assert "launchd_pending" not in schedule.load_manifest(paths.manifest)

    def test_legacy_bootout_failure_keeps_plist(self, paths) -> None:  # type: ignore[no-untyped-def]
        paths.launch_agents.mkdir(parents=True)
        legacy = paths.launch_agents / "com.someone.gimmes.plist"
        legacy.write_bytes(plistlib.dumps({
            "Label": "com.someone.gimmes",
            "ProgramArguments": [str(paths.wrapper)],
        }))

        class Stuck(FakeLaunchctl):
            def __call__(self, args):  # type: ignore[no-untyped-def]
                if args[0] == "print" and args[1].endswith("com.someone.gimmes"):
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 0, "\tstate = waiting\n", "")
                return super().__call__(args)

        rc, out = _install(paths, Stuck(), force=True)
        assert rc == 1
        assert legacy.exists()
        assert "still loaded" in out

    def test_changed_plist_reloads_bootout_then_bootstrap(self, paths, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        _install(paths, lc)
        old = paths.plist.read_bytes()
        monkeypatch.setattr(schedule, "build_plist", lambda p, **kw: old + b"<!-- v2 -->")
        lc.calls.clear()
        rc, _ = _install(paths, lc)
        assert rc == 0
        assert lc.verbs() == ["bootout", "bootstrap"]

    def test_bootstrap_failure_returns_1_and_marks_pending(self, paths) -> None:  # type: ignore[no-untyped-def]
        class Fails(FakeLaunchctl):
            def __call__(self, args):  # type: ignore[no-untyped-def]
                if args[0] == "bootstrap":
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 5, "", "I/O error")
                return super().__call__(args)

        with patch("time.sleep"):
            rc, out = _install(paths, Fails())
        assert rc == 1
        assert "NOT loaded" in out
        assert schedule.load_manifest(paths.manifest)["launchd_pending"] is True

    def test_running_launchd_job_defers(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl(loaded=True, state="running")
        rc, _ = _install(paths, lc)
        assert rc == 0
        assert lc.verbs() == []
        assert not paths.plist.exists()

    def test_wrapper_replace_is_atomic(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        _install(paths, lc)
        paths.wrapper.write_text(paths.wrapper.read_text() + "# drift\n")
        manifest = schedule.load_manifest(paths.manifest)
        manifest["wrapper_sha256"] = schedule.sha256_bytes(paths.wrapper.read_bytes())
        schedule.save_manifest(paths.manifest, manifest)
        old_inode = paths.wrapper.stat().st_ino
        with open(paths.wrapper, "rb") as running:  # a bash mid-read
            _install(paths, lc)
            assert b"# drift" in running.read()
        assert paths.wrapper.stat().st_ino != old_inode

    def test_dry_run_writes_nothing(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        rc, _ = _install(paths, lc, dry_run=True)
        assert rc == 0
        assert not paths.wrapper.exists() and not paths.manifest.exists()
        assert lc.verbs() == []

    def test_non_darwin_refuses(self, paths) -> None:  # type: ignore[no-untyped-def]
        with patch.object(sys, "platform", "linux"):
            rc = schedule.install(
                paths, force=False, dry_run=False, min_free_gb=1.0,
                version="x", runner=FakeLaunchctl(), out=lambda s: None,
            )
        assert rc == 1


class TestRefresh:
    def test_rewrites_unmodified_managed_wrapper(self, paths) -> None:  # type: ignore[no-untyped-def]
        _install(paths, FakeLaunchctl())
        msgs = schedule.refresh(paths, min_free_gb=2.0, version="10.0.0")
        assert any("refreshed" in m for m in msgs)
        assert "MIN_FREE_KB=2097152" in paths.wrapper.read_text()

    def test_refresh_twice_keeps_tracking(self, paths) -> None:  # type: ignore[no-untyped-def]
        _install(paths, FakeLaunchctl())
        schedule.refresh(paths, min_free_gb=1.0, version="10.0.0")
        msgs = schedule.refresh(paths, min_free_gb=1.0, version="11.0.0")
        assert any("refreshed" in m for m in msgs)
        assert "v11.0.0" in paths.wrapper.read_text()

    def test_skips_modified_wrapper(self, paths) -> None:  # type: ignore[no-untyped-def]
        _install(paths, FakeLaunchctl())
        paths.wrapper.write_text("# mine\n")
        msgs = schedule.refresh(paths, min_free_gb=1.0, version="10.0.0")
        assert paths.wrapper.read_text() == "# mine\n"
        assert any("locally modified" in m for m in msgs)

    def test_noop_when_not_installed(self, paths) -> None:  # type: ignore[no-untyped-def]
        assert schedule.refresh(paths, min_free_gb=1.0, version="x") == []
        assert not paths.wrapper.exists()

    def test_never_raises(self, paths, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _install(paths, FakeLaunchctl())
        monkeypatch.setattr(schedule, "render_wrapper", lambda **kw: 1 / 0)
        msgs = schedule.refresh(paths, min_free_gb=1.0, version="x")
        assert any("skipped" in m for m in msgs)


class TestUninstallAndStatus:
    def test_uninstall_refuses_while_running(self, paths, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _install(paths, FakeLaunchctl())
        monkeypatch.setattr(
            "gimmes.store.session.get_active_session", lambda db: {"pid": 7},
        )
        lc = FakeLaunchctl(loaded=True)
        with patch.object(sys, "platform", "darwin"):
            assert schedule.uninstall(paths, runner=lc, out=lambda s: None) == 1
        assert lc.verbs() == []
        assert paths.plist.exists()

    def test_uninstall_unloads_and_backs_up(self, paths) -> None:  # type: ignore[no-untyped-def]
        lc = FakeLaunchctl()
        _install(paths, lc)
        with patch.object(sys, "platform", "darwin"):
            assert schedule.uninstall(paths, runner=lc, out=lambda s: None) == 0
        assert "bootout" in lc.verbs()
        assert not paths.plist.exists()
        assert list(paths.backups.rglob(f"{schedule.LABEL}.plist"))
        assert "uninstalled_at" in schedule.load_manifest(paths.manifest)
        # No nagging on later updates after a deliberate uninstall.
        msgs = schedule.refresh(paths, min_free_gb=1.0, version="9.9.9")
        assert not any("LaunchAgent differs" in m for m in msgs)

    def test_parse_launchctl_print(self) -> None:
        info = schedule.parse_launchctl_print(
            "\tstate = running\n\tpid = 1144\n\truns = 28\n\tlast exit code = 0\n"
        )
        assert (info.state, info.pid, info.runs, info.last_exit) == (
            "running", 1144, 28, 0,
        )

    def test_status_reports_markers_and_pending(self, paths) -> None:  # type: ignore[no-untyped-def]
        paths.logs.mkdir(parents=True)
        (paths.logs / "STARTUP-FAILED-2026-09-14").write_text("x\n")
        schedule.save_manifest(
            paths.manifest, {"version": "1", "launchd_pending": True},
        )
        with patch.object(sys, "platform", "linux"):
            lines = schedule.status(paths, min_free_gb=1.0, version="1")
        text = "\n".join(lines)
        assert "STARTUP-FAILED-2026-09-14" in text
        assert "launchd changes pending" in text


# ---------------------------------------------------------------------------
# Marker ingestion (#827): the loop records failed starts as error rows
# ---------------------------------------------------------------------------


class TestIngestStartupMarkers:
    def _rows(self, db_path: Path) -> list[tuple]:  # type: ignore[type-arg]
        import sqlite3

        con = sqlite3.connect(db_path)
        try:
            return con.execute(
                "SELECT error_code, severity, component FROM error_log",
            ).fetchall()
        finally:
            con.close()

    def test_records_row_and_renames(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from gimmes.cli import _ingest_startup_markers
        from gimmes.config import GimmesConfig

        logs = tmp_path / "logs"
        logs.mkdir()
        marker = logs / "STARTUP-FAILED-2026-09-14"
        marker.write_text("2026-09-14T12:00:04Z startup failed: free disk\n")
        cfg = GimmesConfig(db_path=tmp_path / "g.db")
        n = asyncio.run(_ingest_startup_markers(cfg, logs, tmp_path / "none"))
        assert n == 1
        assert self._rows(cfg.db_path) == [
            ("startup_failed", "error", "schedule.wrapper"),
        ]
        assert not marker.exists()
        assert (logs / "STARTUP-FAILED-2026-09-14.recorded").exists()
        # Idempotent: a recorded marker is never ingested twice.
        assert asyncio.run(_ingest_startup_markers(cfg, logs, tmp_path / "none")) == 0

    def test_reads_tmpdir_fallback(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from gimmes.cli import _ingest_startup_markers
        from gimmes.config import GimmesConfig

        tmpdir = tmp_path / "t"
        tmpdir.mkdir()
        (tmpdir / "gimmes-STARTUP-FAILED-2026-09-14").write_text("x\n")
        cfg = GimmesConfig(db_path=tmp_path / "g.db")
        assert asyncio.run(_ingest_startup_markers(cfg, tmp_path / "logs", tmpdir)) == 1

    def test_insert_failure_keeps_marker(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from gimmes.cli import _ingest_startup_markers
        from gimmes.config import GimmesConfig

        logs = tmp_path / "logs"
        logs.mkdir()
        marker = logs / "STARTUP-FAILED-2026-09-14"
        marker.write_text("x\n")

        async def _fail(db, entry):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

        monkeypatch.setattr("gimmes.store.queries.insert_error", _fail)
        cfg = GimmesConfig(db_path=tmp_path / "g.db")
        assert asyncio.run(_ingest_startup_markers(cfg, logs, tmp_path / "none")) == 0
        assert marker.exists()
