from __future__ import annotations

import json

from mythings.policy import Action, Decision, PolicyResult

import myfleet.red_main as rm

ORG = rm.ORG


class FakeRunner:
    """Answers `gh` by subcommand; records every argv for assertions."""

    def __init__(self, *, runs: dict[str, list[dict]], issues: dict[str, list[dict]]) -> None:
        self.runs = runs
        self.issues = issues
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        repo = argv[argv.index("--repo") + 1].split("/")[-1] if "--repo" in argv else ""
        head = argv[1:3]
        if head == ["run", "list"]:
            return json.dumps(self.runs.get(repo, []))
        if head == ["issue", "list"]:
            return json.dumps(self.issues.get(repo, []))
        if head == ["issue", "create"]:
            return f"https://github.com/{ORG}/{repo}/issues/99\n"
        return ""

    def argv_for(self, *head: str) -> list[list[str]]:
        return [c for c in self.calls if c[1 : 1 + len(head)] == list(head)]


class StubPolicy:
    def __init__(self, decision: Decision = Decision.ALLOW) -> None:
        self.decision = decision
        self.seen: list[Action] = []

    def evaluate(self, action: Action) -> PolicyResult:
        self.seen.append(action)
        return PolicyResult(self.decision)


def _red(sha: str = "abc1234") -> list[dict]:
    return [
        {
            "conclusion": "failure",
            "displayTitle": "Merge pull request #157",
            "url": "https://example/run/1",
            "headSha": sha,
        }
    ]


def _green() -> list[dict]:
    return [{"conclusion": "success", "displayTitle": "ok", "url": "u", "headSha": "def5678"}]


def test_a_red_main_becomes_a_p0_not_a_backlog_item() -> None:
    # The whole reason this module exists: core#157 reddened three kernel mains
    # and no issue existed at all for a day. Filing it is only useful if the
    # queue puts it ahead of feature work, which means prio:P0.
    assert "prio:P0" in rm.issue_labels("lane:kernel")
    assert "prio:P0" in rm.issue_labels("lane:core")


def test_the_priority_comes_from_the_labels_seam_not_a_local_opinion() -> None:
    # No literal "prio:P0" is passed in -- escalate() derives it from
    # lane + kind:bug, so this watcher cannot drift from the fleet-wide rule.
    labels = rm.issue_labels("lane:kernel")
    assert "kind:bug" in labels
    assert rm.MARKER_LABEL in labels


def test_only_an_outright_failure_counts_as_red() -> None:
    # A cancelled or still-running main is absence of evidence. Filing a P0 on
    # it is how a priority label stops being believed.
    assert rm.MainStatus("r", "failure").red
    for conclusion in ("success", "cancelled", "skipped", "in-progress", "none"):
        assert not rm.MainStatus("r", conclusion).red


def test_an_in_progress_run_reports_no_conclusion_rather_than_crashing() -> None:
    runner = FakeRunner(runs={"my-fleet": [{"conclusion": None, "headSha": "a"}]}, issues={})
    assert rm.main_status("my-fleet", runner=runner).conclusion == "in-progress"


def test_a_red_main_with_no_open_issue_files_one() -> None:
    runner = FakeRunner(runs={"my-fleet": _red()}, issues={"my-fleet": []})
    policy = StubPolicy()

    outcomes = rm.sweep({"my-fleet": "lane:kernel"}, policy=policy, execute=True, runner=runner)

    assert [o.action for o in outcomes] == ["filed"]
    created = runner.argv_for("issue", "create")[0]
    assert "prio:P0" in created
    assert rm.MARKER_LABEL in created


def test_a_red_main_that_already_has_an_issue_files_nothing() -> None:
    # Run on a timer, so re-filing every tick would bury the repo in duplicates
    # and make the label worthless.
    runner = FakeRunner(runs={"my-fleet": _red()}, issues={"my-fleet": [{"number": 7}]})
    policy = StubPolicy()

    outcomes = rm.sweep({"my-fleet": "lane:kernel"}, policy=policy, execute=True, runner=runner)

    assert [(o.action, o.detail) for o in outcomes] == [("already-open", "#7")]
    assert runner.argv_for("issue", "create") == []


def test_recovery_closes_the_issue_so_the_marker_means_something() -> None:
    runner = FakeRunner(runs={"my-fleet": _green()}, issues={"my-fleet": [{"number": 7}]})
    policy = StubPolicy()

    outcomes = rm.sweep({"my-fleet": "lane:kernel"}, policy=policy, execute=True, runner=runner)

    assert [o.action for o in outcomes] == ["recovered"]
    assert runner.argv_for("issue", "close")[0][3] == "7"


def test_a_green_main_with_nothing_open_does_nothing_at_all() -> None:
    runner = FakeRunner(runs={"my-fleet": _green()}, issues={"my-fleet": []})
    outcomes = rm.sweep(
        {"my-fleet": "lane:kernel"}, policy=StubPolicy(), execute=True, runner=runner
    )

    assert [o.action for o in outcomes] == ["ok"]
    assert runner.argv_for("issue", "create") == []
    assert runner.argv_for("issue", "close") == []


def test_a_repo_with_no_ci_run_is_reported_not_assumed_broken() -> None:
    runner = FakeRunner(runs={}, issues={})
    outcomes = rm.sweep(
        {"my-fleet": "lane:kernel"}, policy=StubPolicy(), execute=True, runner=runner
    )

    assert [o.action for o in outcomes] == ["no-runs"]
    assert runner.argv_for("issue", "list") == []  # not even worth the API call


def test_a_dry_run_touches_nothing() -> None:
    runner = FakeRunner(runs={"my-fleet": _red()}, issues={"my-fleet": []})
    outcomes = rm.sweep(
        {"my-fleet": "lane:kernel"}, policy=StubPolicy(), execute=False, runner=runner
    )

    assert [o.action for o in outcomes] == ["filed"]
    assert "[dry run]" in outcomes[0].detail
    assert runner.argv_for("issue", "create") == []


def test_a_denied_policy_files_nothing_and_says_so() -> None:
    runner = FakeRunner(runs={"my-fleet": _red()}, issues={"my-fleet": []})
    policy = StubPolicy(Decision.DENY)

    outcomes = rm.sweep({"my-fleet": "lane:kernel"}, policy=policy, execute=True, runner=runner)

    assert [o.action for o in outcomes] == ["denied"]
    assert runner.argv_for("issue", "create") == []
    assert [a.kind for a in policy.seen] == ["issue-create"]


def test_a_denied_close_leaves_the_issue_open_rather_than_failing() -> None:
    # Main is green either way. An issue left open is visible; a crash here
    # would take out the whole sweep over something harmless.
    runner = FakeRunner(runs={"my-fleet": _green()}, issues={"my-fleet": [{"number": 7}]})
    policy = StubPolicy(Decision.DENY)

    outcomes = rm.sweep({"my-fleet": "lane:kernel"}, policy=policy, execute=True, runner=runner)

    assert outcomes[0].action == "denied"
    assert "#7 still open" in outcomes[0].detail
    assert runner.argv_for("issue", "close") == []


def test_an_ask_with_no_channel_collapses_to_deny() -> None:
    # The fleet-wide rule: unattended, an unanswered ASK must never proceed.
    runner = FakeRunner(runs={"my-fleet": _green()}, issues={"my-fleet": [{"number": 7}]})
    outcomes = rm.sweep(
        {"my-fleet": "lane:kernel"},
        policy=StubPolicy(Decision.ASK),
        execute=True,
        runner=runner,
    )

    assert outcomes[0].action == "denied"
    assert runner.argv_for("issue", "close") == []


def test_every_watched_repo_has_a_lane_escalate_recognizes() -> None:
    # A typo'd lane would silently stop promoting: escalate() only acts on
    # lane:core / lane:kernel and returns everything else untouched.
    for repo, lane in rm.WATCHED.items():
        assert "prio:P0" in rm.issue_labels(lane), repo


def test_unknown_repos_are_refused_rather_than_watched_by_accident() -> None:
    try:
        rm.main(["--repos", "not-a-repo"])
    except SystemExit as exc:
        assert "not watched" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
