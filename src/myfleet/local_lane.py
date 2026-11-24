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
# A task is what edits the tree. `public-names` is mechanical: tools importing a private core
# name that now has a public one (mythings.gaps.PUBLIC) are moved to it by mythings.gaps.rewrite,
# with no model call. `adopt-core` replaces a tool's copy of a core function with core's, when a
# local model judges the two interchangeable for the tool's callers (myfleet.lane_coder).
#
#     python -m myfleet.local_lane --core PATH                 # dry run: what would change where
#     python -m myfleet.local_lane --core PATH --execute       # change, gate, commit locally
#     python -m myfleet.local_lane --core PATH --task adopt-core --repos my-todo --execute
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
SUITE_SECONDS = 900
_REPORTED = re.compile(r"^(FAILED|ERROR) (\S+)")

Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Suite:
    failed: frozenset[str]  # node ids that failed or errored, plus "<collection>" when it broke
    code: int


@dataclass(frozen=True)
class Task:
    name: str
    subject: str  # the commit's subject line
    how: Callable[[], str]  # what made the change, for the commit's body, once it is made
    apply: Callable[[Path], list[str]]  # edits a checkout; the paths it changed

    @property
    def branch(self) -> str:
        return f"lane/{self.name}"


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


def _tidy_imports(tree: Path, files: Sequence[str], run: Run) -> None:
    # Sort the import blocks and drop the imports an edit left unused, in the files it touched
    # only, so the diff stays the edit.
    ruff = paths.lab_python().parent / "ruff"
    argv = [str(ruff), "check", "--select", "I,F401", "--fix", "-q", *files]
    run(argv, cwd=tree, check=False)


def _commit(tree: Path, files: Sequence[str], core: Path, task: Task, run: Run) -> str:
    # Commit through the repository's own hooks, never around them; why they refused, or "".
    _git(tree, "add", "--", *files, run=run)
    harness = "HARNESS.md re-vendored from my-things-core's harness.md.\n"
    message = (
        f"{task.subject}\n\n{task.how()}\n"
        + (harness if "HARNESS.md" in files else "")
        + "Made by myfleet.local_lane, gated on this repository's suite.\n"
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


_MOVED = ", ".join(f"{old} -> {new}" for old, new in gaps.PUBLIC.items())
PUBLIC_NAMES = Task(
    name="public-core-names",
    subject="Import core's public names instead of its private ones",
    how=lambda: f"{_MOVED} (mythings.gaps.rewrite).",
    apply=rewrite_tree,
)
BRANCH = PUBLIC_NAMES.branch


def change_one(
    repo: Path, core: Path, task: Task = PUBLIC_NAMES, *, run: Run = subprocess.run
) -> Outcome:
    if _has_branch(repo, task.branch):
        return Outcome(repo.name, "skipped", f"{task.branch} already waits for review")
    if not _has_branch(repo, "main"):
        return Outcome(repo.name, "skipped", "no local main to start from")
    tree = repo / ".claude" / "worktrees" / f"lane-{task.name}"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", task.branch, "main", run=run)
    keep = False
    try:
        outcome = _gated(repo, tree, core, task, run)
        keep = outcome.outcome == "success"
        return outcome
    finally:
        _git(repo, "worktree", "remove", "--force", str(tree), run=run)
        if not keep:
            _git(repo, "branch", "-D", task.branch, run=run)


def _gated(repo: Path, tree: Path, core: Path, task: Task, run: Run) -> Outcome:
    before = run_suite(tree, core, run=run)
    files = task.apply(tree)
    if not files:
        return Outcome(repo.name, "skipped", "nothing to change on main")
    _tidy_imports(tree, files, run)
    files += revendor(tree)
    after = run_suite(tree, core, run=run)
    new = tuple(sorted(after.failed - before.failed))
    if new:
        detail = f"{len(new)} test(s) that passed on main fail after the change"
        return Outcome(repo.name, "failed", detail, tuple(files), new)
    if refused := _commit(tree, files, core, task, run):
        return Outcome(repo.name, "failed", f"the commit hooks refused: {refused}", tuple(files))
    detail = (
        f"{len(files)} file(s) on {task.branch}; tests failing: {len(before.failed)} on main,"
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


def _report(ledger: Ledger, task: Task, outcome: Outcome, execute: bool) -> None:
    print(f"{outcome.repo}: {outcome.outcome}: {outcome.detail}")
    for failure in outcome.new_failures[:5]:
        print(f"  newly failing: {failure}")
    if execute:
        ledger.record(
            TOOL,
            "lane",
            outcome.outcome,
            outcome.detail,
            repo=outcome.repo,
            task=task.name,
            branch=task.branch if outcome.outcome == "success" else "",
            files=list(outcome.files),
            new_failures=list(outcome.new_failures),
        )


def _public_names(args: argparse.Namespace, lab: Path, ledger: Ledger) -> None:
    repos = targets(args.core, lab)
    for name, path in repos.items():
        if args.repos is None or name in args.repos:
            outcome = change_one(path, args.core) if args.execute else plan_one(path)
            _report(ledger, PUBLIC_NAMES, outcome, args.execute)


def _adopt_core(args: argparse.Namespace, lab: Path, ledger: Ledger) -> None:
    from myfleet import lane_coder

    found = gaps.find(args.core, gaps.lab_repos(lab, args.core))
    by_repo = lane_coder.candidates(found)
    system, question = lane_coder.tuned()
    coder = lane_coder.Coder(lane_coder.engine(args.traces), args.core, [], system, question)
    for name, cands in by_repo.items():
        if args.repos is not None and name not in args.repos:
            continue
        if not args.execute:
            listed = ", ".join(f"{c.local} -> {c.target}" for c in cands)
            print(f"{name}: planned: would ask about {listed}")
            continue
        task = lane_coder.task(coder, cands)
        _report(ledger, task, change_one(lab / name, args.core, task), True)
    for cand, yes, reason in coder.decided:
        print(f"  {cand.repo} {cand.local}: {'yes' if yes else 'no'}: {reason[:160]}")
        ledger.record(TOOL, "verdict", "yes" if yes else "no", reason, **vars(cand))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m myfleet.local_lane")
    parser.add_argument("--core", type=Path, required=True, help="the core checkout to test on")
    parser.add_argument("--lab", type=Path, default=None, help="the lab root (default: atlas)")
    parser.add_argument("--repos", nargs="*", default=None, help="only these repositories")
    parser.add_argument("--task", choices=("public-names", "adopt-core"), default="public-names")
    parser.add_argument("--traces", type=Path, default=None, help="where the coder's calls go")
    parser.add_argument("--execute", action="store_true", help="change, gate and commit locally")
    args = parser.parse_args(argv)
    lab = args.lab or paths.lab()
    root = fleet_root(__file__)
    ledger = Ledger(ledger_path(root))
    args.traces = args.traces or Path.home() / ".local/share/mythingslab/fleet/lane-traces.jsonl"
    if args.task == "public-names":
        _public_names(args, lab, ledger)
    else:
        _adopt_core(args, lab, ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
