from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from mythings.ledger import Ledger, LedgerEntry

import myfleet.fleet_dispatch as fd
from myfleet.fleet_usage import family_for

# Captured before any test monkeypatches fd.subprocess.run -- fakes that only
# want to intercept one specific call (e.g. "mycoder") fall back to the real
# subprocess.run for everything else (git, etc). Calling the module-level
# `subprocess.run` name directly would recurse into the same monkeypatch.
_REAL_SUBPROCESS_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _isolate_dispatch_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")



def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "seed").write_text("seed")
    subprocess.run(["git", "-C", str(path), "add", "seed"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True)


def _account(config_dir: Path, settings: dict | None) -> fd.Account:
    config_dir.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (config_dir / "settings.json").write_text(json.dumps(settings))
    return fd.Account(name="account1", config_dir=config_dir)


_RTK_HOOK = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/rtk-rewrite.sh"}],
            }
        ]
    }
}


def _account_with_uuid(config_dir: Path, uuid: str | None) -> fd.Account:
    config_dir.mkdir(parents=True, exist_ok=True)
    if uuid is not None:
        (config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": "x@y"}})
        )
    return fd.Account(name=config_dir.name, config_dir=config_dir)


def test_account_uuid_reads_or_none(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-123")
    assert fd._account_uuid(a.config_dir) == "uuid-123"
    assert fd._account_uuid(tmp_path / "missing") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / ".claude.json").write_text("{not json")
    assert fd._account_uuid(tmp_path / "bad") is None


def test_preflight_distinct_accounts_flags_same_account(tmp_path: Path) -> None:
    # The exact footgun: two config dirs logged into the same account.
    a = _account_with_uuid(tmp_path / "a", "same-uuid")
    b = _account_with_uuid(tmp_path / "b", "same-uuid")
    problems = fd._preflight_distinct_accounts([a, b])
    assert len(problems) == 1
    assert "SAME Claude account" in problems[0]


def test_preflight_distinct_accounts_clean_when_different(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-a")
    b = _account_with_uuid(tmp_path / "b", "uuid-b")
    assert fd._preflight_distinct_accounts([a, b]) == []


def test_preflight_distinct_accounts_flags_unreadable_identity(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-a")
    b = _account_with_uuid(tmp_path / "b", None)  # no .claude.json
    problems = fd._preflight_distinct_accounts([a, b])
    assert len(problems) == 1
    assert "can't read an account identity" in problems[0]


def test_main_dispatches_accounts_concurrently(tmp_path: Path, monkeypatch) -> None:
    # Regression test for the bug this fix closes: main()'s dispatch loop used
    # to call _dispatch_one sequentially, so two accounts' work never
    # overlapped in time. Stub _dispatch_one to block for a bit and record its
    # own [start, end) window; if the loop is truly concurrent the two
    # accounts' windows overlap, if it's sequential they can't.
    calls: list[tuple[str, float, float]] = []
    calls_lock = threading.Lock()

    def fake_dispatch_one(
        account,
        candidate,
        *,
        execute,
        max_budget_usd,
        max_turns,
        ledger,
        org,
        prior=None,
        ready_timeout=0.0,
        session_timeout_s=1800.0,
    ):
        start = time.monotonic()
        time.sleep(0.2)
        end = time.monotonic()
        with calls_lock:
            calls.append((account.name, start, end))

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="repoA#1", repo="repoA", tool="", title="t1", kind="issue", created_at="2020-01-01"
        ),
        fd.Candidate(
            id="repoB#2", repo="repoB", tool="", title="t2", kind="issue", created_at="2020-01-02"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    assert rc == 0
    assert len(calls) == 2
    (_name1, start1, end1), (_name2, start2, end2) = calls
    assert start1 < end2 and start2 < end1


def test_main_surfaces_every_account_failure_not_just_first(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Regression test: future.result() in a loop raises on the first failing
    # future and unwinds before the loop reaches the second, silently
    # dropping any other account's crash. Both accounts fail here on purpose;
    # both should still be reported.
    def fake_dispatch_one(
        account,
        candidate,
        *,
        execute,
        max_budget_usd,
        max_turns,
        ledger,
        org,
        prior=None,
        ready_timeout=0.0,
        session_timeout_s=1800.0,
    ):
        raise RuntimeError(f"boom-{account.name}")

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="repoA#1", repo="repoA", tool="", title="t1", kind="issue", created_at="2020-01-01"
        ),
        fd.Candidate(
            id="repoB#2", repo="repoB", tool="", title="t2", kind="issue", created_at="2020-01-02"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "boom-account1" in out
    assert "boom-account2" in out


def test_critical_halt_issues_parses_gh_search_output(monkeypatch) -> None:
    payload = [
        {
            "repository": {"nameWithOwner": "MyThingsLab/my-things-core"},
            "number": 5,
            "title": "auth bypass",
            "url": "https://github.com/MyThingsLab/my-things-core/issues/5",
        },
    ]

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["gh", "search", "issues"]
        assert "critical" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(fd.subprocess, "run", fake_run)
    assert fd._critical_halt_issues("MyThingsLab") == payload


def test_critical_halt_issues_empty_on_gh_failure(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(fd.subprocess, "run", fake_run)
    assert fd._critical_halt_issues("MyThingsLab") == []


def test_main_halts_dispatch_when_critical_issue_open(tmp_path: Path, monkeypatch, capsys) -> None:
    critical = [
        {
            "repository": {"nameWithOwner": "MyThingsLab/my-things-core"},
            "number": 5,
            "title": "auth bypass",
            "url": "https://github.com/MyThingsLab/my-things-core/issues/5",
        },
    ]
    monkeypatch.setattr(fd, "_critical_halt_issues", lambda org: critical)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])

    def boom_orchestrator(**_kwargs):
        raise AssertionError("Orchestrator should not be constructed while halted")

    monkeypatch.setattr(fd, "Orchestrator", boom_orchestrator)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "halted" in out
    assert "my-things-core#5" in out

    entries = list(Ledger(fd.DISPATCH_LEDGER))
    assert entries[-1].outcome == "halted_critical"


def test_main_skips_issue_with_open_pr_in_flight(tmp_path: Path, monkeypatch) -> None:
    # An issue that already has an open fleet-dispatch PR must not be handed to
    # an account again -- otherwise a second, duplicate PR gets opened for it.
    dispatched: list[str] = []

    def fake_dispatch_one(
        account,
        candidate,
        *,
        execute,
        max_budget_usd,
        max_turns,
        ledger,
        org,
        prior=None,
        ready_timeout=0.0,
        session_timeout_s=1800.0,
    ):
        dispatched.append(candidate.id)

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="repo#1", repo="repo", tool="", title="done", kind="issue", created_at="2020-01-01"
        ),
        fd.Candidate(
            id="repo#2", repo="repo", tool="", title="todo", kind="issue", created_at="2020-01-02"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    # repo#1's branch already has an open PR (#99); repo#2's does not.
    monkeypatch.setattr(
        fd,
        "_open_pr_number",
        lambda org, repo, branch: 99 if branch == fd._mycoder_branch(candidates[0]) else None,
    )

    rc = fd.main(["--accounts", f"{tmp_path / 'a'}"])

    assert rc == 0
    # The single account should get repo#2 (todo), never the in-flight repo#1.
    assert dispatched == ["repo#2"]


# --- A: honest success detection -------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la docs/tools/", "ls"),
        ("rtk ls docs/tools/", "ls"),
        ("cat README.md", "cat"),
        ("grep -rn foo .", "grep"),
        ("rtk grep -rl bar .", "grep"),
        ("head -20 f.py", "head"),
        ("git status", "git"),
        ("rtk git status", "git"),  # rtk-prefixed git now classifies correctly
        ("gh pr view 1", "gh"),
        ("python -m pytest -q", "pytest"),
        # Mutating / code-running commands must NOT be recognised -> friction.
        ("rm conftest.py", None),
        ("pip install -e .", None),
        ("python -c 'import mythings'", None),
        ("find . -delete", None),
    ],
)
def test_family_for_readonly_vs_mutation(command: str, expected: str | None) -> None:
    assert family_for(command) == expected


@pytest.mark.parametrize(
    ("attempt", "blocker_open", "expected"),
    [
        (None, False, "fresh"),
        (fd.Attempt("i#1", "success", "b", 1), False, "skip:done"),
        (fd.Attempt("i#1", "needs_human", "b", 3), False, "skip:needs_human"),
        (fd.Attempt("i#1", "blocked", "b", 1, blocker="o/r#2"), True, "skip:blocked"),
        (fd.Attempt("i#1", "blocked", "b", 1, blocker="o/r#2"), False, "resume"),
        (fd.Attempt("i#1", "needs_review", "b", 1), False, "resume"),
        (fd.Attempt("i#1", "no_changes", "b", 2), False, "resume"),
        (fd.Attempt("i#1", "failed", "b", 3), False, "skip:needs_human"),  # hit the cap
        # my-coder's own vocabulary: both are stopping points, never resumed.
        (fd.Attempt("i#1", "denied", "b", 1), False, "skip:done"),
        (fd.Attempt("i#1", "skipped", "b", 1), False, "skip:done"),
    ],
)
def test_dispatch_decision(attempt, blocker_open: bool, expected: str) -> None:
    assert fd._dispatch_decision(attempt, blocker_open, max_attempts=3) == expected


def test_last_attempt_reads_latest_terminal_and_counts_attempts(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.jsonl")
    led.record("fleet_dispatch", "dispatch", "started", candidate="r#1", branch="b")
    led.record(
        "fleet_dispatch",
        "dispatch",
        "no_changes",
        candidate="r#1",
        branch="b",
        final_message="stuck on ls",
    )
    led.record("fleet_dispatch", "dispatch", "started", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "needs_review", candidate="r#1", branch="b", commits=1)
    led.record("fleet_dispatch", "dispatch", "success", candidate="other#2", branch="b2")

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.outcome == "needs_review"
    assert a.attempt_number == 2  # two terminal entries; "started" doesn't count
    assert a.branch == "b"
    assert fd._last_attempt(led, "nope#9") is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("You've hit your session limit · resets 6pm", True),
        ("Error: usage limit reached", True),
        ("overloaded_error: server busy", True),
        ("Traceback: AssertionError in test_foo", False),
        ("could not find the file", False),
    ],
)
def test_is_transient_failure(message: str, expected: bool) -> None:
    assert fd._is_transient_failure(message) is expected


def test_transient_failures_do_not_count_toward_attempt_cap(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.jsonl")
    # Two transient (deferred) runs and one real failure.
    led.record("fleet_dispatch", "dispatch", "deferred", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "failed", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "deferred", candidate="r#1", branch="b")

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.outcome == "deferred"  # latest
    assert a.attempt_number == 1  # only the real "failed" counts, not the two deferred


def test_failed_entry_with_transient_message_does_not_count(tmp_path: Path) -> None:
    # Defends against "failed" entries recorded before transient classification
    # existed (exactly the two rate-limited #17 runs in the live ledger): a
    # failure whose message is transient must not count toward the cap.
    led = Ledger(tmp_path / "l.jsonl")
    led.record(
        "fleet_dispatch",
        "dispatch",
        "failed",
        candidate="r#1",
        branch="b",
        final_message="You've hit your session limit · resets 6pm",
    )
    led.record(
        "fleet_dispatch",
        "dispatch",
        "failed",
        candidate="r#1",
        branch="b",
        final_message="You've hit your session limit · resets 6pm",
    )

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.attempt_number == 0  # both transient -> neither counts


def test_dispatch_decision_deferred_always_resumes() -> None:
    # Even a long string of transient deferrals never escalates to a human,
    # because attempt_number excludes them (here it's 0).
    deferred = fd.Attempt("r#1", "deferred", "b", 0)
    assert fd._dispatch_decision(deferred, blocker_open=False, max_attempts=3) == "resume"


def test_human_telegram_retry_resumes_needs_human_candidate(tmp_path: Path) -> None:
    # Issue my-fleet#65: When an operator taps "Retry" on a blocker escalation,
    # blocker_decision outcome="retry" entry allows the candidate to resume.
    led = Ledger(tmp_path / "l.jsonl")
    led.record("fleet_dispatch", "dispatch", "needs_human", candidate="r#65", branch="b")
    led.record("mytelegrambot", "blocker_decision", "retry", candidate="r#65", detail="retry from chat")

    attempt = fd._last_attempt(led, "r#65")
    assert attempt is not None
    assert attempt.has_human_retry is True
    assert fd._dispatch_decision(attempt, blocker_open=False, max_attempts=3) == "resume"


def test_main_resumes_or_skips_by_prior_attempt(
    tmp_path: Path, monkeypatch, telegram: list[tuple[str, dict]]
) -> None:
    got: dict[str, object] = {}

    def fake_dispatch_one(
        account,
        candidate,
        *,
        execute,
        max_budget_usd,
        max_turns,
        ledger,
        org,
        prior=None,
        ready_timeout=0.0,
        session_timeout_s=1800.0,
    ):
        got[candidate.id] = prior

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")

    candidates = [
        fd.Candidate(
            id="r#1", repo="r", tool="", title="resume", kind="issue", created_at="2020-01-01"
        ),
        fd.Candidate(
            id="r#2", repo="r", tool="", title="blocked", kind="issue", created_at="2020-01-02"
        ),
        fd.Candidate(
            id="r#3", repo="r", tool="", title="capped", kind="issue", created_at="2020-01-03"
        ),
        fd.Candidate(
            id="r#4", repo="r", tool="", title="fresh", kind="issue", created_at="2020-01-04"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])

    attempts = {
        "r#1": fd.Attempt("r#1", "needs_review", "b1", 1),
        "r#2": fd.Attempt("r#2", "blocked", "b2", 1, blocker="MyThingsLab/core#9"),
        "r#3": fd.Attempt("r#3", "failed", "b3", 3),  # at the attempt cap
    }
    monkeypatch.setattr(fd, "_last_attempt", lambda ledger, cid: attempts.get(cid))
    monkeypatch.setattr(fd, "_issue_is_open", lambda ref: True)  # r#2's blocker still open

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    assert rc == 0
    # r#2 (blocked, still open) and r#3 (hit cap) skipped; r#1 resumed, r#4 fresh.
    assert set(got) == {"r#1", "r#4"}
    assert got["r#1"] is attempts["r#1"]  # resumed with its prior attempt
    assert got["r#4"] is None  # fresh
    # r#3 hitting the cap is recorded as needs_human so it stays skipped.
    outcomes = [
        e.outcome for e in Ledger(tmp_path / "ledger.jsonl") if e.data.get("candidate") == "r#3"
    ]
    assert "needs_human" in outcomes
    # ...and a human is actually told, which is the point of needs_human. Pinned
    # here rather than left to happen: unstubbed, this path shelled out to the
    # real `mytelegrambot` and messaged the operator on every run (#47).
    escalated = [kwargs["candidate"] for name, kwargs in telegram if name == "escalate_blocker"]
    assert escalated == ["r#3"]


# --- deny-reads shrink what a worker may read ------------------------------


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        ("", "none"),
        ("pass\npass", "pass"),
        # A skipped REQUIRED check is not a pass (#32). These are the real
        # bucket values gh reported for draft PR my-things-core#152, whose
        # `test` job `ci.yml` skips precisely because the PR is a draft --
        # which is the state every my-coder PR is born in.
        ("skipping", "skipped"),
        ("skipping\nskipping", "skipped"),
        # ...and not even alongside a green sibling: the skipped check still
        # produced no result of its own.
        ("pass\nskipping", "skipped"),
        ("pass\npending", "pending"),
        ("pass\nfail", "fail"),
        ("cancel", "fail"),
        # Failure and cancellation still outrank a skip.
        ("skipping\nfail", "fail"),
    ],
)
def test_checks_state_collapses_buckets(monkeypatch, buckets: str, expected: str) -> None:
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=buckets, stderr=""),
    )
    assert fd._checks_state("org", "repo", 1) == expected


def test_checks_state_asks_only_for_required_checks(monkeypatch) -> None:
    # The discriminator for #32. A repo's optional jobs skip all the time --
    # the dependabot `automerge` job skips on every human PR -- so "was it
    # skipped" is only a usable signal once the question is narrowed to the
    # checks branch protection actually requires.
    seen: list[list[str]] = []

    def fake_run(argv, *a, **k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="pass", stderr="")

    monkeypatch.setattr(fd.subprocess, "run", fake_run)
    fd._checks_state("org", "repo", 1)

    assert "--required" in seen[0]


def test_wait_for_checks_returns_pending_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "pending")
    # timeout=0 -> a single check, no sleeping; still-running stays 'pending'.
    assert fd._wait_for_checks("org", "repo", 1, timeout=0) == "pending"


def test_finalize_pr_needs_review_when_tests_not_passed(monkeypatch) -> None:
    # Trusts my-coder's own structured tests_passed signal directly -- never a
    # success without it, regardless of CI state.
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "pass")

    outcome, msg = fd._finalize_pr("org", "repo", 42, tests_passed=None, ready_timeout=0)

    assert outcome == "needs_review"
    assert "did not report tests passing" in msg


def test_finalize_pr_succeeds_when_tests_passed_and_ci_green(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "pass")

    outcome, msg = fd._finalize_pr("org", "repo", 42, tests_passed=True, ready_timeout=0)

    assert outcome == "success"
    assert "awaiting a human merge" in msg


def test_finalize_pr_never_promotes_or_merges_anything() -> None:
    # The gate moved to the merge (#32). my-coder opens the PR ready when its
    # own suite passed, so there is nothing left here to promote -- and a human
    # has always been the only thing that merges. Asserted structurally: this
    # module must not retain a promotion helper that a later change could
    # quietly start calling again.
    assert not hasattr(fd, "_promote_pr")
    source = Path(fd.__file__).read_text()
    assert "gh pr ready" not in source
    assert "gh pr merge" not in source


def test_finalize_pr_needs_review_when_ci_fails(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "fail")
    outcome, msg = fd._finalize_pr("org", "repo", 42, tests_passed=True, ready_timeout=0)
    assert outcome == "needs_review"
    assert "CI failing" in msg


def test_finalize_pr_needs_review_when_no_ci_checks(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "none")
    outcome, msg = fd._finalize_pr("org", "repo", 42, tests_passed=True, ready_timeout=0)
    assert outcome == "needs_review"
    assert "no CI checks" in msg


def test_finalize_pr_never_succeeds_on_a_skipped_check(monkeypatch) -> None:
    # The regression #32 is about. `ci.yml` skips required checks while a PR is
    # a draft and my-coder used to open only drafts, so EVERY worker PR reached
    # this path with skipped checks -- and was promoted to ready and recorded
    # `success` with the detail "(CI green)" for a suite that never ran. The
    # fleet's autonomous success signal was a false green by construction.
    #
    # Unreachable now that a verified PR is opened ready, which is the point:
    # if this ever fires again, the ready-on-verified path has regressed.
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "skipped")

    outcome, msg = fd._finalize_pr("org", "repo", 42, tests_passed=True, ready_timeout=0)

    assert outcome == "needs_review"
    assert "skipped" in msg and "not green" in msg


def test_wait_for_checks_treats_skipped_as_terminal(monkeypatch) -> None:
    # A skipped check will not start later, so polling it to the deadline would
    # burn the full --ready-timeout only to return the same answer.
    calls = []

    def state(*a, **k):
        calls.append(1)
        return "skipped"

    monkeypatch.setattr(fd, "_checks_state", state)
    assert fd._wait_for_checks("org", "repo", 1, timeout=600) == "skipped"
    assert len(calls) == 1


def test_abort_arms_halt_marker_without_needing_accounts(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "HALT"
    monkeypatch.setattr(fd, "HALT_MARKER", marker)

    rc = fd.main(["--abort"])

    assert rc == 0
    assert marker.exists()


def test_clear_halt_removes_marker(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "HALT"
    marker.write_text("halted at some point\n")
    monkeypatch.setattr(fd, "HALT_MARKER", marker)

    rc = fd.main(["--clear-halt"])

    assert rc == 0
    assert not marker.exists()


def test_clear_halt_when_not_set_is_a_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")

    rc = fd.main(["--clear-halt"])

    assert rc == 0


def test_abort_and_clear_halt_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        fd.main(["--abort", "--clear-halt"])


def _dispatch_stub(calls: list) -> callable:
    def fake_dispatch_one(*a, **k):
        calls.append(k)

    return fake_dispatch_one


def _wire_single_candidate_orchestrator(monkeypatch) -> None:
    candidates = [
        fd.Candidate(
            id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)


def test_main_refuses_execute_when_halted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    (tmp_path / "HALT").write_text("halted\n")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a"), "--execute"])

    assert rc == 1
    assert calls == [], "the kill switch must stop launch before any session starts"


def test_main_dry_run_still_reports_when_halted(tmp_path: Path, monkeypatch, capsys) -> None:
    # A dry run spends nothing, so the marker is informational there, not a
    # block -- symmetric with how --max-daily-usd treats dry runs.
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    (tmp_path / "HALT").write_text("halted\n")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "HALT marker present" in out
    assert len(calls) == 1  # dry-run _dispatch_one still runs; it just prints and returns


def _usage_entry(ledger: Ledger, *, cost_usd: float, ts: str) -> None:
    ledger.append(
        LedgerEntry(
            tool="fleet_dispatch",
            kind="usage",
            outcome="success",
            ts=ts,
            data={"cost_usd": cost_usd},
        )
    )


def test_today_spend_usd_sums_only_todays_usage_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    _usage_entry(ledger, cost_usd=1.5, ts="2026-07-10T01:00:00Z")
    _usage_entry(ledger, cost_usd=2.25, ts="2026-07-10T02:00:00Z")
    _usage_entry(ledger, cost_usd=99.0, ts="2026-07-09T23:59:59Z")  # yesterday, excluded

    assert fd._today_spend_usd(ledger, today="2026-07-10") == pytest.approx(3.75)


def test_today_spend_usd_ignores_non_usage_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record(tool="fleet_dispatch", kind="dispatch", outcome="success", cost_usd=50.0)

    assert fd._today_spend_usd(ledger, today="2026-07-10") == 0.0


def test_main_refuses_to_dispatch_over_daily_cap(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=19.0, ts=f"{today}T00:00:00Z")

    called = False

    def fake_dispatch_one(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--execute",
            "--allow-personal-token",
            "--max-budget-usd",
            "3.0",
            "--max-daily-usd",
            "20.0",
        ]
    )

    assert rc == 1
    assert called is False, "must refuse before spend, not launch and hope"


def test_main_dry_run_ignores_daily_cap(tmp_path: Path, monkeypatch) -> None:
    # A dry run spends nothing, so it must not be blocked by the cap -- the
    # whole point of --dry-run is to report safely regardless of budget state.
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=999.0, ts=f"{today}T00:00:00Z")

    def fake_dispatch_one(*a, **k):
        pass

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0


def _gh_identity_stub(login: str = "someone"):
    real_run = fd.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{login}\n", stderr="")
        return real_run(argv, *args, **kwargs)

    return fake_run


def test_effective_daily_cap_without_override_returns_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd, "DAILY_CAP_OVERRIDE", tmp_path / "override.json")

    assert fd._effective_daily_cap(20.0) == 20.0


def test_effective_daily_cap_uses_override_for_today(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"day": "2026-07-12", "cap_usd": 40.0}))
    monkeypatch.setattr(fd, "DAILY_CAP_OVERRIDE", override)

    assert fd._effective_daily_cap(20.0, today="2026-07-12") == 40.0


def test_effective_daily_cap_ignores_override_from_a_different_day(
    tmp_path: Path, monkeypatch
) -> None:
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"day": "2026-07-11", "cap_usd": 40.0}))
    monkeypatch.setattr(fd, "DAILY_CAP_OVERRIDE", override)

    assert fd._effective_daily_cap(20.0, today="2026-07-12") == 20.0


def test_effective_daily_cap_never_lowers_below_default(tmp_path: Path, monkeypatch) -> None:
    # A raise is meant to widen, never narrow -- a stale low override from
    # earlier in the day must not undercut a --max-daily-usd bump.
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"day": "2026-07-12", "cap_usd": 5.0}))
    monkeypatch.setattr(fd, "DAILY_CAP_OVERRIDE", override)

    assert fd._effective_daily_cap(20.0, today="2026-07-12") == 20.0


def test_spend_alert_sent_today_true_only_for_successful_same_day_entry(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(
        LedgerEntry(
            tool="fleet_dispatch", kind="spend_alert", outcome="failure", ts="2026-07-12T01:00:00Z"
        )
    )
    assert fd._spend_alert_sent_today(ledger, today="2026-07-12") is False

    ledger.append(
        LedgerEntry(
            tool="fleet_dispatch", kind="spend_alert", outcome="success", ts="2026-07-11T01:00:00Z"
        )
    )
    assert fd._spend_alert_sent_today(ledger, today="2026-07-12") is False

    ledger.append(
        LedgerEntry(
            tool="fleet_dispatch", kind="spend_alert", outcome="success", ts="2026-07-12T02:00:00Z"
        )
    )
    assert fd._spend_alert_sent_today(ledger, today="2026-07-12") is True


def test_main_raise_daily_cap_writes_override_and_exits(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "override.json"
    monkeypatch.setattr(fd, "DAILY_CAP_OVERRIDE", override)

    def boom_orchestrator(**_kwargs):
        raise AssertionError("--raise-daily-cap must exit before touching the orchestrator")

    monkeypatch.setattr(fd, "Orchestrator", boom_orchestrator)

    rc = fd.main(["--raise-daily-cap", "35.0"])

    assert rc == 0
    data = json.loads(override.read_text())
    assert data["cap_usd"] == 35.0
    assert data["day"] == datetime.now(fd.UTC).strftime("%Y-%m-%d")


def test_main_pushes_spend_alert_once_when_threshold_crossed(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=17.0, ts=f"{today}T00:00:00Z")  # 85% of a $20 cap

    monkeypatch.setattr(fd.subprocess, "run", _gh_identity_stub())
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    alert_calls: list = []
    monkeypatch.setattr(
        fd.fleet_ask,
        "alert_spend",
        lambda **kw: alert_calls.append(kw) or True,
    )
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--execute",
            "--allow-personal-token",
            "--max-budget-usd",
            "1.0",
            "--max-daily-usd",
            "20.0",
        ]
    )

    assert rc == 0
    assert len(alert_calls) == 1
    assert alert_calls[0]["spent"] == pytest.approx(17.0)
    assert alert_calls[0]["cap"] == pytest.approx(20.0)

    entries = ledger.read(tool="fleet_dispatch", kind="spend_alert")
    assert len(entries) == 1
    assert entries[0].outcome == "success"


def test_main_does_not_repush_spend_alert_same_day(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=17.0, ts=f"{today}T00:00:00Z")
    ledger.record(
        tool="fleet_dispatch", kind="spend_alert", outcome="success", detail="already sent"
    )

    monkeypatch.setattr(fd.subprocess, "run", _gh_identity_stub())
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub([]))
    alert_calls: list = []
    monkeypatch.setattr(fd.fleet_ask, "alert_spend", lambda **kw: alert_calls.append(kw) or True)
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--execute",
            "--allow-personal-token",
            "--max-budget-usd",
            "1.0",
            "--max-daily-usd",
            "20.0",
        ]
    )

    assert rc == 0
    assert alert_calls == []


def test_main_escalates_blocker_on_needs_human(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
    )

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(candidate)]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    stuck = fd.Attempt(
        candidate_id="repo#1",
        outcome="failed",
        branch="fleet-dispatch/repo-1",
        attempt_number=fd.MAX_ATTEMPTS,
        blocker=None,
        final_message="nope",
    )
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: stuck)

    escalate_calls: list = []
    monkeypatch.setattr(
        fd.fleet_ask,
        "escalate_blocker",
        lambda **kw: escalate_calls.append(kw) or True,
    )

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0
    assert len(escalate_calls) == 1
    assert escalate_calls[0]["candidate"] == "repo#1"
    assert escalate_calls[0]["attempt"] == fd.MAX_ATTEMPTS


def test_main_auto_resumes_a_blocked_candidate_once_its_blocker_closes(
    tmp_path: Path, monkeypatch
) -> None:
    # Nothing should have to manually re-trigger this: every main() run
    # already re-checks _issue_is_open for a blocked candidate's blocker via
    # the same per-candidate loop that handles fresh/resume/needs_human.
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
    )

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(candidate)]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    blocked = fd.Attempt(
        candidate_id="repo#1",
        outcome="blocked",
        branch="mycoder/repo-1",
        attempt_number=1,
        blocker="MyThingsLab/my-guard#7",
        final_message="paused on cross-repo blocker MyThingsLab/my-guard#7",
    )
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: blocked)
    monkeypatch.setattr(fd, "_issue_is_open", lambda ref: False)  # the blocker is now closed

    dispatched: list[tuple[str, fd.Attempt | None]] = []

    def fake_dispatch_one(
        account,
        candidate,
        *,
        execute,
        max_budget_usd,
        max_turns,
        ledger,
        org,
        prior=None,
        ready_timeout=0.0,
        session_timeout_s=1800.0,
    ):
        dispatched.append((candidate.id, prior))

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0
    assert len(dispatched) == 1
    dispatched_id, prior = dispatched[0]
    assert dispatched_id == "repo#1"
    # Resuming from the closed-blocker attempt, not treated as a fresh start --
    # its attempt_number must carry forward so a resume-then-fail-again run
    # still counts toward MAX_ATTEMPTS normally.
    assert prior is blocked
    assert prior.attempt_number == 1


def test_main_still_skips_a_blocked_candidate_whose_blocker_stays_open(
    tmp_path: Path, monkeypatch
) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
    )

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(candidate)]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    blocked = fd.Attempt(
        candidate_id="repo#1",
        outcome="blocked",
        branch="mycoder/repo-1",
        attempt_number=1,
        blocker="MyThingsLab/my-guard#7",
    )
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: blocked)
    monkeypatch.setattr(fd, "_issue_is_open", lambda ref: True)  # still open

    dispatched = []
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: dispatched.append(a))

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0
    assert dispatched == []


def test_main_execute_refuses_personal_token_without_optin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")

    def boom_orchestrator(**_kwargs):
        raise AssertionError("must refuse before ranking anything")

    monkeypatch.setattr(fd, "Orchestrator", boom_orchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])

    rc = fd.main(["--accounts", str(tmp_path / "a"), "--execute"])

    assert rc == 1
    assert "refusing to --execute on the ambient personal gh token" in capsys.readouterr().out


def test_main_execute_proceeds_with_allow_personal_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    real_run = fd.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 0, stdout="someone\n", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a"), "--execute", "--allow-personal-token"])

    assert rc == 0
    assert len(calls) == 1


def test_main_execute_refuses_personal_token_when_gh_not_authenticated(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    real_run = fd.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not authenticated")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    def boom_orchestrator(**_kwargs):
        raise AssertionError("must refuse before ranking anything")

    monkeypatch.setattr(fd, "Orchestrator", boom_orchestrator)

    rc = fd.main(["--accounts", str(tmp_path / "a"), "--execute", "--allow-personal-token"])

    assert rc == 1
    assert "gh auth status" in capsys.readouterr().out


def test_main_execute_proceeds_with_app_identity(tmp_path: Path, monkeypatch) -> None:
    # The App IS the sanctioned identity: no extra opt-in needed.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fd, "github_app_token", lambda *a: "ghs_" + "x" * 36)
    monkeypatch.setattr(fd, "app_installation_org", lambda *a: "MyThingsLab")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--execute",
            "--app-id",
            "1",
            "--app-installation-id",
            "2",
            "--app-private-key",
            "/k.pem",
        ]
    )

    assert rc == 0
    assert len(calls) == 1


def test_main_execute_refuses_app_installation_for_wrong_org(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    monkeypatch.setattr(fd, "app_installation_org", lambda *a: "SomeoneElsesOrg")

    def boom_token(*_a):
        raise AssertionError("must refuse before minting a token")

    monkeypatch.setattr(fd, "github_app_token", boom_token)

    def boom_orchestrator(**_kwargs):
        raise AssertionError("must refuse before ranking anything")

    monkeypatch.setattr(fd, "Orchestrator", boom_orchestrator)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--execute",
            "--app-id",
            "1",
            "--app-installation-id",
            "2",
            "--app-private-key",
            "/k.pem",
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "SomeoneElsesOrg" in out
    assert "MyThingsLab" in out


def test_main_dry_run_needs_no_identity_optin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0
    assert len(calls) == 1  # the dry-run _dispatch_one still reports


def test_main_requires_all_three_app_flags_together(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(SystemExit):
        fd.main(["--accounts", str(tmp_path / "a"), "--app-id", "4260739"])


def test_main_mints_app_token_and_sets_gh_token_env(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")

    minted = []

    def fake_token(app_id, installation_id, private_key_path):
        minted.append((app_id, installation_id, private_key_path))
        return "ghs_minted_token"

    monkeypatch.setattr(fd, "github_app_token", fake_token)
    monkeypatch.setattr(fd, "app_installation_org", lambda *a: "MyThingsLab")
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: None)
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(
        [
            "--accounts",
            str(tmp_path / "a"),
            "--app-id",
            "4260739",
            "--app-installation-id",
            "145558758",
            "--app-private-key",
            "/path/to/key.pem",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert minted == [("4260739", "145558758", "/path/to/key.pem")]
    assert os.environ["GH_TOKEN"] == "ghs_minted_token"
    assert "authenticating as the GitHub App" in out


def test_main_without_app_flags_does_not_touch_gh_token_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: None)
    _wire_single_candidate_orchestrator(monkeypatch)

    fd.main(["--accounts", str(tmp_path / "a")])

    assert "GH_TOKEN" not in os.environ


def _setup_dispatch_one_repo(
    tmp_path: Path, monkeypatch
) -> tuple[fd.Candidate, fd.Account, Ledger]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    subprocess.run(["git", "-C", str(repo_path), "branch", "-M", "main"], check=True)

    monkeypatch.setattr(fd, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(fd, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t", kind="issue", created_at="2020-01-01"
    )
    account = _account(tmp_path / "cfg", {"model": "sonnet"})
    ledger = Ledger(tmp_path / "ledger.jsonl")
    return candidate, account, ledger


def _fake_mycoder_run(**result_fields):
    # Stands in for the `mycoder build --json` subprocess call: returns its
    # canned JSON on stdout for a "mycoder" argv, real git for anything else
    # (the _fresh_base_ref pre-fetch _dispatch_one still does).
    payload = {
        "outcome": None,
        "detail": "",
        "issue": 1,
        "pr": None,
        "files_touched": [],
        "tests_passed": None,
        "cost_usd": 0.01,
        "attempts": 1,
        "blocker": None,
        **result_fields,
    }

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    return fake_run


def test_dispatch_one_dry_run_never_calls_mycoder(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(fd.subprocess, "run", lambda argv, **k: calls.append(argv))

    fd._dispatch_one(
        account,
        candidate,
        execute=False,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    assert calls == []


def test_dispatch_one_builds_mycoder_argv(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"outcome": "no_changes", "detail": "nothing to do"})
            )
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=2.5,
        max_turns=15,
        ledger=ledger,
        org="MyThingsLab",
        session_timeout_s=900.0,
    )

    argv = captured["argv"]
    assert argv[:2] == ["mycoder", "build"]
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "MyThingsLab/repo"
    assert "--issue" in argv and argv[argv.index("--issue") + 1] == "1"
    assert "--max-budget-usd" in argv and argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "15"
    assert "--session-timeout-s" in argv and argv[argv.index("--session-timeout-s") + 1] == "900.0"
    assert "--run-tests" in argv
    assert "--json" in argv
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(account.config_dir)


@pytest.mark.parametrize(
    ("mycoder_outcome", "expected_outcome"),
    [
        ("no_changes", "no_changes"),
        ("needs_review", "needs_review"),
        ("denied", "denied"),
        ("skipped", "skipped"),
    ],
)
def test_dispatch_one_passes_through_mycoder_outcomes(
    tmp_path: Path, monkeypatch, mycoder_outcome: str, expected_outcome: str
) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess, "run", _fake_mycoder_run(outcome=mycoder_outcome, detail="detail text")
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == expected_outcome


def test_dispatch_one_translates_failure_to_failed(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess, "run", _fake_mycoder_run(outcome="failure", detail="AssertionError")
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "failed"


def test_dispatch_one_reclassifies_transient_failure_as_deferred(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        _fake_mycoder_run(outcome="failure", detail="claude exited 1: session limit reached"),
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "deferred"


def test_dispatch_one_records_blocker_from_mycoder_result(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        _fake_mycoder_run(
            outcome="blocked",
            detail="paused on cross-repo blocker MyThingsLab/my-guard#7",
            blocker="MyThingsLab/my-guard#7",
        ),
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "blocked"
    assert entry.data["blocker"] == "MyThingsLab/my-guard#7"


def test_dispatch_one_success_runs_the_readiness_gate(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        _fake_mycoder_run(outcome="success", detail="opened draft PR #9", pr=9, tests_passed=True),
    )
    finalize_calls = []
    monkeypatch.setattr(
        fd,
        "_finalize_pr",
        lambda org, repo, pr_number, *, tests_passed, ready_timeout: (
            finalize_calls.append((pr_number, tests_passed)) or ("success", "promoted")
        ),
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    assert finalize_calls == [(9, True)]
    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "success"
    assert entry.data["pr_number"] == 9


def test_dispatch_one_records_deferred_on_timeout(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
        session_timeout_s=5.0,
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "deferred"
    assert "wall-clock timeout" in entry.detail


def test_dispatch_one_treats_unparseable_mycoder_output_as_failed(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="mycoder: command not found"
            )
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (entry,) = [e for e in ledger.read() if e.kind == "dispatch" and e.outcome != "started"]
    assert entry.outcome == "failed"


def test_dispatch_one_records_minimal_usage_entry(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        _fake_mycoder_run(outcome="no_changes", detail="nothing to do", cost_usd=0.42),
    )

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    (usage,) = [e for e in ledger.read() if e.kind == "usage"]
    assert usage.data["cost_usd"] == 0.42


def test_main_scaffolds_do_not_consume_slots_when_issues_exist(tmp_path: Path, monkeypatch) -> None:
    scaffold = fd.Candidate(
        id="scaffold:my-scaffold",
        repo="my-scaffold",
        tool="",
        title="t0",
        kind="scaffold",
        created_at="2020-01-01",
    )
    issue = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"
    )

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(scaffold), FakeRecommendation(issue)]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    dispatched = []
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: dispatched.append(a))

    rc = fd.main(["--accounts", str(tmp_path / "a")])
    assert rc == 0
    assert len(dispatched) == 1
    # Issue was dispatched even though scaffold was top of pool
    assert dispatched[0][1].id == "repo#1"


def test_main_records_no_dispatchable_candidates_when_only_scaffolds(
    tmp_path: Path, monkeypatch
) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)

    scaffold = fd.Candidate(
        id="scaffold:my-scaffold",
        repo="my-scaffold",
        tool="",
        title="t0",
        kind="scaffold",
        created_at="2020-01-01",
    )

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(scaffold)]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    rc = fd.main(["--accounts", str(tmp_path / "a")])
    assert rc == 0

    ledger = Ledger(dispatch_ledger_path)
    entries = ledger.read(tool="fleet_dispatch")
    assert any(e.outcome == "no_dispatchable_candidates" for e in entries)


def test_main_records_backlog_empty_when_no_candidates(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list:
            return []

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])

    rc = fd.main(["--accounts", str(tmp_path / "a")])
    assert rc == 0

    ledger = Ledger(dispatch_ledger_path)
    entries = ledger.read(tool="fleet_dispatch")
    assert any(e.outcome == "backlog_empty" for e in entries)


def test_dispatch_one_uses_gemini_runner_when_provider_is_gemini(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    captured_argv: list[str] = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"outcome": "no_changes", "detail": "ok"}), stderr=""
        )

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
        provider="gemini",
    )

    idx = captured_argv.index("--session-runner")
    assert captured_argv[idx + 1] == "gemini"


def test_active_worker_blackboard_register_and_deregister(tmp_path: Path) -> None:
    path = tmp_path / "active_workers.json"
    assert fd._read_active_workers(path) == {}

    fd._register_worker("account1", "my-things-core#150", "my-things-core", path=path)
    workers = fd._read_active_workers(path)
    assert "account1" in workers
    assert workers["account1"]["candidate"] == "my-things-core#150"
    assert workers["account1"]["repo"] == "my-things-core"

    fd._register_worker("account2", "my-tester#9", "my-tester", path=path)
    workers = fd._read_active_workers(path)
    assert len(workers) == 2
    assert workers["account2"]["candidate"] == "my-tester#9"

    fd._deregister_worker("account1", path=path)
    workers = fd._read_active_workers(path)
    assert "account1" not in workers
    assert "account2" in workers

    fd._deregister_worker("account2", path=path)
    assert fd._read_active_workers(path) == {}


def test_active_worker_blackboard_cleanup_stale_leases(tmp_path: Path) -> None:
    path = tmp_path / "active_workers.json"
    old_time = "2026-09-13T10:00:00+00:00"
    fresh_time = "2026-09-13T16:00:00+00:00"
    now = datetime.fromisoformat("2026-09-13T17:00:00+00:00")

    fd._register_worker("account1", "repo#1", "repo", started_at=old_time, path=path)
    fd._register_worker("account2", "repo#2", "repo", started_at=fresh_time, path=path)

    # max_age_s = 3600 (1 hour): account1 is 7h old (stale), account2 is 1h old (fresh)
    removed = fd._cleanup_stale_leases(max_age_s=3600.0, now=now, path=path)
    assert removed == 1
    workers = fd._read_active_workers(path)
    assert "account1" not in workers
    assert "account2" in workers

    # force_all clears everything
    fd._cleanup_stale_leases(force_all=True, path=path)
    assert fd._read_active_workers(path) == {}


def test_active_fleet_context_formats_siblings(tmp_path: Path) -> None:
    path = tmp_path / "active_workers.json"
    fd._register_worker("account1", "my-things-core#150", "my-things-core", path=path)
    fd._register_worker("account2", "my-tester#9", "my-tester", path=path)

    # account1 sees account2
    ctx1 = fd._active_fleet_context("account1", path=path)
    assert "Active Fleet Context:" in ctx1
    assert "Worker 'account2': my-tester#9" in ctx1
    assert "Worker 'account1'" not in ctx1

    # account2 sees account1
    ctx2 = fd._active_fleet_context("account2", path=path)
    assert "Worker 'account1': my-things-core#150" in ctx2
    assert "Worker 'account2'" not in ctx2

    # A lone worker sees empty context
    fd._deregister_worker("account1", path=path)
    assert fd._active_fleet_context("account2", path=path) == ""


def test_dispatch_one_registers_worker_and_passes_context(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    blackboard_path = tmp_path / "active_workers.json"
    monkeypatch.setattr(fd, "ACTIVE_WORKERS_FILE", blackboard_path)

    # Pre-register a sibling worker
    fd._register_worker("account2", "sibling-repo#5", "sibling-repo", path=blackboard_path)

    captured_env = {}

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            captured_env.update(kwargs.get("env", {}))
            # Verify account1 was registered on the blackboard during run
            active = fd._read_active_workers(blackboard_path)
            assert "account1" in active
            assert active["account1"]["candidate"] == candidate.id
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"outcome": "success", "detail": "pr opened", "pr": 1}),
            )
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    # After _dispatch_one finishes, account1 should be deregistered, sibling retained
    active = fd._read_active_workers(blackboard_path)
    assert "account1" not in active
    assert "account2" in active

    # Verify MYTHINGS_FLEET_CONTEXT was passed to the worker env
    assert "MYTHINGS_FLEET_CONTEXT" in captured_env
    assert "sibling-repo#5" in captured_env["MYTHINGS_FLEET_CONTEXT"]


def test_ensure_repo_graph_builds_and_caches(tmp_path: Path) -> None:
    repo_path = tmp_path / "my-tool"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    # Add python source and markdown documentation
    pkg_dir = repo_path / "src" / "mytool"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "worker.py").write_text("def do_work() -> int:\n    return 42\n")
    doc_dir = repo_path / "docs"
    doc_dir.mkdir(parents=True)
    (doc_dir / "adr.md").write_text("# ADR 001\n`do_work` is fast.\n")

    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "add code and docs"], check=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    db_path = fd._ensure_repo_graph(repo_path)
    assert db_path is not None
    assert db_path.exists()
    assert (repo_path / ".mythings" / "graph.meta.json").exists()

    meta = json.loads((repo_path / ".mythings" / "graph.meta.json").read_text())
    assert meta["commit_sha"] == head_sha

    from mythings.graph import CodebaseGraph
    graph = CodebaseGraph(db_path)
    symbols = graph.find_symbols("do_work")
    assert len(symbols) == 1
    assert symbols[0].name == "do_work"
    graph.close()

    # Second call returns cached path without rebuild
    db_path_cached = fd._ensure_repo_graph(repo_path)
    assert db_path_cached == db_path

    # New commit updates cache
    (pkg_dir / "worker.py").write_text("def do_work() -> int:\n    return 100\ndef new_fn(): pass\n")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "update"], check=True)
    new_sha = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_sha != head_sha

    db_path_updated = fd._ensure_repo_graph(repo_path)
    assert db_path_updated == db_path
    meta_updated = json.loads((repo_path / ".mythings" / "graph.meta.json").read_text())
    assert meta_updated["commit_sha"] == new_sha

    graph_updated = CodebaseGraph(db_path)
    assert len(graph_updated.find_symbols("new_fn")) == 1
    graph_updated.close()


def test_dispatch_one_injects_graph_path(tmp_path: Path, monkeypatch) -> None:
    candidate, account, ledger = _setup_dispatch_one_repo(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"

    src_dir = repo_path / "src" / "repo"
    src_dir.mkdir(parents=True)
    (src_dir / "core.py").write_text("def helper(): pass\n")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "add core"], check=True)

    captured_env = {}

    def fake_run(argv, **kwargs):
        if argv[0] == "mycoder":
            captured_env.update(kwargs.get("env", {}))
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"outcome": "success", "detail": "pr opened", "pr": 1}),
            )
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.0,
        max_turns=10,
        ledger=ledger,
        org="MyThingsLab",
    )

    assert "MYTHINGS_GRAPH_PATH" in captured_env
    graph_file = Path(captured_env["MYTHINGS_GRAPH_PATH"])
    assert graph_file.exists()
    assert graph_file.name == "graph.sqlite"


def test_main_preserves_orchestrator_priority_order_over_lane(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression test for #104. `mythings.labels.sort_key` ranks prio above
    # lane, so the orchestrator hands back a product P0 ahead of a kernel P3.
    # A second sort keyed on lane in the dispatcher made lane primary again and
    # flipped these two; with only one account, the flip decides which issue
    # gets worked and which waits for the next tick.
    dispatched: list[str] = []

    def fake_dispatch_one(account, candidate, **_kwargs):
        dispatched.append(candidate.id)

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(
            id="my-dashboard#5",
            repo="my-dashboard",  # lane:product, prio:P0
            tool="my-dashboard",
            title="P0 in a product repo",
            kind="issue",
            created_at="2026-09-13T20:00:00Z",
        ),
        fd.Candidate(
            id="my-fleet#80",
            repo="my-fleet",  # lane:kernel, prio:P3
            tool="my-fleet",
            title="P3 in a kernel repo",
            kind="issue",
            created_at="2026-09-13T20:00:00Z",
        ),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0
    assert dispatched[0] == "my-dashboard#5"


def test_pull_queue_fast_worker_pulls_multiple_candidates(tmp_path: Path, monkeypatch) -> None:
    dispatched: list[tuple[str, str]] = []
    dispatched_lock = threading.Lock()

    def fake_dispatch_one(account, candidate, **_kwargs):
        with dispatched_lock:
            dispatched.append((account.name, candidate.id))

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repoA#1", repo="repoA", tool="", title="t1", kind="issue", created_at="2026-09-13T20:00:00Z"),
        fd.Candidate(id="repoB#2", repo="repoB", tool="", title="t2", kind="issue", created_at="2026-09-13T20:00:01Z"),
        fd.Candidate(id="repoC#3", repo="repoC", tool="", title="t3", kind="issue", created_at="2026-09-13T20:00:02Z"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", str(tmp_path / "account1")])

    assert rc == 0
    # Single fast worker account pulled all 3 candidates from the queue
    assert [cid for _, cid in dispatched] == ["repoA#1", "repoB#2", "repoC#3"]
    assert [acc for acc, _ in dispatched] == ["account1", "account1", "account1"]


def test_pull_queue_respects_repo_leases(tmp_path: Path, monkeypatch) -> None:
    active_repos_seen: list[set[str]] = []
    current_active: set[str] = set()
    lock = threading.Lock()

    def fake_dispatch_one(account, candidate, **_kwargs):
        repo = candidate.repo
        with lock:
            assert repo not in current_active, f"Repo {repo} was leased concurrently!"
            current_active.add(repo)
            active_repos_seen.append(set(current_active))
        time.sleep(0.1)
        with lock:
            current_active.remove(repo)

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repoA#1", repo="repoA", tool="", title="t1", kind="issue", created_at="2026-09-13T20:00:00Z"),
        fd.Candidate(id="repoA#2", repo="repoA", tool="", title="t2", kind="issue", created_at="2026-09-13T20:00:01Z"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", f"{tmp_path / 'acc1'},{tmp_path / 'acc2'}"])

    assert rc == 0
    # Both repoA#1 and repoA#2 ran sequentially because repoA was leased
    assert len(active_repos_seen) == 2


def test_ensure_repo_graph_rebuilds_when_the_extractor_version_moved(tmp_path: Path) -> None:
    # A matching commit SHA is not enough: the repo can be unchanged while the
    # extractor has moved on. Reusing a cache in the old edge format is worse
    # than having none -- the traversal discards what it cannot parse, and the
    # Agent Context Pack then reports no callers and no tests.
    import sqlite3

    from mythings.graph import CodebaseGraph

    repo_path = tmp_path / "my-tool"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    (repo_path / "worker.py").write_text("def do_work() -> int:\n    return 42\n")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "add code"], check=True)

    db_path = fd._ensure_repo_graph(repo_path)
    assert db_path is not None

    # Same commit, cache written by a different extractor version.
    stale = sqlite3.connect(str(db_path))
    stale.execute("DELETE FROM nodes")
    stale.execute("PRAGMA user_version = 0")
    stale.commit()
    stale.close()

    rebuilt = fd._ensure_repo_graph(repo_path)
    assert rebuilt is not None
    graph = CodebaseGraph(rebuilt)
    assert len(graph.find_symbols("do_work")) == 1
    graph.close()


def test_route_candidate_params() -> None:
    # Mechanical small task: size:S + kind:chore -> budget 1.0, turns 20
    cand_small = fd.Candidate(
        id="repoA#1", repo="repoA", tool="", title="t1", kind="issue", created_at="2026-09-13T20:00:00Z",
        labels=("size:S", "kind:chore")
    )
    provider, budget, turns = fd.route_candidate_params(cand_small)
    assert provider == "claude"
    assert budget == 1.0
    assert turns == 20

    # Complex / non-small task: size:M + kind:feat -> default budget 3.0, turns 40
    cand_large = fd.Candidate(
        id="repoA#2", repo="repoA", tool="", title="t2", kind="issue", created_at="2026-09-13T20:00:00Z",
        labels=("size:M", "kind:feat")
    )
    provider, budget, turns = fd.route_candidate_params(cand_large)
    assert budget == 3.0
    assert turns == 40

    # When route=False: returns defaults
    provider, budget, turns = fd.route_candidate_params(cand_small, route=False)
    assert budget == 3.0
    assert turns == 40
