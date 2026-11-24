from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mythings import paths
from mythings.ledger import Ledger
from mythings.testing import make_git_repo

from myfleet import local_lane

_CORE = {
    "src/mythings/__init__.py": "",
    "src/mythings/github.py": "def gh(argv):\n    return 'ok'\n\n\n_gh = gh\n",
}
_TOOL = "from mythings.github import _gh\n\n\ndef ask():\n    return _gh(['x'])\n"
_TEST = "from tool.ask import ask\n\n\ndef test_ask():\n    assert ask() == 'ok'\n"


@pytest.fixture(autouse=True)
def lab_python(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite under the lane runs with the same Python as this one (pytest is there).
    import sys

    monkeypatch.setattr(paths, "lab_python", lambda: Path(sys.executable))


def _core(root: Path) -> Path:
    for name, text in _CORE.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _tool(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    files = {
        "src/tool/__init__.py": "",
        "src/tool/ask.py": _TOOL,
        "tests/test_ask.py": _TEST,
        **(extra or {}),
    }
    return make_git_repo(tmp_path / "tool", files=files).path


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
    ).stdout


def test_a_change_that_keeps_the_suite_is_committed_on_a_local_branch(tmp_path: Path) -> None:
    repo = _tool(tmp_path)

    outcome = local_lane.change_one(repo, _core(tmp_path / "core"))

    assert outcome.outcome == "success" and outcome.files == ("src/tool/ask.py",)
    assert "from mythings.github import gh\n" in _git(
        repo, "show", f"{local_lane.BRANCH}:src/tool/ask.py"
    )
    assert "return gh(['x'])" in _git(repo, "show", f"{local_lane.BRANCH}:src/tool/ask.py")
    assert "_gh" in _git(repo, "show", "main:src/tool/ask.py")  # main is untouched
    assert _git(repo, "worktree", "list").count("\n") == 1  # the lane's worktree is gone
    assert local_lane.BRANCH not in _git(repo, "ls-remote", "--heads", "origin")  # not pushed


def test_a_change_that_breaks_a_passing_test_leaves_no_branch(tmp_path: Path) -> None:
    # A test that patches the tool's own `_gh` by name breaks when the name goes away.
    patched = (
        "import tool.ask\n\n\ndef test_patched(monkeypatch):\n"
        "    monkeypatch.setattr('tool.ask._gh', lambda argv: 'stub')\n"
        "    assert tool.ask.ask() == 'stub'\n"
    )
    repo = _tool(tmp_path, {"tests/test_patched.py": patched})

    outcome = local_lane.change_one(repo, _core(tmp_path / "core"))

    assert outcome.outcome == "failed"
    assert outcome.new_failures == ("tests/test_patched.py::test_patched",)
    assert local_lane.BRANCH not in _git(repo, "branch")


def test_a_test_already_failing_on_main_does_not_block_the_change(tmp_path: Path) -> None:
    red = "def test_stale():\n    assert False\n"
    repo = _tool(tmp_path, {"tests/test_stale.py": red})

    outcome = local_lane.change_one(repo, _core(tmp_path / "core"))

    assert outcome.outcome == "success" and "1 failing before and after" in outcome.detail


def test_a_branch_waiting_for_review_is_not_made_twice(tmp_path: Path) -> None:
    repo = _tool(tmp_path)
    _git(repo, "branch", local_lane.BRANCH)

    outcome = local_lane.change_one(repo, _core(tmp_path / "core"))

    assert outcome.outcome == "skipped" and "waits for review" in outcome.detail


def test_the_dry_run_reads_main_and_changes_nothing(tmp_path: Path) -> None:
    repo = _tool(tmp_path)

    outcome = local_lane.plan_one(repo)

    assert (outcome.outcome, outcome.files) == ("planned", ("src/tool/ask.py",))
    assert _git(repo, "branch").strip() == "* main"


def test_the_suite_report_names_what_failed_and_a_broken_collection() -> None:
    out = "FAILED tests/test_a.py::test_x - assert 1\nERROR tests/test_b.py - ImportError\n"

    assert local_lane.parse_suite(out, 1).failed == {"tests/test_a.py::test_x", "tests/test_b.py"}
    assert "<collection>" in local_lane.parse_suite("", 2).failed
    assert local_lane.parse_suite("", 5).failed == frozenset()


def _lab(tmp_path: Path) -> Path:
    lab = tmp_path / "lab"
    _core(lab / "my-things-core")
    files = {
        "pyproject.toml": "[project]\nname = 'tool'\n",
        "src/tool/__init__.py": "",
        "src/tool/ask.py": _TOOL,
        "tests/test_ask.py": _TEST,
    }
    for name in ("my-a", "my-b"):
        make_git_repo(tmp_path / name, files=files).path.rename(lab / name)
    return lab


def test_the_command_runs_the_lane_on_each_repository_a_public_name_gap_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = _lab(tmp_path)
    core = lab / "my-things-core"
    monkeypatch.setenv("MYTHINGS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("MYTHINGS_GRAPH_CACHE", str(tmp_path / "cache"))

    assert local_lane.main(["--core", str(core), "--lab", str(lab)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "my-a: planned: would change 1 file(s)",
        "my-b: planned: would change 1 file(s)",
    ]

    assert (
        local_lane.main(["--core", str(core), "--lab", str(lab), "--repos", "my-b", "--execute"])
        == 0
    )
    assert capsys.readouterr().out.startswith("my-b: success: 1 file(s) on lane/public-core-names")
    (entry,) = Ledger(tmp_path / ".my-fleet" / "ledger.jsonl")
    assert (entry.tool, entry.outcome, entry.data["repo"]) == ("local_lane", "success", "my-b")
