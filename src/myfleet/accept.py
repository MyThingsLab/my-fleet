#!/usr/bin/env python3
"""Decide whether an open PR may merge without a human, and say why.

This is the mechanism behind the workspace rule "merging is gated, not
forbidden". That rule lists four conditions, each of which has to be
established by something other than the merging agent's own judgement --
otherwise an agent merges its own work on its own say-so, which is the thing
the old blanket "never merge" rule was really guarding against. Those four are
`not_draft`, `main_protected`, `required_checks_pass` and `diff_in_scope`.

Three verdicts, and the distinction between the last two matters:

  ACCEPTED    -> every check passed; a caller with --execute may merge it
  REJECTED    -> a check failed on evidence. The PR is wrong, not unclear.
                 Reserved for facts, not estimates: a draft is REJECTED, an
                 oversized diff is not (the size label may be the wrong half).
  NEEDS_HUMAN -> no verdict is available: a check could not be evaluated, or
                 the diff touches something this gate is not allowed to clear.

Collapsing NEEDS_HUMAN into REJECTED would be a mistake. "I looked and it is
bad" and "I cannot tell" have opposite follow-ups -- the first is the worker's
to fix, the second is a human's to look at -- and a gate that cannot say "I
don't know" will eventually say "yes" instead.

## What this deliberately does not check

The #25 dry run measured a proposed gate against 30 real PRs before any of it
was built. `ledger_entry` would have failed 30 of 30, including PRs that were
plainly fine: it was measuring a convention nothing actually followed. A check
that never passes is not strict, it is broken, and it teaches whoever reads the
output to ignore the gate. It is not implemented here. `coverage_delta` is
absent for the same reason -- no repo in the fleet reports it per-PR, so the
check could only ever have been unevaluable.

`lint_clean` and `types_clean` are absent because they are not separable: every
repo runs ruff inside the same CI job `required_checks_pass` already reads, so a
separate check would either duplicate that evidence or invent new evidence by
running the tooling here, which is not what a gate reading GitHub should do.

Beyond the four conditions, `no_new_dependency` is checked but can only ever
pass or escalate -- see its comment in `assess()`.

## Trust boundary

Every input here comes from GitHub's API, never from the PR's own content.
A PR body claiming `Closes #7` is checked against the real issue #7; a branch
name is never evidence of anything. This matters because the PRs being judged
are increasingly written by agents, and an agent that learns the gate reads its
prose can write prose that satisfies it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum

from myfleet.fleet_dispatch import _checks_state

ORG = "MyThingsLab"

# Paths whose change always needs a human, however green the PR is. Each entry
# is (glob-ish prefix or suffix, why). The common thread: merging a bad change
# to one of these removes the ability to catch the next bad change, so they are
# exactly the files a gate must not be able to wave through.
#
# Applied to every repo, because none of these are one repo's problem.
SHARED_CARVE_OUTS: tuple[tuple[str, str], ...] = (
    # The constraints on agents. An agent that can merge a relaxation of its own
    # constraints has no constraints, only a delay.
    ("AGENTS.md", "the rules that constrain agents"),
    ("CLAUDE.md", "the rules that constrain agents"),
    ("GEMINI.md", "the rules that constrain agents"),
    ("HARNESS.md", "the build harness contract"),
    (".github/workflows/", "CI definitions -- the evidence this gate relies on"),
    (".claude/", "agent permissions and settings"),
    # Credentials. A redaction bug is invisible in a diff that looks tidy.
    ("_secrets.py", "credential handling"),
)

# The code deciding what agents may do, which is spread across repos: my-fleet
# dispatches and merges, my-coder opens the PRs, my-guard *is* the policy
# engine, and mythings.policy is the contract all three speak. A list shaped
# around my-fleet alone let my-coder#30 -- a change to exactly where the PR-open
# action is gated -- come back `accepted` (#41).
REPO_CARVE_OUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "my-fleet": (
        # A gate must never certify itself. If accept.py could accept a change
        # to accept.py, one bad merge silently widens every future merge.
        ("src/myfleet/accept.py", "the acceptance gate's own code"),
        ("src/myfleet/merge_ready_prs.py", "a myfleet merge path"),
        ("src/myfleet/merge_order_prs.py", "a myfleet merge path"),
        ("src/myfleet/fleet_dispatch.py", "the dispatch path that opens PRs"),
        ("src/myfleet/fleet_cycle.py", "the autonomous cycle loop driver"),
        ("src/myfleet/cycle_driver.py", "the cycle driver controls"),
        ("src/myfleet/preflight.py", "preflight dispatch invariants"),
    ),
    "my-coder": (
        ("src/mycoder/coder.py", "where the PR-open action is gated"),
        ("src/mycoder/cli.py", "decides whether a policy is wired at all"),
    ),
    # Whole package, not a file list: my-guard is the policy mechanism itself,
    # so a change anywhere in it can widen what every other tool may do.
    "my-guard": (("src/myguard/", "the policy engine every other tool defers to"),),
    "my-things-core": (("src/mythings/policy.py", "the policy contract"),),
}

# Churn a PR may contain, by the `size:` label on the issue it closes. The gate
# reuses the CAD label schema rather than inventing a second scale: the point of
# `diff_in_scope` is that the work matches what was *asked for*, and the size
# label is where "what was asked for" is already written down.
SIZE_BOUNDS: dict[str, int] = {"size:S": 150, "size:M": 600, "size:L": 2000}
DEFAULT_BOUND = 150


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class Check:
    name: str
    # True passed, False failed on evidence, None could not be evaluated. The
    # three-way is the whole point -- see the module docstring.
    passed: bool | None
    detail: str


@dataclass
class Assessment:
    repo: str
    number: int
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if any(c.passed is False for c in self.checks):
            return Verdict.REJECTED
        if any(c.passed is None for c in self.checks):
            return Verdict.NEEDS_HUMAN
        return Verdict.ACCEPTED

    @property
    def reason(self) -> str:
        blockers = [c for c in self.checks if c.passed is not True]
        if not blockers:
            return "all checks passed"
        return "; ".join(f"{c.name}: {c.detail}" for c in blockers)


def _gh(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(["gh", *argv], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def main_is_protected(repo: str) -> bool | None:
    # Not cosmetic. `_checks_state` can only report on checks branch protection
    # marks *required*; with no protection there are none, so an unprotected
    # repo has nothing that can ever constitute evidence. Returns None when the
    # answer itself is unavailable (network, token scope) rather than guessing.
    code, _, err = _gh(["api", f"repos/{ORG}/{repo}/branches/main/protection", "--silent"])
    if code == 0:
        return True
    if "Branch not protected" in err or "404" in err:
        return False
    return None


def closing_issue(body: str) -> int | None:
    # GitHub's own closing-keyword grammar, narrowed to this repo. A bare "#12"
    # is not a claim to close anything, and a cross-repo "owner/x#12" is out of
    # scope by construction.
    match = re.search(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", body or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def changed_files(repo: str, number: int) -> list[dict] | None:
    code, out, _ = _gh(["pr", "view", str(number), "--repo", f"{ORG}/{repo}", "--json", "files"])
    if code != 0:
        return None
    return json.loads(out).get("files") or []


def _requirement_name(spec: str) -> str:
    # "my-things-core @ git+https://...@v1.0.0" -> "my-things-core";
    # "pytest-cov>=5" -> "pytest-cov". Only the distribution name matters here:
    # a version bump is a `kind:chore` this gate is happy to clear, a brand new
    # import in the dependency tree is not.
    name = re.split(r"[<>=!~;@\[\s]", spec.strip(), maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def dependency_names(repo: str, path: str, ref: str) -> frozenset[str] | None:
    # None means "could not read", never "no dependencies" -- an unreadable
    # pyproject has to stay unevaluable rather than collapse into an empty set
    # that makes every dependency look pre-existing.
    code, out, _ = _gh(
        [
            "api",
            f"repos/{ORG}/{repo}/contents/{path}?ref={ref}",
            "-H",
            "Accept: application/vnd.github.raw",
        ]
    )
    if code != 0:
        return None
    try:
        data = tomllib.loads(out)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project") or {}
    specs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group)
    return frozenset(_requirement_name(s) for s in specs if isinstance(s, str))


def pyproject_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if p == "pyproject.toml" or p.endswith("/pyproject.toml")]


def new_dependencies(repo: str, paths: list[str], base: str, head: str) -> frozenset[str] | None:
    # Every changed pyproject.toml, compared name-set to name-set across the
    # PR's two ends. None if any of them could not be read.
    added: set[str] = set()
    for path in paths:
        before = dependency_names(repo, path, base)
        after = dependency_names(repo, path, head)
        if before is None or after is None:
            return None
        added |= after - before
    return frozenset(added)


def carve_outs_for_repo(repo: str) -> tuple[tuple[str, str], ...]:
    # An unknown repo gets the shared set, never an empty one. Most of the ~44
    # repos are not in the map and never will be, so the default is the case
    # that matters: forgetting to add a repo must not silently disable its
    # carve-outs.
    return SHARED_CARVE_OUTS + REPO_CARVE_OUTS.get(repo, ())


def carve_out_for(paths: list[str], repo: str) -> tuple[str, str] | None:
    for path in paths:
        for pattern, why in carve_outs_for_repo(repo):
            if pattern.endswith("/"):
                hit = path.startswith(pattern) or f"/{pattern}" in f"/{path}"
            else:
                hit = path == pattern or path.endswith(f"/{pattern}")
            if hit:
                return path, why
    return None


def assess(repo: str, number: int) -> Assessment:
    found = Assessment(repo=repo, number=number)
    code, out, _ = _gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            f"{ORG}/{repo}",
            "--json",
            "isDraft,body,mergeable,mergeStateStatus,state,baseRefName,headRefOid",
        ]
    )
    if code != 0:
        found.checks.append(Check("pr_readable", None, "could not read the PR from GitHub"))
        return found
    pr = json.loads(out)

    if pr["state"] != "OPEN":
        found.checks.append(Check("pr_open", None, f"state is {pr['state']}"))
        return found

    found.checks.append(
        Check("not_draft", not pr["isDraft"], "draft" if pr["isDraft"] else "ready for review")
    )

    protected = main_is_protected(repo)
    found.checks.append(
        Check(
            "main_protected",
            protected,
            {
                True: "main requires a PR and a green check",
                False: "main has no branch protection, so no check is required and none can be evidence",
                None: "could not read branch protection",
            }[protected],
        )
    )

    # Only meaningful once protection is confirmed: with none, "required checks"
    # is an empty set and `pass` would mean nothing was checked.
    if protected is True:
        state = _checks_state(ORG, repo, number)
        found.checks.append(
            Check(
                "required_checks_pass",
                True if state == "pass" else (False if state == "fail" else None),
                f"required checks report {state!r}",
            )
        )

    files = changed_files(repo, number)
    if files is None:
        found.checks.append(Check("diff_readable", None, "could not read the PR diff"))
        return found
    paths = [f["path"] for f in files]

    carved = carve_out_for(paths, repo)
    if carved:
        path, why = carved
        found.checks.append(Check("no_carve_out", None, f"touches {path} — {why}"))

    # A new dependency is never REJECTED -- taking one on is a legitimate
    # decision, just not this gate's to make. Note what is *not* accepted as
    # evidence: the issue's "or one declared" half would have let a PR body
    # naming the dependency clear the check, and a PR body is exactly the input
    # the trust boundary above refuses to read. There is no way to declare a
    # dependency to this gate; there is only a human.
    manifests = pyproject_paths(paths)
    if not manifests:
        found.checks.append(Check("no_new_dependency", True, "no pyproject.toml in the diff"))
    else:
        added = new_dependencies(repo, manifests, pr["baseRefName"], pr["headRefOid"])
        if added is None:
            found.checks.append(
                Check("no_new_dependency", None, "could not read a changed pyproject.toml")
            )
        elif added:
            found.checks.append(Check("no_new_dependency", None, f"adds {', '.join(sorted(added))}"))
        else:
            found.checks.append(Check("no_new_dependency", True, "no dependency added"))

    issue = closing_issue(pr.get("body") or "")
    if issue is None:
        found.checks.append(
            Check(
                "diff_in_scope", None, "no 'Closes #N' — nothing states what this was meant to do"
            )
        )
        return found

    code, out, _ = _gh(
        ["issue", "view", str(issue), "--repo", f"{ORG}/{repo}", "--json", "labels,state"]
    )
    if code != 0:
        found.checks.append(Check("diff_in_scope", None, f"#{issue} could not be read"))
        return found
    labels = {obj["name"] for obj in json.loads(out)["labels"]}
    bound = next((SIZE_BOUNDS[s] for s in SIZE_BOUNDS if s in labels), DEFAULT_BOUND)
    churn = sum(f["additions"] + f["deletions"] for f in files)
    # Overshooting the bound is None, never False. A `size:` label is an estimate
    # made before the work existed, so a diff exceeding it is evidence the
    # estimate was wrong at least as often as it is evidence the diff is. The
    # first real corpus run rejected two genuinely good PRs this way -- a new
    # seam with tests and an ADR is simply not `size:S`, and the label was the
    # thing at fault. REJECTED would throw that work away; NEEDS_HUMAN routes it
    # to the one party who can tell the two cases apart.
    found.checks.append(
        Check(
            "diff_in_scope",
            True if churn <= bound else None,
            f"{churn} lines changed against a {bound}-line bound for #{issue}"
            + (
                ""
                if churn <= bound
                else " — bigger than the label predicted, so scope needs a look"
            ),
        )
    )
    return found


def merge_pr(repo: str, number: int, *, retries: int = 4) -> None:
    for attempt in range(retries):
        code, _, err = _gh(
            [
                "pr",
                "merge",
                str(number),
                "--repo",
                f"{ORG}/{repo}",
                "--squash",
                "--delete-branch",
            ]
        )
        if code == 0:
            return
        if any(t in err for t in ("Base branch was modified", "not mergeable", "not up to date")) and attempt < retries - 1:
            time.sleep(10)
            continue
        raise RuntimeError(f"gh pr merge {repo}#{number} failed: {err.strip()}")


def list_open_prs_in_org(org: str = ORG) -> list[tuple[str, int, str, bool]]:
    code, out, _ = _gh(
        ["search", "prs", "--owner", org, "--state", "open", "--limit", "200", "--json", "number,title,repository,isDraft"]
    )
    if code != 0:
        return []
    items = json.loads(out)
    res = []
    for item in items:
        repo_name = item.get("repository", {}).get("name")
        number = item.get("number")
        title = item.get("title", "")
        is_draft = item.get("isDraft", False)
        if repo_name and number:
            res.append((repo_name, number, title, is_draft))
    return res


def settle(
    *,
    execute: bool = False,
    ask_human: bool = False,
    ledger: object | None = None,
    repo_filter: list[str] | None = None,
) -> list[Assessment]:
    open_prs = list_open_prs_in_org(ORG)
    assessments: list[Assessment] = []
    guard = None
    if ask_human:
        try:
            from myguard import Guard
            from mythings.policy import Action, Decision
            from myguard.rules import MERGE_ACTION
            guard = Guard()
        except ImportError:
            guard = None

    for repo, number, title, is_draft in open_prs:
        if repo_filter and repo not in repo_filter:
            continue
        assessment = assess(repo, number)
        assessments.append(assessment)
        verdict = assessment.verdict
        failing_checks = [c.name for c in assessment.checks if c.passed is not True]

        if verdict is Verdict.ACCEPTED:
            if execute:
                try:
                    merge_pr(repo, number)
                    print(f"ACCEPTED & MERGED: {repo}#{number} {title!r}")
                    if ledger and hasattr(ledger, "record"):
                        ledger.record(
                            tool="fleet_accept",
                            kind="accept",
                            outcome="merged",
                            repo=repo,
                            pr=number,
                            detail=assessment.reason,
                        )
                except Exception as exc:
                    print(f"ACCEPTED but merge FAILED: {repo}#{number}: {exc}", file=sys.stderr)
                    if ledger and hasattr(ledger, "record"):
                        ledger.record(
                            tool="fleet_accept",
                            kind="accept",
                            outcome="merge_failed",
                            repo=repo,
                            pr=number,
                            detail=str(exc),
                        )
            else:
                print(f"ACCEPTED (dry run): {repo}#{number} {title!r}")
                if ledger and hasattr(ledger, "record"):
                    ledger.record(
                        tool="fleet_accept",
                        kind="accept",
                        outcome="accepted",
                        repo=repo,
                        pr=number,
                        detail=assessment.reason,
                    )
        elif verdict is Verdict.NEEDS_HUMAN:
            approved = False
            if ask_human and guard is not None:
                from mythings.policy import Action, Decision
                from myguard.rules import MERGE_ACTION
                action = Action(
                    kind=MERGE_ACTION,
                    payload={"repo": f"{ORG}/{repo}", "number": number, "title": title},
                )
                decision = guard.evaluate(action).under(unattended=True)
                if decision is Decision.ALLOW:
                    approved = True
                    print(f"NEEDS_HUMAN -> APPROVED over Telegram: {repo}#{number} {title!r}")
                    if execute:
                        try:
                            merge_pr(repo, number)
                            print(f"MERGED via Telegram approval: {repo}#{number}")
                            if ledger and hasattr(ledger, "record"):
                                ledger.record(
                                    tool="fleet_accept",
                                    kind="accept",
                                    outcome="human_approved_and_merged",
                                    repo=repo,
                                    pr=number,
                                    detail=assessment.reason,
                                )
                        except Exception as exc:
                            print(f"FAILED to merge after approval: {repo}#{number}: {exc}", file=sys.stderr)
                            if ledger and hasattr(ledger, "record"):
                                ledger.record(
                                    tool="fleet_accept",
                                    kind="accept",
                                    outcome="merge_failed",
                                    repo=repo,
                                    pr=number,
                                    detail=str(exc),
                                )
                    else:
                        if ledger and hasattr(ledger, "record"):
                            ledger.record(
                                tool="fleet_accept",
                                kind="accept",
                                outcome="human_approved",
                                repo=repo,
                                pr=number,
                                detail=assessment.reason,
                            )
            if not approved:
                print(f"NEEDS_HUMAN: {repo}#{number} — {assessment.reason}")
                if ledger and hasattr(ledger, "record"):
                    ledger.record(
                        tool="fleet_accept",
                        kind="accept",
                        outcome="needs_human",
                        repo=repo,
                        pr=number,
                        detail=assessment.reason,
                        failing_checks=failing_checks,
                    )
        else:
            print(f"REJECTED: {repo}#{number} — {assessment.reason}")
            if ledger and hasattr(ledger, "record"):
                ledger.record(
                    tool="fleet_accept",
                    kind="accept",
                    outcome="rejected",
                    repo=repo,
                    pr=number,
                    detail=assessment.reason,
                    failing_checks=failing_checks,
                )
    return assessments


def report(assessments: list[Assessment], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "repo": a.repo,
                        "number": a.number,
                        "verdict": a.verdict.value,
                        "reason": a.reason,
                        "checks": [
                            {"name": c.name, "passed": c.passed, "detail": c.detail}
                            for c in a.checks
                        ],
                    }
                    for a in assessments
                ],
                indent=2,
            )
        )
        return
    for a in assessments:
        print(f"{a.repo}#{a.number}: {a.verdict.value.upper()}")
        for c in a.checks:
            mark = {True: "pass", False: "FAIL", None: "????"}[c.passed]
            print(f"    {mark}  {c.name}: {c.detail}")
    tally = {v: sum(1 for a in assessments if a.verdict is v) for v in Verdict}
    print()
    print("  ".join(f"{v.value}={n}" for v, n in tally.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", action="append", help="repo name within the org (repeatable)")
    parser.add_argument(
        "--pr", type=int, action="append", help="PR number (repeatable)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--settle", action="store_true", help="run settlement pass across org open PRs"
    )
    parser.add_argument(
        "--execute", action="store_true", help="actually merge accepted PRs"
    )
    parser.add_argument(
        "--ask-human", action="store_true", help="route needs_human PRs to Telegram ask channel"
    )
    args = parser.parse_args(argv)

    if args.settle:
        from mythings.ledger import Ledger
        from myfleet.fleet_dispatch import DISPATCH_LEDGER
        ledger = Ledger(DISPATCH_LEDGER)
        assessments = settle(
            execute=args.execute,
            ask_human=args.ask_human,
            ledger=ledger,
            repo_filter=args.repo,
        )
        return 0 if all(a.verdict is Verdict.ACCEPTED for a in assessments) else 1

    if not args.repo or not args.pr:
        parser.error("either --settle or both --repo and --pr are required")

    repo_name = args.repo[0]
    assessments = [assess(repo_name, n) for n in args.pr]
    report(assessments, as_json=args.as_json)
    return 0 if all(a.verdict is Verdict.ACCEPTED for a in assessments) else 1


if __name__ == "__main__":
    sys.exit(main())
