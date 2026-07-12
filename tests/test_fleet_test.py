from __future__ import annotations

import sys
from pathlib import Path

import pytest

import myfleet.fleet_test as ft


def make_workspace(tmp_path: Path, *repos: str, without_tests: tuple[str, ...] = ()) -> Path:
    for name in repos:
        (tmp_path / name / "tests").mkdir(parents=True)
        (tmp_path / name / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    for name in without_tests:
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return tmp_path


def test_test_repos_discovers_only_repos_with_tests(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, "my-b", "my-a", without_tests=("my-scripts",))
    (root / "not-a-repo").mkdir()
    assert [r.name for r in ft.test_repos(root)] == ["my-a", "my-b"]


def test_test_repos_only_filter_keeps_requested_order(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, "my-a", "my-b", "my-c")
    assert [r.name for r in ft.test_repos(root, ["my-c", "my-a"])] == ["my-c", "my-a"]


def test_test_repos_unknown_only_fails_loudly(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, "my-a")
    with pytest.raises(SystemExit, match="no such test repo"):
        ft.test_repos(root, ["my-a", "my-nope"])


def test_run_suites_argv_env_and_cwd(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, "my-a")
    seen: list[tuple[list[str], Path, dict[str, str]]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
        seen.append((argv, cwd, env))
        return 0

    results = ft.run_suites(ft.test_repos(root), runner=runner)
    argv, cwd, env = seen[0]
    assert argv == [sys.executable, "-m", "pytest", "tests", "-q", "-m", "not slow"]
    assert cwd == root / "my-a"
    # No src/ dir in this fake repo, so no PYTHONPATH override.
    assert env.get("PYTHONPATH") == ft.os.environ.get("PYTHONPATH")
    # Pass-through, not an override: the attended path inherits the caller's
    # env verbatim (including GITHUB_ACTIONS when fleet_test runs inside CI).
    assert env.get("GITHUB_ACTIONS") == ft.os.environ.get("GITHUB_ACTIONS")
    assert results == [ft.RepoResult("my-a", True, results[0].duration)]


def test_run_suites_unattended_and_slow_flags(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, "my-a")
    seen: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
        seen.append((argv, env))
        return 1

    results = ft.run_suites(
        ft.test_repos(root), unattended=True, include_slow=True, runner=runner
    )
    argv, env = seen[0]
    assert "not slow" not in argv
    assert env["GITHUB_ACTIONS"] == "true"
    assert not results[0].passed


def test_run_suites_puts_the_checkout_src_first_on_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_workspace(tmp_path, "my-a")
    (root / "my-a" / "src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    seen: list[dict[str, str]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
        seen.append(env)
        return 0

    ft.run_suites(ft.test_repos(root), runner=runner)
    assert seen[0]["PYTHONPATH"] == f"{root / 'my-a' / 'src'}:/elsewhere"


def test_summarize_reports_failures() -> None:
    out = ft.summarize(
        [ft.RepoResult("my-a", True, 1.0), ft.RepoResult("my-b", False, 2.0)]
    )
    assert "1/2 suites green" in out
    assert "FAILED: my-b" in out
    assert "my-a" in out and "ok" in out


def test_check_editable_core_accepts_the_serving_checkout() -> None:
    import mythings

    resolved = Path(mythings.__file__).resolve().parent
    if resolved.parts[-3:] != ("my-things-core", "src", "mythings"):
        pytest.skip("mythings is not a workspace editable install here (e.g. CI site-packages)")
    assert ft.check_editable_core(resolved.parents[2]) is None


def test_check_editable_core_rejects_other_roots(tmp_path: Path) -> None:
    error = ft.check_editable_core(tmp_path)
    assert error is not None and "mythings resolves to" in error


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    root = make_workspace(tmp_path, "my-a", "my-b")
    monkeypatch.setattr(ft, "check_editable_core", lambda _root: None)
    codes = {"my-a": 0, "my-b": 1}
    monkeypatch.setattr(
        ft, "default_runner", lambda argv, cwd, env: codes[cwd.name]
    )
    assert ft.main(["--root", str(root)]) == 1
    assert "FAILED: my-b" in capsys.readouterr().out

    assert ft.main(["--root", str(root), "--only", "my-a"]) == 0


def test_main_fails_fast_on_wrong_core(tmp_path: Path, capsys) -> None:
    root = make_workspace(tmp_path, "my-a")
    assert ft.main(["--root", str(root)]) == 2
    assert "mythings resolves to" in capsys.readouterr().err
