#!/usr/bin/env python3
"""Apply one deterministic change across every repo that needs it, as one batch.

Some changes are not a repo's own business but the fleet's: re-vendoring
`HARNESS.md` when core's canonical `harness.md` moves, adjusting the agent
instructions, retiring a seam. There are ~52 repos with a test suite, and until
now the only tooling for this was `mythings._harness.revendor`, which rewrites
the local copies and stops -- leaving a human to hand-drive branch, commit,
push and `gh pr create` fifty times. In practice nobody finishes: at the time
this was written my-telegram-bot and my-fleet were both parked on a stale
`docs/revendor-harness` branch and my-telegram-bot's `main` was red on
`test_harness_drift` for want of a one-file copy.

What this adds over `revendor`:

  * It goes all the way to a PR, one per repo, in a branch named for the sweep,
    so re-running is idempotent -- an existing open PR on that branch counts as
    done rather than opening a second one.
  * It works in a throwaway git worktree off `origin/main`, never in the
    checkout you have open. A sweep cannot clobber your working tree or move
    the branch you had checked out.
  * A dry run is the real thing with the push removed, so "what would this do"
    and "what did this do" cannot drift apart.
  * It refuses, by default, to open a PR that CI will not check (see below).

What it deliberately does NOT do is merge. `myfleet.accept` owns that, and the
changes a sweep is best at -- `HARNESS.md`, agent instructions, CI workflows --
are on the workspace's "needs a human however green they are" list precisely
because merging a bad one destroys the ability to catch the next one.

    python -m myfleet.sweep harness                 # dry run: who needs it
    python -m myfleet.sweep harness --execute       # branch, commit, push, PR
    python -m myfleet.sweep harness --only my-guard my-coder
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mythings._harness import harness_text
from mythings.ledger import Ledger

from myfleet.workspace import fleet_root, ledger_path

TOOL = "fleet_sweep"
KIND = "sweep"

WORKSPACE_ROOT = fleet_root(__file__)
DEFAULT_LEDGER = ledger_path(WORKSPACE_ROOT)

# (argv, cwd) -> (exit code, stdout). The only subprocess boundary; tests fake it.
Runner = Callable[[list[str], Path], "tuple[int, str]"]


# `git commit` runs the repo's pre-commit hooks, and the fleet's are
# `language: system` -- `ruff check`, `pytest -q`. A scratch worktree inherits
# no activated virtualenv, so those resolve against a bare PATH and the commit
# dies on a conftest `ModuleNotFoundError: mythings`, which reads exactly like
# the sweep having broken the repo. Putting the shared venv on PATH lets the
# hooks actually run and actually pass -- far better than reaching for
# `--no-verify`, which would commit straight past a real gate.
VENV_BIN = WORKSPACE_ROOT / ".venv" / "bin"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if VENV_BIN.is_dir():
        env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=_env())
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass(frozen=True)
class Transform:
    """One deterministic edit, plus enough about it to describe a PR."""

    name: str
    title: str
    body: str
    # Edits the checkout in place. Returns nothing: whether anything actually
    # changed is decided by `git status`, not by the transform's own opinion,
    # so a transform that lies about it cannot produce an empty PR.
    apply: Callable[[Path], None]


def _revendor_harness(repo: Path) -> None:
    target = repo / "HARNESS.md"
    if target.exists():
        target.write_text(harness_text(), encoding="utf-8")


# The workflows spell it on one line and identically across the fleet:
#     paths-ignore: ["**.md", "docs/**", "dev-ledger/**"]
# Anything else is left alone and reported, rather than guessed at -- a regex
# that half-understands a CI trigger is how a repo ends up with no CI at all.
_PATHS_IGNORE_LINE = re.compile(r'^(\s*paths-ignore:\s*\[)(.*)(\]\s*)$')


def _drop_md_from_paths_ignore(repo: Path) -> None:
    for workflow in sorted((repo / ".github" / "workflows").glob("*.yml")):
        lines = workflow.read_text(encoding="utf-8").splitlines(keepends=True)
        out = []
        for line in lines:
            match = _PATHS_IGNORE_LINE.match(line.rstrip("\n"))
            if match is None:
                out.append(line)
                continue
            kept = [
                item
                for item in (i.strip() for i in match.group(2).split(","))
                if item and item not in ('"**.md"', "'**.md'")
            ]
            newline = "\n" if line.endswith("\n") else ""
            if kept:
                out.append(f"{match.group(1)}{', '.join(kept)}{match.group(3)}{newline}")
            # An ignore list that is now empty is dropped entirely: an empty
            # `paths-ignore: []` is not the same trigger as no paths-ignore key.
        workflow.write_text("".join(out), encoding="utf-8")


TRANSFORMS = {
    "harness": Transform(
        name="harness",
        title="docs(harness): re-vendor the canonical HARNESS.md",
        body=(
            "`my-things-core`'s `harness.md` is canonical; this repo's `HARNESS.md` "
            "is a vendored copy and had drifted, which fails `test_harness_drift`.\n\n"
            "Opened by `python -m myfleet.sweep harness`. Deterministic file copy — "
            "no judgement, no Engine call."
        ),
        apply=_revendor_harness,
    ),
    "ci-md": Transform(
        name="ci-md",
        title="ci: let markdown-only changes run the test suite",
        body=(
            "`paths-ignore: [\"**.md\", ...]` means a docs-only PR triggers no run, "
            "so it gets no `test` check. Where `test` is a required check that PR can "
            "never merge, and `myfleet.accept` correctly refuses it (a skipped check "
            "is not a pass). That makes every fleet-wide documentation change — "
            "`HARNESS.md`, agent instructions — permanently unmergeable.\n\n"
            "Dropping `**.md` costs one cheap run per docs PR and buys back the "
            "ability to land them.\n\n"
            "Opened by `python -m myfleet.sweep ci-md`. **Needs a human:** CI "
            "workflows are on the workspace's no-autonomous-merge list."
        ),
        apply=_drop_md_from_paths_ignore,
    ),
}


# GitHub's `paths-ignore` globs are not fnmatch: `**.md` means "any .md at any
# depth" and `docs/**` means "anything under docs/". Only these two shapes and a
# literal path are recognised. Anything else returns False -- unrecognised must
# mean "assume CI runs", so the worst case is a PR we flagged as fine that turns
# out not to be checked, which `accept` then refuses visibly. The opposite
# default would silently skip repos that were never actually blocked.
def _ignored_by(pattern: str, path: str) -> bool:
    pattern = pattern.strip().strip("\"'")
    if pattern.startswith("**") and "/" not in pattern[2:]:
        return path.endswith(pattern[2:])
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern


def ci_would_skip(repo: Path, changed: list[str]) -> bool:
    """True when every changed path is excluded from every workflow trigger.

    A PR like that gets no `test` check at all, which under a required-check
    branch protection is not "untested" but "unmergeable, forever".
    """
    workflows = sorted((repo / ".github" / "workflows").glob("*.yml"))
    if not workflows or not changed:
        return False
    patterns: list[str] = []
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = _PATHS_IGNORE_LINE.match(line)
            if match:
                patterns += [p for p in match.group(2).split(",") if p.strip()]
    if not patterns:
        return False
    return all(any(_ignored_by(p, path) for p in patterns) for path in changed)


@dataclass
class RepoOutcome:
    repo: str
    state: str  # current | would_change | opened | in_flight | ci_blocked | failed
    detail: str = ""
    changed: list[str] = field(default_factory=list)
    url: str = ""


def sweep_repos(root: Path, only: list[str] | None = None) -> list[Path]:
    repos = sorted(p.parent for p in root.glob("*/.git") if p.parent.name.startswith("my-"))
    if only:
        known = {r.name: r for r in repos}
        missing = [name for name in only if name not in known]
        if missing:
            raise SystemExit(f"no such repo(s) under {root}: {', '.join(missing)}")
        repos = [known[name] for name in only]
    return repos


def branch_for(transform: Transform) -> str:
    return f"sweep/{transform.name}"


def _existing_pr(repo: Path, branch: str, runner: Runner) -> str:
    code, out = runner(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
        repo,
    )
    if code != 0:
        return ""
    try:
        found = json.loads(out or "[]")
    except json.JSONDecodeError:
        return ""
    return str(found[0]["url"]) if found else ""


def archived_repos(org: str, *, runner: Runner = _run, cwd: Path | None = None) -> set[str]:
    """Names in `org` that are archived, hence read-only, hence unsweepable.

    One org-wide call rather than one per repo: a sweep already makes two
    network round-trips per repo and this would be a third.
    """
    code, out = runner(
        ["gh", "repo", "list", org, "--json", "name,isArchived", "--limit", "300"],
        cwd or Path.cwd(),
    )
    if code != 0:
        return set()
    try:
        return {str(e["name"]) for e in json.loads(out) if e.get("isArchived")}
    except (json.JSONDecodeError, TypeError):
        return set()


def apply_to(
    repo: Path,
    transform: Transform,
    *,
    execute: bool,
    allow_unchecked: bool,
    archived: frozenset[str] = frozenset(),
    runner: Runner = _run,
) -> RepoOutcome:
    """Run `transform` against a throwaway worktree of `repo` off origin/main."""
    name = repo.name
    branch = branch_for(transform)

    if name in archived:
        # An archived repo is read-only: the push cannot succeed, now or ever.
        # Reporting it as `failed` makes a permanent condition look like a
        # transient one and buries the real failures under it every single run.
        return RepoOutcome(name, "archived", "archived on GitHub, read-only")

    existing = _existing_pr(repo, branch, runner)
    if existing:
        # Idempotence. Re-running a sweep must never open a second PR, and a
        # sweep that is half-landed is the normal state, not an error.
        return RepoOutcome(name, "in_flight", "an open PR already carries it", url=existing)

    code, out = runner(["git", "fetch", "origin", "--quiet"], repo)
    if code != 0:
        return RepoOutcome(name, "failed", f"git fetch: {out}")

    scratch = Path(tempfile.mkdtemp(prefix=f"sweep-{transform.name}-"))
    tree = scratch / name
    try:
        code, out = runner(
            ["git", "worktree", "add", "--detach", str(tree), "origin/main"], repo
        )
        if code != 0:
            return RepoOutcome(name, "failed", f"git worktree add: {out}")

        transform.apply(tree)
        code, out = runner(["git", "status", "--porcelain"], tree)
        if code != 0:
            return RepoOutcome(name, "failed", f"git status: {out}")
        # ` M path` / `?? path` / `MM path`. Split rather than slice a fixed
        # offset: `_run` strips the output, which eats the leading status column
        # on the first line only, so an offset silently loses one character from
        # exactly one filename per repo.
        changed = sorted(
            line.strip().split(maxsplit=1)[-1] for line in out.splitlines() if line.strip()
        )
        if not changed:
            return RepoOutcome(name, "current", "already up to date")

        if ci_would_skip(tree, changed) and not allow_unchecked:
            return RepoOutcome(
                name,
                "ci_blocked",
                "every changed path is in paths-ignore, so this PR would get no "
                "`test` check and could never be merged",
                changed=changed,
            )

        if not execute:
            return RepoOutcome(name, "would_change", f"{len(changed)} file(s)", changed=changed)

        for argv in (
            ["git", "checkout", "-b", branch],
            ["git", "add", "--all"],
            ["git", "commit", "-m", transform.title],
            ["git", "push", "--set-upstream", "origin", branch],
        ):
            code, out = runner(argv, tree)
            if code != 0:
                return RepoOutcome(name, "failed", f"{' '.join(argv[:2])}: {out}", changed=changed)

        code, out = runner(
            ["gh", "pr", "create", "--title", transform.title, "--body", transform.body,
             "--base", "main", "--head", branch],
            tree,
        )
        if code != 0:
            return RepoOutcome(name, "failed", f"gh pr create: {out}", changed=changed)
        return RepoOutcome(name, "opened", transform.title, changed=changed, url=out.splitlines()[-1])
    finally:
        # The worktree is registered in the *real* repo's .git, so dropping the
        # temp dir alone would leave it listed forever as a prunable stale entry.
        runner(["git", "worktree", "remove", "--force", str(tree)], repo)
        shutil.rmtree(scratch, ignore_errors=True)


_ORDER = ["failed", "ci_blocked", "opened", "would_change", "in_flight", "archived", "current"]
_ICON = {
    "failed": "❌",
    "ci_blocked": "🚧",
    "opened": "🚢",
    "would_change": "✏️",
    "in_flight": "⏳",
    "archived": "📦",
    "current": "✅",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transform", choices=sorted(TRANSFORMS), help="which change to fan out")
    parser.add_argument("--root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--only", nargs="+", metavar="REPO")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="branch, commit, push and open a PR (default is a dry run that does "
        "everything but push)",
    )
    parser.add_argument(
        "--allow-unchecked",
        action="store_true",
        help="open a PR even where paths-ignore means CI will not check it",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--jobs", type=int, default=8, help="repos to sweep concurrently")
    parser.add_argument("--org", default="MyThingsLab", help="org to read archived repos from")
    args = parser.parse_args(argv)

    transform = TRANSFORMS[args.transform]
    repos = sweep_repos(args.root, args.only)
    archived = frozenset(archived_repos(args.org, cwd=args.root))
    # Two network round-trips per repo (`git fetch`, `gh pr list`) times fifty
    # repos is minutes of wall clock, serially -- long enough that the first
    # fleet-scale run of this was killed by a timeout rather than finishing.
    # Each repo has its own checkout, its own worktree and its own scratch dir,
    # so they share nothing; the bound is GitHub's rate limiter, not us.
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outcomes = list(
            pool.map(
                lambda repo: apply_to(
                    repo,
                    transform,
                    execute=args.execute,
                    allow_unchecked=args.allow_unchecked,
                    archived=archived,
                ),
                repos,
            )
        )

    by_state: dict[str, list[RepoOutcome]] = {}
    for outcome in outcomes:
        by_state.setdefault(outcome.state, []).append(outcome)
    for state in _ORDER:
        for outcome in by_state.get(state, []):
            line = f"{_ICON[state]} {outcome.repo}: {outcome.detail}"
            print(f"{line} {outcome.url}".rstrip())

    counts = {state: len(by_state.get(state, [])) for state in _ORDER if by_state.get(state)}
    print(f"\n{len(repos)} repos scanned: " + ", ".join(f"{n} {s}" for s, n in counts.items()))
    if not args.execute and by_state.get("would_change"):
        print("(dry run — pass --execute to open the PRs)")
    if by_state.get("ci_blocked"):
        print(
            "\n🚧 Those repos ignore every path this sweep touches, so their PR would "
            "carry no `test` check and could never be merged.\n"
            "   Run `python -m myfleet.sweep ci-md --execute` first, or re-run with "
            "--allow-unchecked to open them anyway."
        )

    Ledger(args.ledger).record(
        tool=TOOL,
        kind=KIND,
        outcome="success" if not by_state.get("failed") else "failure",
        detail=f"{transform.name}: " + ", ".join(f"{n} {s}" for s, n in counts.items()),
        transform=transform.name,
        execute=args.execute,
        counts=counts,
    )
    return 1 if by_state.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
