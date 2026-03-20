"""Unit tests for shell functions in bin/gimmes.sh."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GIMMES_SH = Path(__file__).parents[2] / "bin" / "gimmes.sh"

# Extract only the function definitions (everything before show_version) via
# sed, then eval into the current shell. This avoids executing the main case
# block or functions that require a real git repo at $REPO. The shebang and
# variable assignments (GIMMES_HOME, REPO, PYTHON) are stripped so tests can
# supply their own REPO value.
_LOAD_FUNCTIONS = (
    "eval \"$(sed -n '"
    "/^#!/d; /^GIMMES_HOME=/d; /^REPO=/d; /^PYTHON=/d; "
    f"/^show_version/q; p' '{GIMMES_SH}')\""
)

# Prepended to git-setup scripts so failures are caught and a test identity
# is available even on machines with no global git config.
_SETUP_PREAMBLE = (
    "set -euo pipefail; "
    "export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test "
    "GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test; "
)


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet and return the result."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


def run_setup(script: str) -> None:
    """Run a git-setup script, failing the test if any command errors."""
    result = run_bash(_SETUP_PREAMBLE + script)
    assert result.returncode == 0, f"setup failed: {result.stderr}"


def run_function(
    call: str, *, repo: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Load gimmes.sh functions and run a shell command."""
    env = f'export REPO="{repo}"; ' if repo else ""
    result = run_bash(f"{env}{_LOAD_FUNCTIONS} && {call}")
    assert result.returncode == 0, (
        f"shell function failed (rc={result.returncode}): {result.stderr}"
    )
    return result


def init_repo(path: Path) -> None:
    """Create a git repo with a single empty commit."""
    run_setup(
        f'git init "{path}" --quiet'
        f' && git -C "{path}" commit --allow-empty -m "init" --quiet'
    )


def add_tags(repo: Path, *tags: str) -> None:
    """Add one or more tags to an existing repo."""
    script = " && ".join(f'git -C "{repo}" tag {tag}' for tag in tags)
    run_setup(script)


def run_semver_compare(a: str, b: str) -> int:
    """Run semver_compare and return its exit code (0=equal, 1=gt, 2=lt)."""
    result = run_bash(
        f'{_LOAD_FUNCTIONS} || exit 99; '
        f'semver_compare "{a}" "{b}" && rc=0 || rc=$?; echo $rc'
    )
    assert result.returncode == 0, (
        f"function loading failed (rc={result.returncode}): {result.stderr}"
    )
    return int(result.stdout.strip())


# -- semver_compare -----------------------------------------------------------


class TestSemverCompare:
    """semver_compare returns 0 (equal), 1 (first > second), 2 (first < second)."""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            # Equal versions
            ("v1.0.0", "v1.0.0", 0),
            ("v0.1.0", "v0.1.0", 0),
            # Major differences
            ("v1.0.0", "v2.0.0", 2),
            ("v2.0.0", "v1.0.0", 1),
            # Minor differences
            ("v1.0.0", "v1.1.0", 2),
            ("v1.1.0", "v1.0.0", 1),
            # Patch differences
            ("v1.0.0", "v1.0.1", 2),
            ("v1.0.1", "v1.0.0", 1),
            # Numeric vs lexicographic (v1.9 < v1.10)
            ("v1.9.0", "v1.10.0", 2),
            ("v1.10.0", "v1.9.0", 1),
            # Without v prefix
            ("1.2.3", "v1.2.3", 0),
            ("v1.2.3", "1.2.3", 0),
            # Pre-release suffix stripped
            ("v1.0.0-rc1", "v1.0.0", 0),
            ("v1.0.0", "v1.0.0-beta2", 0),
            # Missing patch component defaults to 0
            ("v1.0", "v1.0.0", 0),
            # Complex comparison
            ("v2.0.0", "v1.9.9", 1),
            ("v0.9.9", "v1.0.0", 2),
        ],
        ids=[
            "equal-full",
            "equal-minor",
            "major-less",
            "major-greater",
            "minor-less",
            "minor-greater",
            "patch-less",
            "patch-greater",
            "numeric-not-lexicographic",
            "numeric-not-lexicographic-reverse",
            "no-v-prefix-first",
            "no-v-prefix-second",
            "prerelease-stripped-first",
            "prerelease-stripped-second",
            "missing-patch",
            "major-beats-minor-patch",
            "rollover",
        ],
    )
    def test_comparison(self, a: str, b: str, expected: int) -> None:
        assert run_semver_compare(a, b) == expected


# -- latest_remote_tag --------------------------------------------------------


class TestLatestRemoteTag:
    """latest_remote_tag returns the highest v* tag by version sort."""

    def test_returns_highest_tag(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        add_tags(repo, "v0.1.0", "v0.2.0", "v1.0.0", "v0.9.0")
        result = run_function("latest_remote_tag", repo=repo)
        assert result.stdout.strip() == "v1.0.0"

    def test_numeric_sort_not_lexicographic(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        add_tags(repo, "v1.9.0", "v1.10.0", "v1.2.0")
        result = run_function("latest_remote_tag", repo=repo)
        assert result.stdout.strip() == "v1.10.0"

    def test_filters_non_version_tags(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        add_tags(repo, "v1.0.0", "beta-1", "release-candidate")
        result = run_function("latest_remote_tag", repo=repo)
        assert result.stdout.strip() == "v1.0.0"

    def test_empty_when_no_tags(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        result = run_function("latest_remote_tag", repo=repo)
        assert result.stdout.strip() == ""

    def test_empty_when_only_non_version_tags(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        add_tags(repo, "beta-1", "test-deploy")
        result = run_function("latest_remote_tag", repo=repo)
        assert result.stdout.strip() == ""


# -- suggest_update -----------------------------------------------------------


class TestSuggestUpdate:
    """suggest_update prints a formatted update message."""

    def test_contains_message(self) -> None:
        result = run_function('suggest_update "v1.0.0 → v2.0.0"')
        assert "Update available" in result.stdout
        assert "gimmes update" in result.stdout


# -- check_update_release -----------------------------------------------------


class TestCheckUpdateRelease:
    """check_update_release compares local vs remote tags."""

    def test_behind_shows_update(self) -> None:
        result = run_function("check_update_release v1.0.0 v2.0.0")
        assert "Update available" in result.stdout
        assert "v1.0.0" in result.stdout
        assert "v2.0.0" in result.stdout

    def test_up_to_date(self) -> None:
        result = run_function("check_update_release v2.0.0 v2.0.0")
        assert "Up to date" in result.stdout

    def test_ahead_shows_up_to_date(self) -> None:
        result = run_function("check_update_release v3.0.0 v2.0.0")
        assert "Up to date" in result.stdout

    def test_non_semver_local_suggests_update(self) -> None:
        result = run_function("check_update_release dev v1.0.0")
        assert "Update available" in result.stdout
        assert "v1.0.0" in result.stdout

    def test_non_semver_sha_suggests_update(self) -> None:
        result = run_function("check_update_release abc1234 v1.0.0")
        assert "Update available" in result.stdout


# -- check_update_dev ---------------------------------------------------------


class TestCheckUpdateDev:
    """check_update_dev uses commit count when no remote tags exist."""

    def _make_dev_repo(self, tmp_path: Path, ahead_count: int = 0) -> Path:
        """Create a repo where origin/main is *ahead_count* commits ahead of HEAD."""
        repo = tmp_path / "repo"
        init_repo(repo)
        run_setup(f'git -C "{repo}" branch -M main')

        if ahead_count == 0:
            run_setup(f'git -C "{repo}" update-ref refs/remotes/origin/main HEAD')
            return repo

        # Save local HEAD, add commits, set origin/main, then reset back
        run_setup(
            f'local_ref=$(git -C "{repo}" rev-parse HEAD)'
            + "".join(
                f' && git -C "{repo}" commit --allow-empty -m "ahead{i}" --quiet'
                for i in range(ahead_count)
            )
            + f' && git -C "{repo}" update-ref refs/remotes/origin/main HEAD'
            + f' && git -C "{repo}" reset --hard "$local_ref" --quiet'
        )
        return repo

    def test_up_to_date(self, tmp_path: Path) -> None:
        repo = self._make_dev_repo(tmp_path, ahead_count=0)
        result = run_function("check_update_dev", repo=repo)
        assert "Up to date" in result.stdout

    def test_behind_singular(self, tmp_path: Path) -> None:
        repo = self._make_dev_repo(tmp_path, ahead_count=1)
        result = run_function("check_update_dev", repo=repo)
        assert "Update available" in result.stdout
        assert "1 commit ahead" in result.stdout

    def test_behind_plural(self, tmp_path: Path) -> None:
        repo = self._make_dev_repo(tmp_path, ahead_count=3)
        result = run_function("check_update_dev", repo=repo)
        assert "Update available" in result.stdout
        assert "3 commits ahead" in result.stdout


# -- _post-update venv bypass ------------------------------------------------


class TestPostUpdateVenvBypass:
    """The venv check should not block _post-update (it creates the venv)."""

    def test_venv_check_blocks_normal_commands(self, tmp_path: Path) -> None:
        """Without _post-update, a missing venv causes exit 1."""
        home = tmp_path / "gimmes_home"
        repo = home / "repo"
        repo.mkdir(parents=True)
        init_repo(repo)
        result = run_bash(
            f'export GIMMES_HOME="{home}"; '
            f'bash "{GIMMES_SH}" version'
        )
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "virtual environment not found" in combined

    def test_venv_check_bypassed_for_post_update(self, tmp_path: Path) -> None:
        """_post-update skips the venv check so uv sync can create it."""
        home = tmp_path / "gimmes_home"
        repo = home / "repo"
        repo.mkdir(parents=True)
        init_repo(repo)
        # The script will fail at uv sync (no real project), but it should
        # NOT fail at the venv check
        result = run_bash(
            f'export GIMMES_HOME="{home}"; '
            f'bash "{GIMMES_SH}" _post-update v1.0.0 2>&1'
        )
        combined = result.stdout + result.stderr
        assert "virtual environment not found" not in combined
