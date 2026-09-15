#!/usr/bin/env python3
"""Flag a MyThingsLab repo that vanished from GitHub without a written decision.

my-fleet#87: `my-office` and `my-city` were reported by August's review as
"never completed genesis" -- reasonable, since their local checkouts still
exist. The truth was worse: **both had been deleted from GitHub**, not
archived, and nothing recorded that this happened, let alone why. The only
reason it surfaced at all is that the 2026-09 review happened to run
`gh repo view` against a local directory listing.

Archiving and deleting are not interchangeable: archiving keeps history,
issues, and PRs read-only; deleting throws all of it away. This module makes
that distinction mechanical instead of something a review has to notice by
hand: a repo missing from GitHub is either explained by an entry in RETIRED
(a decision, made and dated) or it is `undocumented` -- indistinguishable from
an accidental `gh repo delete`.

    python -m myfleet.repo_registry my-office my-city   # exits 1 if undocumented
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

ORG = "MyThingsLab"

# A repo leaves this dict only by an explicit, written decision -- restore it
# (drop the entry, recreate the GitHub repo) or retire it (record why here,
# durably, in version control -- not a local file on one laptop). Silence is
# a bug, not a choice.
RETIRED: dict[str, str] = {
    "my-office": (
        "deleted from GitHub before genesis (CI, branch protection, AGENTS.md) "
        "completed. Retired rather than restored on 2026-09-14: recreating it "
        "would add back a tool while the fleet's own 2026-08 review explicitly "
        "held the tool count, and the design work is preserved in the local "
        "checkout's `feat/office-renderer` branch (4f24749) for anyone who later "
        "reopens the case for restoring it. See my-fleet#87."
    ),
    "my-city": (
        "deleted from GitHub before genesis completed; never had a remote at "
        "all beyond the initial local commit. Retired on 2026-09-14 for the "
        "same reason as `my-office` -- see my-fleet#87. History: local "
        "`genesis-my-city` branch (8e163c4), never pushed."
    ),
}


@dataclass(frozen=True)
class RepoState:
    repo: str
    exists: bool
    archived: bool

    @property
    def deleted(self) -> bool:
        return not self.exists

    @property
    def undocumented(self) -> bool:
        """Gone from GitHub, not archived, and RETIRED has no entry for it."""
        return self.deleted and self.repo not in RETIRED


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True)


def repo_state(repo: str, *, runner: Runner = _run) -> RepoState:
    proc = runner(["gh", "repo", "view", f"{ORG}/{repo}", "--json", "isArchived"])
    if proc.returncode != 0:
        return RepoState(repo=repo, exists=False, archived=False)
    data = json.loads(proc.stdout)
    return RepoState(repo=repo, exists=True, archived=bool(data.get("isArchived")))


def undocumented_deletions(repos: list[str], *, runner: Runner = _run) -> list[RepoState]:
    return [state for repo in repos if (state := repo_state(repo, runner=runner)).undocumented]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="+", help="repo names to check, e.g. my-office my-city")
    args = parser.parse_args(argv)

    bad = undocumented_deletions(args.repos)
    if not bad:
        print(f"OK: no undocumented deletions among {len(args.repos)} repo(s).")
        return 0
    for state in bad:
        print(
            f"UNDOCUMENTED DELETION: {state.repo} is gone from GitHub, not archived, "
            "and has no entry in myfleet.repo_registry.RETIRED. Restore it or record "
            "the retirement decision."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
