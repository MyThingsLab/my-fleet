# The local lane: code work the fleet does without GitHub. The September deploy opened 79 pull
# requests in 37 repositories over two days and none could merge unattended (164 accept checks
# said "needs a human", 45 rejected): most were one change copied into every repository, each
# waiting on CI and a human merge. Here a change is made in a worktree cut from each
# repository's main, gated locally, and left on a local branch for the author to read and merge.
# Nothing is pushed; publishing is a batch the author starts.
#
# The gate is the repository's own suite, run before and after the change on the same tree: the
# change passes when no test that passed before fails after. Mains are not always green (a stale
# HARNESS.md fails its drift test), and "every test passes" would refuse every change there.
#
# The first task is mechanical: tools importing a private core name that now has a public one
# (mythings.gaps.PUBLIC) are moved to it by mythings.gaps.rewrite, with no model call.
#
#     python -m myfleet.local_lane --core PATH                 # dry run: what would change where
#     python -m myfleet.local_lane --core PATH --execute       # change, gate, commit locally
#     python -m myfleet.local_lane --core PATH --repos my-todo --execute
from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mythings import gaps, paths
from mythings.instructions import harness_text
from mythings.ledger import Ledger

from myfleet.workspace import fleet_root, ledger_path

TOOL = "local_lane"
TASK = "public-core-names"
BRANCH = f"lane/{TASK}"
SUITE_SECONDS = 900
_REPORTED = re.compile(r"^(FAILED|ERROR) (\S+)")

Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Suite:
    failed: frozenset[str]  # node ids that failed or errored, plus "<collection>" when it broke
    code: int


@dataclass(frozen=True)
class Outcome:
    repo: str
    outcome: str  # success, failed, skipped
    detail: str
    files: tuple[str, ...] = ()
    new_failures: tuple[str, ...] = field(default_factory=tuple)


def _git(repo: Path, *argv: str, run: Run = subprocess.run) -> str:
    proc = run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _has_branch(repo: Path, name: str) -> bool:
    try:
        _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    except subprocess.CalledProcessError:
        return False
    return True


def targets(core: Path, lab: Path) -> dict[str, Path]:
    # The repositories a gap with a public replacement names, by name.
    found = gaps.find(core, gaps.lab_repos(lab, core))
    names = {
        where.split(":", 1)[0]
        for gap in found
        if gap.kind == "private" and gap.what in gaps.PUBLIC
        for where in gap.where
    }
    return {name: lab / name for name in sorted(names)}


def rewrite_tree(tree: Path) -> list[str]:
    # Apply mythings.gaps.rewrite to every Python file of the checkout; the paths it changed.
    changed = []
    for path in sorted([*tree.glob("src/**/*.py"), *tree.glob("tests/**/*.py")]):
        source = path.read_text(encoding="utf-8")
        new = gaps.rewrite(source)
        if new != source:
            path.write_text(new, encoding="utf-8")
            changed.append(str(path.relative_to(tree)))
    return changed


def revendor(tree: Path) -> list[str]:
    # A HARNESS.md that is not core's harness.md fails the repository's drift test, so every
    # commit hook on that main refuses. Re-vendoring it is the fix the September deploy made as
    # one pull request per repository (58 of its accept checks); here it rides on the lane's
    # branch. Only a repository that already vendors one is touched.
    vendored = tree / "HARNESS.md"
    canonical = harness_text()
    if not vendored.is_file() or vendored.read_text(encoding="utf-8") == canonical:
        return []
    vendored.write_text(canonical, encoding="utf-8")
    return ["HARNESS.md"]


def parse_suite(output: str, code: int) -> Suite:
    failed = {m.group(2) for line in output.splitlines() if (m := _REPORTED.match(line))}
    if code not in (0, 1, 5):  # 2: interrupted, 3: internal error, 4: usage: nothing ran
        failed.add("<collection>")
    return Suite(failed=frozenset(failed), code=code)


def _env(tree: Path, core: Path) -> dict[str, str]:
    # The tree's own package and the given core imported first, with the lab's Python and its
    # tools (pytest, ruff) first on PATH: the suite and the repository's commit hooks see the
    # same code.
    return {
        **os.environ,
        "PATH": f"{paths.lab_python().parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"{tree / 'src'}{os.pathsep}{core / 'src'}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_suite(tree: Path, core: Path, *, run: Run = subprocess.run) -> Suite:
    # The repository's tests on this tree, with the lab's Python (every tool's dependencies).
    env = _env(tree, core)
    argv = [str(paths.lab_python()), "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE"]
    try:
        proc = run(argv, cwd=tree, env=env, capture_output=True, text=True, timeout=SUITE_SECONDS)
    except subprocess.TimeoutExpired:
        return Suite(failed=frozenset({"<timeout>"}), code=-1)
    return parse_suite(proc.stdout, proc.returncode)


def _sort_imports(tree: Path, files: Sequence[str], run: Run) -> None:
    # Only the import blocks of the files the rewrite touched, so the diff stays the rewrite.
    ruff = paths.lab_python().parent / "ruff"
    run([str(ruff), "check", "--select", "I", "--fix", "-q", *files], cwd=tree, check=False)


def _commit(tree: Path, files: Sequence[str], core: Path, run: Run) -> str:
    # Commit through the repository's own hooks, never around them; why they refused, or "".
    _git(tree, "add", "--", *files, run=run)
    moved = ", ".join(f"{old} -> {new}" for old, new in gaps.PUBLIC.items())
    message = (
        "Import core's public names instead of its private ones\n\n"
        f"{moved}.\n"
        + (
            "HARNESS.md re-vendored from my-things-core's harness.md.\n"
            if "HARNESS.md" in files
            else ""
        )
        + "Made by myfleet.local_lane (mythings.gaps.rewrite), gated on this repository's suite.\n"
    )
    argv = ["git", "-C", str(tree), "commit", "-q", "-m", message]
    proc = run(argv, env=_env(tree, core), capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return ""
    said = (proc.stdout + proc.stderr).strip().splitlines()
    # pre-commit prints every hook; the reason is the first FAILED test, else the failed hook.
    for mark in ("FAILED ", "Failed"):
        if hit := next((line.strip() for line in said if mark in line), None):
            return hit
    return said[-1] if said else f"git commit exited {proc.returncode}"


def change_one(repo: Path, core: Path, *, run: Run = subprocess.run) -> Outcome:
    if _has_branch(repo, BRANCH):
        return Outcome(repo.name, "skipped", f"{BRANCH} already waits for review")
    if not _has_branch(repo, "main"):
        return Outcome(repo.name, "skipped", "no local main to start from")
    tree = repo / ".claude" / "worktrees" / f"lane-{TASK}"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", BRANCH, "main", run=run)
    keep = False
    try:
        outcome = _gated(repo, tree, core, run)
        keep = outcome.outcome == "success"
        return outcome
    finally:
        _git(repo, "worktree", "remove", "--force", str(tree), run=run)
        if not keep:
            _git(repo, "branch", "-D", BRANCH, run=run)


def _gated(repo: Path, tree: Path, core: Path, run: Run) -> Outcome:
    before = run_suite(tree, core, run=run)
    files = rewrite_tree(tree)
    if not files:
        return Outcome(repo.name, "skipped", "main has nothing to move (the use is on a branch)")
    _sort_imports(tree, files, run)
    files += revendor(tree)
    after = run_suite(tree, core, run=run)
    new = tuple(sorted(after.failed - before.failed))
    if new:
        detail = f"{len(new)} test(s) that passed on main fail after the change"
        return Outcome(repo.name, "failed", detail, tuple(files), new)
    if refused := _commit(tree, files, core, run):
        return Outcome(repo.name, "failed", f"the commit hooks refused: {refused}", tuple(files))
    detail = (
        f"{len(files)} file(s) on {BRANCH}; tests failing: {len(before.failed)} on main,"
        f" {len(after.failed)} after"
    )
    return Outcome(repo.name, "success", detail, tuple(files))


def plan_one(repo: Path) -> Outcome:
    # Dry run: what the rewrite would change in the repository's main, read from git, no worktree.
    try:
        listed = _git(repo, "ls-tree", "-r", "--name-only", "main", "--", "src", "tests")
    except subprocess.CalledProcessError:
        return Outcome(repo.name, "skipped", "no local main to start from")
    files = []
    for name in listed.splitlines():
        if name.endswith(".py"):
            source = _git(repo, "show", f"main:{name}")
            if gaps.rewrite(source + "\n") != source + "\n":
                files.append(name)
    detail = f"would change {len(files)} file(s)" if files else "main has nothing to move"
    return Outcome(repo.name, "planned" if files else "skipped", detail, tuple(files))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m myfleet.local_lane")
    parser.add_argument("--core", type=Path, required=True, help="the core checkout to test on")
    parser.add_argument("--lab", type=Path, default=None, help="the lab root (default: atlas)")
    parser.add_argument("--repos", nargs="*", default=None, help="only these repositories")
    parser.add_argument("--execute", action="store_true", help="change, gate and commit locally")
    args = parser.parse_args(argv)
    lab = args.lab or paths.lab()
    repos = targets(args.core, lab)
    if args.repos is not None:
        repos = {name: path for name, path in repos.items() if name in args.repos}
    ledger = Ledger(ledger_path(fleet_root(__file__)))
    for path in repos.values():
        outcome = change_one(path, args.core) if args.execute else plan_one(path)
        print(f"{outcome.repo}: {outcome.outcome}: {outcome.detail}")
        for failure in outcome.new_failures[:5]:
            print(f"  newly failing: {failure}")
        if args.execute:
            ledger.record(
                TOOL,
                "lane",
                outcome.outcome,
                outcome.detail,
                repo=outcome.repo,
                task=TASK,
                branch=BRANCH if outcome.outcome == "success" else "",
                files=list(outcome.files),
                new_failures=list(outcome.new_failures),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
