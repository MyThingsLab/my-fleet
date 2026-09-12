from __future__ import annotations

import json

import pytest

from myfleet.accept import (
    REPO_CARVE_OUTS,
    SHARED_CARVE_OUTS,
    Assessment,
    Check,
    Verdict,
    assess,
    carve_out_for,
    closing_issue,
    main_is_protected,
)


def _assessment(*checks: Check) -> Assessment:
    return Assessment(repo="my-fleet", number=1, checks=list(checks))


class TestVerdict:
    def test_all_passing_is_accepted(self) -> None:
        found = _assessment(Check("a", True, ""), Check("b", True, ""))
        assert found.verdict is Verdict.ACCEPTED

    def test_a_failure_rejects(self) -> None:
        found = _assessment(Check("a", True, ""), Check("b", False, "nope"))
        assert found.verdict is Verdict.REJECTED

    def test_an_unevaluable_check_needs_a_human(self) -> None:
        found = _assessment(Check("a", True, ""), Check("b", None, "could not tell"))
        assert found.verdict is Verdict.NEEDS_HUMAN

    def test_evidence_of_failure_outranks_not_knowing(self) -> None:
        # A PR that is both broken and unclear is broken: that verdict is
        # actionable by the worker, where needs_human parks it on a person.
        found = _assessment(Check("a", None, "unclear"), Check("b", False, "broken"))
        assert found.verdict is Verdict.REJECTED


class TestCarveOuts:
    @pytest.mark.parametrize(
        "path",
        [
            "src/myfleet/accept.py",
            "src/myfleet/merge_ready_prs.py",
            "CLAUDE.md",
            "some/nested/CLAUDE.md",
            ".github/workflows/ci.yml",
            ".claude/settings.local.json",
            "src/mythings/_secrets.py",
        ],
    )
    def test_protected_paths_are_caught(self, path: str) -> None:
        assert carve_out_for([path], "my-fleet") is not None

    def test_ordinary_code_is_not_caught(self) -> None:
        assert carve_out_for(["src/myfleet/study_all.py", "README.md"], "my-fleet") is None

    def test_one_carved_path_taints_an_otherwise_clean_diff(self) -> None:
        assert carve_out_for(["README.md", "CLAUDE.md"], "my-fleet") is not None

    def test_the_gate_cannot_certify_itself(self) -> None:
        # The load-bearing one. If accept.py could ever accept a change to
        # accept.py, a single bad merge widens every merge after it -- the
        # failure compounds instead of staying contained.
        assert carve_out_for(["src/myfleet/accept.py"], "my-fleet") is not None

    def test_every_carve_out_states_why(self) -> None:
        # The reason is shown to whoever gets handed the needs_human verdict.
        every = SHARED_CARVE_OUTS + tuple(p for v in REPO_CARVE_OUTS.values() for p in v)
        assert all(why.strip() for _, why in every)


class TestClosingIssue:
    @pytest.mark.parametrize("body", ["Closes #12", "closes #12", "Fixes #12", "Resolves #12"])
    def test_closing_keywords(self, body: str) -> None:
        assert closing_issue(body) == 12

    def test_a_bare_reference_does_not_claim_to_close(self) -> None:
        assert closing_issue("related to #12, see also #13") is None

    def test_no_body_is_not_a_crash(self) -> None:
        assert closing_issue("") is None


class TestMainIsProtected:
    def test_unprotected_is_false_not_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Distinct from "could not tell": one is a fact about the repo, the
        # other is a fact about us. my-fleet's own main really was unprotected.
        monkeypatch.setattr(
            "myfleet.accept._gh", lambda argv: (1, "", "gh: Branch not protected (HTTP 404)")
        )
        assert main_is_protected("my-fleet") is False

    def test_an_unreadable_answer_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("myfleet.accept._gh", lambda argv: (1, "", "could not resolve host"))
        assert main_is_protected("my-fleet") is None


class TestAssess:
    """Drives assess() against a scripted gh, keyed on the subcommand."""

    @staticmethod
    def _gh_returning(*, pr: dict, files: list[dict], labels: list[str], protected: bool):
        def fake(argv: list[str]) -> tuple[int, str, str]:
            if argv[0] == "api":
                return (0, "", "") if protected else (1, "", "Branch not protected (HTTP 404)")
            if argv[0] == "pr" and "files" in argv:
                return 0, json.dumps({"files": files}), ""
            if argv[0] == "pr":
                return 0, json.dumps(pr), ""
            if argv[0] == "issue":
                return 0, json.dumps({"labels": [{"name": n} for n in labels], "state": "OPEN"}), ""
            raise AssertionError(f"unexpected gh call: {argv}")

        return fake

    def test_a_clean_small_pr_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 10, "deletions": 2}],
                labels=["size:S"],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "pass")
        assert assess("my-fleet", 1).verdict is Verdict.ACCEPTED

    def test_a_skipped_required_check_is_not_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #32, inherited correctly: a skipped check produced no result, and the
        # gate must not read that absence as permission.
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 10, "deletions": 2}],
                labels=["size:S"],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "skipped")
        assert assess("my-fleet", 1).verdict is Verdict.NEEDS_HUMAN

    def test_an_unprotected_main_cannot_be_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no protection nothing is required, so a green check is not
        # evidence of anything. Never accept on it.
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 1, "deletions": 0}],
                labels=["size:S"],
                protected=False,
            ),
        )
        assert assess("my-fleet", 1).verdict is Verdict.REJECTED

    def test_an_oversized_diff_needs_a_human_rather_than_being_thrown_away(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A size: label is an estimate made before the work existed. Exceeding it
        # is as likely to mean the estimate was wrong as that the diff is, and
        # only a human can tell those apart -- so escalate, never reject.
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 900, "deletions": 0}],
                labels=["size:S"],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "pass")
        found = assess("my-fleet", 1)
        assert found.verdict is Verdict.NEEDS_HUMAN
        assert "bigger than the label predicted" in found.reason

    def test_a_pr_closing_nothing_needs_a_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without a stated intent there is no scope to compare the diff against,
        # so "in scope" is unanswerable rather than false.
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "a tidy-up",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 3, "deletions": 0}],
                labels=[],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "pass")
        assert assess("my-fleet", 1).verdict is Verdict.NEEDS_HUMAN

    def test_a_draft_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": True,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "src/myfleet/study_all.py", "additions": 3, "deletions": 0}],
                labels=["size:S"],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "pass")
        # Draft stays a hard REJECTED. Unlike an oversized diff, this is not an
        # estimate that might have been wrong -- the author said it isn't done.
        assert assess("my-fleet", 1).verdict is Verdict.REJECTED

    def test_a_carved_path_is_never_accepted_however_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "myfleet.accept._gh",
            self._gh_returning(
                pr={
                    "isDraft": False,
                    "body": "Closes #5",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
                files=[{"path": "CLAUDE.md", "additions": 2, "deletions": 1}],
                labels=["size:S"],
                protected=True,
            ),
        )
        monkeypatch.setattr("myfleet.accept._checks_state", lambda *a: "pass")
        found = assess("my-fleet", 1)
        assert found.verdict is Verdict.NEEDS_HUMAN
        assert "CLAUDE.md" in found.reason


class TestPerRepoCarveOuts:
    """#41: the carve-out list was shaped around my-fleet and missed the
    equivalent gating code in every other repo."""

    def test_my_coders_gating_code_is_carved_out(self) -> None:
        # The regression. my-coder#30 changes coder.py -- where the PR-open
        # action is gated -- and came back `accepted`, because the list only
        # knew my-fleet's paths. It had to be held by hand, which is precisely
        # the judgement call the gate exists to remove.
        assert carve_out_for(["src/mycoder/coder.py"], "my-coder") is not None
        assert carve_out_for(["src/mycoder/cli.py"], "my-coder") is not None

    def test_my_guard_is_carved_out_wholesale(self) -> None:
        # Not a file list: my-guard IS the policy engine, so any change in it
        # can widen what every other tool is permitted to do.
        assert carve_out_for(["src/myguard/rules.py"], "my-guard") is not None
        assert carve_out_for(["src/myguard/ask.py"], "my-guard") is not None

    def test_the_policy_contract_is_carved_out_in_core(self) -> None:
        assert carve_out_for(["src/mythings/policy.py"], "my-things-core") is not None

    def test_shared_carve_outs_apply_to_a_repo_not_in_the_map(self) -> None:
        # The default must be the safe one. Most of the ~44 repos will never be
        # listed, and forgetting to add one must not silently disable its
        # carve-outs.
        assert carve_out_for(["CLAUDE.md"], "my-flashcards") is not None
        assert carve_out_for([".github/workflows/ci.yml"], "some-new-repo") is not None

    def test_one_repos_gating_paths_do_not_leak_into_another(self) -> None:
        # my-coder has no src/myfleet/, but if the lists were merged globally a
        # same-named file anywhere would trip the wrong reason string.
        found = carve_out_for(["src/myfleet/accept.py"], "my-coder")
        assert found is None

    def test_ordinary_code_in_a_gating_repo_is_still_mergeable(self) -> None:
        # The carve-outs must not swallow the whole repo -- my-coder's session
        # runner and my-fleet's study scripts are ordinary code.
        assert carve_out_for(["src/mycoder/session.py"], "my-coder") is None
        assert carve_out_for(["src/myfleet/study_all.py"], "my-fleet") is None
