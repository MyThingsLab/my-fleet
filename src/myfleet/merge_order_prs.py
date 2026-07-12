#!/usr/bin/env python3
"""Print the correct merge order for every open PR across the org.

Two things can force an order:

1. Cross-repo sibling dependencies. Every tool's CI installs its sibling
   packages straight from `git+https://github.com/<org>/<repo>@main` (see
   each repo's "Install siblings" CI step) -- there's no PyPI in between. If
   `my-guard`'s own main is broken (e.g. its rename PR hasn't merged yet),
   every downstream repo that installs `my-guard@main` will show red CI no
   matter what their own PR changes. So a repo's fix-PRs must merge before
   any PR in a repo that depends on it.
2. Within a repo, PRs stacked on another PR's branch (baseRefName pointing at
   another open PR's headRefName, not at the trunk) must merge in base-first
   order -- GitHub can't merge the top of a stack before its base.

This never merges anything -- it only computes and prints an order. The user
merges by hand (see merge_ready_prs.py for the actual merge, which is also
dry-run by default).
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys

from myfleet.merge_ready_prs import PR, list_open_prs, list_org_repos

ORG = "MyThingsLab"

# The org renamed mythings-core -> my-things-core mid-flight; old workflow
# files on stale branches still say the old name. Both names mean this repo.
REPO_ALIASES = {"mythings-core": "my-things-core"}

SIBLING_RE = re.compile(r"git\+https://[^\"']+/([A-Za-z0-9_.-]+)@")


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout


def sibling_dependencies(org: str, repo: str) -> set[str]:
    """Repos this one's CI installs from git+https at @main, per its ci.yml on main."""
    try:
        raw = _run(
            [
                "gh",
                "api",
                f"repos/{org}/{repo}/contents/.github/workflows/ci.yml",
                "--jq",
                ".content",
            ]
        )
    except RuntimeError:
        return set()
    try:
        content = base64.b64decode(raw.strip()).decode()
    except Exception:
        return set()
    deps = {REPO_ALIASES.get(name, name) for name in SIBLING_RE.findall(content)}
    deps.discard(repo)
    return deps


def repo_order(repos: list[str], graph: dict[str, set[str]]) -> list[str]:
    """Topological sort: a repo comes after every sibling it depends on."""
    remaining = set(repos)
    deps = {r: {d for d in graph.get(r, ()) if d in remaining} for r in repos}
    ordered: list[str] = []
    while remaining:
        ready = sorted(r for r in remaining if not deps[r] & remaining)
        if not ready:
            # cycle (shouldn't happen for this org) -- break it deterministically
            ready = [sorted(remaining)[0]]
        for r in ready:
            ordered.append(r)
            remaining.discard(r)
    return ordered


def pr_order_within_repo(prs: list[PR]) -> list[PR]:
    """Base-branch-first order: a PR stacked on another open PR's branch
    merges after it."""
    by_head = {pr.head: pr for pr in prs if pr.head}
    remaining = list(prs)
    ordered: list[PR] = []
    placed: set[int] = set()
    while remaining:
        ready = [
            pr
            for pr in remaining
            if pr.base not in by_head or by_head[pr.base].number in placed
        ]
        if not ready:
            ready = remaining[:1]  # cycle guard
        ready.sort(key=lambda pr: pr.number)
        for pr in ready:
            ordered.append(pr)
            placed.add(pr.number)
            remaining.remove(pr)
    return ordered


def main(argv: list[str] | None = None) -> int:
    repos = list_org_repos(ORG)

    graph: dict[str, set[str]] = {}
    prs_by_repo: dict[str, list[PR]] = {}
    for repo in repos:
        graph[repo] = sibling_dependencies(ORG, repo)
        try:
            prs = list_open_prs(repo)
        except RuntimeError as exc:
            print(f"skipping {repo}: {exc}", file=sys.stderr)
            prs = []
        if prs:
            prs_by_repo[repo] = prs

    ordered_repos = [r for r in repo_order(repos, graph) if r in prs_by_repo]

    print(f"merge order across {len(ordered_repos)} repos with open PRs:\n")
    step = 1
    for repo in ordered_repos:
        deps = graph.get(repo) & prs_by_repo.keys()
        dep_note = f" (after: {', '.join(sorted(deps))})" if deps else ""
        print(f"== {repo}{dep_note} ==")
        for pr in pr_order_within_repo(prs_by_repo[repo]):
            status = "ready" if pr.ready else f"NOT READY - {pr.reason_not_ready}"
            stack_note = f" [stacked on {pr.base}]" if pr.base not in ("main", "master") else ""
            print(f"  {step:>3}. #{pr.number} {pr.title!r}{stack_note} -- {status}")
            step += 1
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
