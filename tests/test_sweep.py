from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from myfleet import sweep

WORKFLOW = """\
name: ci
on:
  push:
    branches: [main]
    paths-ignore: ["**.md", "docs/**", "dev-ledger/**"]
  pull_request:
    branches: [main]
    paths-ignore: ["**.md", "docs/**", "dev-ledger/**"]
jobs:
  test:
    runs-on: ubuntu-latest
"""


def make_repo(root: Path, name: str, *, harness: str = "old", workflow: str | None = None) -> Path:
    repo = root / name
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "HARNESS.md").write_text(harness, encoding="utf-8")
    if workflow is not None:
        (repo / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        # Repo-local config, not `-c` on this one commit: `apply_to` makes a
        # commit of its own inside a worktree, and a worktree inherits the
        # repo's config. Without this the suite passes on any dev box with a
        # global git identity and fails on a CI runner, which has none -- the
        # commit dies on "Author identity unknown" and every assertion about
        # the execute path reads `failed` instead of `opened`.
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "--all"],
        ["git", "commit", "-qm", "init"],
        # A sweep works off origin/main, so the fixture needs one. Pointing the
        # remote-tracking ref at the repo's own HEAD is enough for `git worktree
        # add origin/main` without standing up a second bare repo.
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    return repo


def fake_runner(log: list[list[str]], *, pr_list: str = "[]", fail: str | None = None):
    """Runs git for real; fakes every `gh` call so no network is touched."""

    def run(argv: list[str], cwd: Path) -> tuple[int, str]:
        log.append(argv)
        if fail is not None and argv[:2] == fail.split()[:2]:
            return 1, f"boom: {' '.join(argv)}"
        if argv[0] == "gh":
            if argv[1:3] == ["pr", "list"]:
                return 0, pr_list
            if argv[1:3] == ["pr", "create"]:
                return 0, "https://github.com/MyThingsLab/x/pull/7"
            return 0, ""
        if argv[:2] == ["git", "fetch"] or argv[:2] == ["git", "push"]:
            return 0, ""  # no remote in the fixture
        return sweep._run(argv, cwd)

    return run


def harness_transform(text: str = "new") -> sweep.Transform:
    return sweep.Transform(
        name="t",
        title="docs: test sweep",
        body="body",
        apply=lambda repo: (repo / "HARNESS.md").write_text(text, encoding="utf-8"),
    )


def test_a_repo_already_carrying_the_change_is_current(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a", harness="new")
    out = sweep.apply_to(
        repo, harness_transform(), execute=False, allow_unchecked=False, runner=fake_runner([])
    )
    assert out.state == "current"


def test_a_dry_run_reports_the_change_without_pushing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a")
    log: list[list[str]] = []

    out = sweep.apply_to(
        repo, harness_transform(), execute=False, allow_unchecked=False, runner=fake_runner(log)
    )

    assert out.state == "would_change"
    assert out.changed == ["HARNESS.md"]
    assert not any(argv[:2] == ["git", "push"] for argv in log)
    assert not any(argv[1:3] == ["pr", "create"] for argv in log)


def test_a_dry_run_leaves_the_real_checkout_untouched(tmp_path: Path) -> None:
    # The whole reason this works in a throwaway worktree: a sweep must never
    # edit the tree you have open or move the branch you had checked out.
    repo = make_repo(tmp_path, "my-a")
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout

    sweep.apply_to(
        repo, harness_transform(), execute=False, allow_unchecked=False, runner=fake_runner([])
    )

    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert before == after == ""
    assert (repo / "HARNESS.md").read_text() == "old"


def test_execute_commits_and_opens_one_pr(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a")
    log: list[list[str]] = []

    out = sweep.apply_to(
        repo, harness_transform(), execute=True, allow_unchecked=False, runner=fake_runner(log)
    )

    assert out.state == "opened"
    assert out.url.endswith("/pull/7")
    assert [argv for argv in log if argv[1:3] == ["pr", "create"]]
    pushed = [argv for argv in log if argv[:2] == ["git", "push"]]
    assert len(pushed) == 1
    assert "sweep/t" in pushed[0]


def test_an_existing_open_pr_is_not_reopened(tmp_path: Path) -> None:
    # Re-running a half-landed sweep is the normal case, not an error.
    repo = make_repo(tmp_path, "my-a")
    log: list[list[str]] = []
    runner = fake_runner(log, pr_list='[{"url": "https://github.com/x/y/pull/3"}]')

    out = sweep.apply_to(
        repo, harness_transform(), execute=True, allow_unchecked=False, runner=runner
    )

    assert out.state == "in_flight"
    assert out.url.endswith("/pull/3")
    assert not any(argv[1:3] == ["pr", "create"] for argv in log)


def test_a_pr_ci_would_skip_is_refused_by_default(tmp_path: Path) -> None:
    # The deadlock: `paths-ignore: ["**.md"]` means a HARNESS.md-only PR runs no
    # workflow, so it gets no `test` check, so a required-check branch protection
    # can never merge it -- and `myfleet.accept` correctly refuses it too.
    repo = make_repo(tmp_path, "my-a", workflow=WORKFLOW)
    log: list[list[str]] = []

    out = sweep.apply_to(
        repo, harness_transform(), execute=True, allow_unchecked=False, runner=fake_runner(log)
    )

    assert out.state == "ci_blocked"
    assert "paths-ignore" in out.detail
    assert not any(argv[:2] == ["git", "push"] for argv in log)


def test_allow_unchecked_opens_it_anyway(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a", workflow=WORKFLOW)

    out = sweep.apply_to(
        repo, harness_transform(), execute=True, allow_unchecked=True, runner=fake_runner([])
    )

    assert out.state == "opened"


def test_a_repo_whose_ci_checks_markdown_is_not_blocked(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a", workflow=WORKFLOW.replace('"**.md", ', ""))

    out = sweep.apply_to(
        repo, harness_transform(), execute=False, allow_unchecked=False, runner=fake_runner([])
    )

    assert out.state == "would_change"


def test_a_failed_push_is_reported_not_raised(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a")

    out = sweep.apply_to(
        repo,
        harness_transform(),
        execute=True,
        allow_unchecked=False,
        runner=fake_runner([], fail="git push"),
    )

    assert out.state == "failed"
    assert "git push" in out.detail


def test_the_scratch_worktree_is_always_cleaned_up(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a")

    sweep.apply_to(
        repo,
        harness_transform(),
        execute=True,
        allow_unchecked=False,
        runner=fake_runner([], fail="git push"),
    )

    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "sweep-t" not in listed


def test_an_archived_repo_is_skipped_not_failed(tmp_path: Path) -> None:
    # An archived repo is read-only: the push cannot succeed now or ever.
    # Reported as `failed` it makes a permanent condition look transient and
    # buries the real failures under it on every run. Four of these turned the
    # first real fleet sweep's summary from readable into noise.
    repo = make_repo(tmp_path, "my-a")
    log: list[list[str]] = []

    out = sweep.apply_to(
        repo,
        harness_transform(),
        execute=True,
        allow_unchecked=False,
        archived=frozenset({"my-a"}),
        runner=fake_runner(log),
    )

    assert out.state == "archived"
    assert log == []  # not even a `gh pr list` is worth spending on it


def test_archived_repos_come_from_one_org_wide_call(tmp_path: Path) -> None:
    log: list[list[str]] = []

    def runner(argv: list[str], cwd: Path) -> tuple[int, str]:
        log.append(argv)
        return 0, '[{"name":"my-old","isArchived":true},{"name":"my-new","isArchived":false}]'

    assert sweep.archived_repos("MyThingsLab", runner=runner, cwd=tmp_path) == {"my-old"}
    assert len(log) == 1  # one call for the whole org, not one per repo


def test_archived_lookup_failing_does_not_stop_the_sweep(tmp_path: Path) -> None:
    # Losing the archived list should cost us a clean skip, not the whole run.
    assert sweep.archived_repos("x", runner=lambda a, c: (1, "boom"), cwd=tmp_path) == set()
    assert sweep.archived_repos("x", runner=lambda a, c: (0, "not json"), cwd=tmp_path) == set()


def test_the_commit_can_see_the_shared_venv(tmp_path: Path, monkeypatch) -> None:
    # The fleet's pre-commit hooks are `language: system` (`ruff check`,
    # `pytest -q`). A scratch worktree activates no virtualenv, so they resolved
    # against a bare PATH and the commit died on a conftest
    # `ModuleNotFoundError: mythings` -- which reads as the sweep having broken
    # the repo. This is what stopped 17 of the first real ci-md sweep's repos.
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(sweep, "VENV_BIN", venv_bin)

    assert sweep._env()["PATH"].startswith(f"{venv_bin}:")


def test_a_missing_venv_leaves_path_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sweep, "VENV_BIN", tmp_path / "nope" / "bin")
    monkeypatch.setenv("PATH", "/usr/bin")

    assert sweep._env()["PATH"] == "/usr/bin"


@pytest.mark.parametrize(
    ("pattern", "path", "ignored"),
    [
        ("**.md", "HARNESS.md", True),
        ("**.md", "docs/deep/x.md", True),
        ("**.md", "src/a.py", False),
        ("docs/**", "docs/a.md", True),
        ("docs/**", "src/a.md", False),
        ("AGENTS.md", "AGENTS.md", True),
        ("weird[!", "HARNESS.md", False),  # unrecognised -> assume CI runs
    ],
)
def test_paths_ignore_glob_semantics(pattern: str, path: str, ignored: bool) -> None:
    assert sweep._ignored_by(pattern, path) is ignored


def test_ci_md_transform_drops_only_the_markdown_pattern(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "my-a", workflow=WORKFLOW)

    sweep.TRANSFORMS["ci-md"].apply(repo)

    text = (repo / ".github" / "workflows" / "ci.yml").read_text()
    assert '"**.md"' not in text
    assert '"docs/**"' in text and '"dev-ledger/**"' in text
    assert text.count("paths-ignore") == 2  # both triggers kept, neither dropped


def test_ci_md_transform_leaves_an_unfamiliar_workflow_alone(tmp_path: Path) -> None:
    # A regex that half-understands a CI trigger is how a repo ends up with no CI.
    odd = "on:\n  push:\n    paths-ignore:\n      - '**.md'\n"
    repo = make_repo(tmp_path, "my-a", workflow=odd)

    sweep.TRANSFORMS["ci-md"].apply(repo)

    assert (repo / ".github" / "workflows" / "ci.yml").read_text() == odd


def test_sweep_repos_finds_sibling_checkouts_and_rejects_unknown_names(tmp_path: Path) -> None:
    make_repo(tmp_path, "my-a")
    make_repo(tmp_path, "my-b")
    (tmp_path / "not-a-tool").mkdir()

    assert [r.name for r in sweep.sweep_repos(tmp_path)] == ["my-a", "my-b"]
    assert [r.name for r in sweep.sweep_repos(tmp_path, ["my-b"])] == ["my-b"]
    with pytest.raises(SystemExit):
        sweep.sweep_repos(tmp_path, ["my-nope"])
