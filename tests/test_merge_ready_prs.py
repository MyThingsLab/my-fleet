from __future__ import annotations

import json
from pathlib import Path

import pytest
from mythings.policy import Decision

import myfleet.merge_ready_prs as merge_ready_prs
from myfleet.merge_ready_prs import PR, approve, merge_by_asking

# Merging is the one thing the fleet says only a human may do. It rides the ask
# channel: MyGuard answers the structured `pr-merge` Action with ASK, which is a
# real Allow/Deny prompt on the operator's phone -- and their tap *is* the merge.
#
# The property that matters: nothing merges without an explicit ALLOW.


def _pr(number: int = 1, repo: str = "my-idea") -> PR:
    return PR(
        repo=repo,
        number=number,
        title=f"a change to {repo}",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=[],
        required_state="pass",
    )


class _Guard:
    # Stands in for MyGuard with an ask channel wired: whatever the human "taps".
    def __init__(self, *decisions: Decision) -> None:
        self.decisions = list(decisions)
        self.asked: list[str] = []

    def evaluate(self, action):
        from mythings.policy import PolicyResult

        self.asked.append(f"{action.payload['repo']}#{action.payload['number']}")
        return PolicyResult(self.decisions.pop(0), reason="human", rule="merge_needs_a_human")


@pytest.fixture
def merged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    done: list[str] = []
    monkeypatch.setattr(
        merge_ready_prs, "merge", lambda pr, **kw: done.append(f"{pr.repo}#{pr.number}")
    )
    return done


def test_the_action_carries_what_a_human_needs_to_decide() -> None:
    # The prompt is rendered from this payload. "Action: pr-merge, number: 12" with
    # no repo or title is not something anyone can approve responsibly.
    guard = _Guard(Decision.ALLOW)

    approve(_pr(12, "my-guard"), guard)

    (action,) = [guard.asked[0]]
    assert action == "MyThingsLab/my-guard#12"


def test_an_approved_pr_is_merged(merged: list[str]) -> None:
    assert merge_by_asking([_pr(1)], _Guard(Decision.ALLOW), budget_s=60) == 0
    assert merged == ["my-idea#1"]


def test_a_refused_pr_is_not_merged(merged: list[str]) -> None:
    merge_by_asking([_pr(1)], _Guard(Decision.DENY), budget_s=60)

    assert merged == []


def test_an_unanswered_prompt_is_a_no(merged: list[str]) -> None:
    # A timeout comes back as DENY, indistinguishable from a tap on Deny. Both are
    # a "no", and fail-closed is the only safe reading of silence.
    merge_by_asking([_pr(1)], _Guard(Decision.DENY), budget_s=60)

    assert merged == []


def test_each_pr_is_asked_about_separately(merged: list[str]) -> None:
    # Approving one merge must never approve the next. One tap, one PR.
    guard = _Guard(Decision.ALLOW, Decision.DENY, Decision.ALLOW)

    merge_by_asking([_pr(1), _pr(2), _pr(3)], guard, budget_s=60)

    assert len(guard.asked) == 3
    assert merged == ["my-idea#1", "my-idea#3"]


def test_the_budget_stops_the_pass_rather_than_timing_out_pr_after_pr(
    merged: list[str],
) -> None:
    # Each unanswered ask blocks for the full timeout. A queue of PRs with nobody
    # home would spend the entire pass timing out, one prompt at a time, so the
    # budget caps it -- and the rest are reported *unasked*, not silently denied.
    guard = _Guard(Decision.ALLOW)

    merge_by_asking([_pr(1), _pr(2), _pr(3)], guard, budget_s=0)

    assert guard.asked == []  # the budget was already gone
    assert merged == []


def test_a_merge_that_fails_after_approval_does_not_strand_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[str] = []

    def flaky(pr: PR, **kw: object) -> None:
        if pr.number == 1:
            raise RuntimeError("base branch moved")
        done.append(f"{pr.repo}#{pr.number}")

    monkeypatch.setattr(merge_ready_prs, "merge", flaky)

    code = merge_by_asking([_pr(1), _pr(2)], _Guard(Decision.ALLOW, Decision.ALLOW), budget_s=60)

    assert done == ["my-idea#2"]  # one stuck PR must not strand the queue
    assert code == 1  # but the run is honest about having failed


def test_the_ask_ledger_can_be_pointed_at_the_daemons_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # `ask` and the daemon rendezvous through one ledger file and nothing else. The
    # default is derived relative to this script, which is only correct when this
    # repo *is* the workspace root -- it is not on the Pi, where the tools live under
    # ~/repos/MyThingsLab but this repo is checked out elsewhere. Pointing at the
    # wrong file sends every prompt and sees no tap: every merge would time out and
    # deny, with nothing to say why.
    import myfleet.fleet_ask as fleet_ask

    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    env: dict[str, str] = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}

    fleet_ask.enable(ledger=ledger, env=env)

    assert str(ledger) in env["MYTHINGS_ASK_CMD"]


def test_the_ask_path_actually_parses_and_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    # The gap this closes: every other test calls merge_by_asking() directly, so
    # main()'s --ask branch was never executed. It referenced args.ask_ledger while
    # the flag itself was missing -- an AttributeError on every real run, and CI was
    # perfectly green. Drive main() so the wiring is exercised, not just the pieces.
    import myfleet.fleet_ask as fleet_ask

    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    monkeypatch.setattr(merge_ready_prs, "list_org_repos", lambda org: ["my-idea"])
    monkeypatch.setattr(merge_ready_prs, "list_open_prs", lambda repo: [_pr(7)])
    monkeypatch.setattr(merge_ready_prs, "merge", lambda pr, **kw: None)
    # The human taps Deny, so nothing merges and no gh call is needed.
    monkeypatch.setattr(merge_ready_prs, "approve", lambda pr, guard: Decision.DENY)

    code = merge_ready_prs.main(["--ask", "--ask-ledger", str(ledger), "--ask-budget-min", "1"])

    assert code == 0
    assert str(ledger) in capsys.readouterr().out  # it armed the channel we named


def test_the_ask_path_refuses_when_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import myfleet.fleet_ask as fleet_ask

    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: False)
    monkeypatch.setattr(merge_ready_prs, "list_org_repos", lambda org: ["my-idea"])
    monkeypatch.setattr(merge_ready_prs, "list_open_prs", lambda repo: [_pr(7)])

    # Nobody would see the tap, so every merge would time out and deny. Refuse.
    assert merge_ready_prs.main(["--ask", "--ask-ledger", str(ledger)]) == 2


# --- what "green" means (#37) ---------------------------------------------
#
# `ready` gates the bulk `--execute` path, so a PR that is wrongly ready is a
# PR that merges with nobody looking. Before #37 this module decided that from
# `statusCheckRollup` -- every check on the PR, with SKIPPED counted as fine --
# rather than from the checks branch protection actually requires.


def _pr_with(state: str) -> PR:
    return PR(
        repo="my-idea",
        number=1,
        title="a change",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=[],
        required_state=state,
    )


def test_a_skipped_required_check_is_not_ready() -> None:
    # The #32 regression, on the merge path. A skipped check produced no result;
    # absence of evidence is not evidence.
    pr = _pr_with("skipped")
    assert not pr.ready
    assert "skipped" in pr.reason_not_ready


def test_an_unprotected_main_is_not_ready() -> None:
    # 'none' means nothing is required, so nothing green here is evidence. This
    # is not hypothetical -- my-fleet's own main was unprotected until #35.
    pr = _pr_with("none")
    assert not pr.ready
    assert "unprotected" in pr.reason_not_ready


def test_an_unestablished_state_is_not_ready() -> None:
    # The default fails closed: never having asked must not read as a pass.
    assert not _pr_with("unknown").ready


def test_a_passing_required_check_is_ready() -> None:
    assert _pr_with("pass").ready


def test_green_optional_checks_cannot_make_a_pr_ready(monkeypatch) -> None:
    # The other half of #37: the rollup is not the required set. A PR whose
    # required check skipped, but whose optional jobs are all green, used to
    # look perfectly mergeable.
    pr = PR(
        repo="my-idea",
        number=1,
        title="a change",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=[{"name": "lint", "conclusion": "SUCCESS"}],
        required_state="skipped",
    )
    assert not pr.ready


def test_a_disqualified_pr_costs_no_extra_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # One _checks_state call per PR is a real cost on a 30-PR sweep, and a draft
    # is already not ready however its checks look.
    calls: list[int] = []
    monkeypatch.setattr(
        merge_ready_prs, "_checks_state", lambda org, repo, n: calls.append(n) or "pass"
    )
    monkeypatch.setattr(
        merge_ready_prs,
        "_run",
        lambda argv: json.dumps(
            [
                {
                    "number": 1,
                    "title": "draft",
                    "isDraft": True,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                },
                {
                    "number": 2,
                    "title": "ready",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                },
            ]
        ),
    )
    prs = merge_ready_prs.list_open_prs("my-idea")
    assert calls == [2]
    assert prs[0].required_state == "unknown"
    assert prs[1].required_state == "pass"
