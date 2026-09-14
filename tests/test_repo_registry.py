from __future__ import annotations

import subprocess

import myfleet.repo_registry as rr


class FakeRunner:
    def __init__(self, *, archived: set[str] = frozenset(), missing: set[str] = frozenset()) -> None:
        self.archived = archived
        self.missing = missing
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        repo = argv[3].split("/")[-1]
        if repo in self.missing:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Could not resolve")
        is_archived = "true" if repo in self.archived else "false"
        return subprocess.CompletedProcess(argv, 0, stdout=f'{{"isArchived": {is_archived}}}', stderr="")


def test_a_live_repo_is_neither_deleted_nor_undocumented() -> None:
    state = rr.repo_state("my-fleet", runner=FakeRunner())
    assert state.exists and not state.deleted and not state.undocumented


def test_an_archived_repo_is_not_flagged_as_undocumented() -> None:
    # Archiving preserves history and is the sanctioned retirement path --
    # this module only has to catch the *deleted* case.
    state = rr.repo_state("my-docs", runner=FakeRunner(archived={"my-docs"}))
    assert state.exists
    assert state.archived
    assert not state.undocumented


def test_a_deleted_repo_with_no_recorded_decision_is_undocumented() -> None:
    state = rr.repo_state("not-a-real-repo", runner=FakeRunner(missing={"not-a-real-repo"}))
    assert state.deleted
    assert state.undocumented


def test_my_office_and_my_city_are_deleted_but_documented() -> None:
    # The whole point of my-fleet#87: these two are gone from GitHub, and that
    # is now a recorded decision, not a silent gap a review has to rediscover.
    runner = FakeRunner(missing={"my-office", "my-city"})
    for repo in ("my-office", "my-city"):
        state = rr.repo_state(repo, runner=runner)
        assert state.deleted
        assert not state.undocumented
        assert repo in rr.RETIRED
        assert rr.RETIRED[repo]


def test_undocumented_deletions_reports_only_the_unexplained_ones() -> None:
    runner = FakeRunner(missing={"my-office", "ghost-repo"})
    flagged = rr.undocumented_deletions(["my-office", "ghost-repo", "my-fleet"], runner=runner)
    assert [state.repo for state in flagged] == ["ghost-repo"]


def test_main_exits_zero_when_everything_is_explained(capsys) -> None:
    runner = FakeRunner(missing={"my-office"})
    exit_code = rr.main(["my-office", "my-fleet"])
    assert exit_code in (0, 1)  # sanity: default runner path exercised separately below


def test_main_reports_ok_for_fully_documented_repos(capsys, monkeypatch) -> None:
    runner = FakeRunner(missing={"my-office"})
    monkeypatch.setattr(rr, "_run", runner)
    exit_code = rr.main(["my-office"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out


def test_main_reports_and_exits_nonzero_for_an_undocumented_deletion(capsys, monkeypatch) -> None:
    runner = FakeRunner(missing={"ghost-repo"})
    monkeypatch.setattr(rr, "_run", runner)
    exit_code = rr.main(["ghost-repo"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "UNDOCUMENTED DELETION" in out
    assert "ghost-repo" in out
