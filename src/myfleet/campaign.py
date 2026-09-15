#!/usr/bin/env python3
"""Expand one campaign issue into N mechanically-identical child candidates,
and tally where the set of them stands.

## The gap this closes (#109)

Every other primitive in the fleet assumes one issue -> one PR -> one repo:
`Candidate.id` is `repo#number`, `myfleet.fleet_dispatch._mycoder_branch` is
`mycoder/<repo>-<number>`, `myfleet.accept.assess` judges one open PR. A
**campaign** -- one goal, replicated identically across a repo set -- has no
shape in any of that, and #84 ("fan out the always-reporting `test` job to the
20 active repos carrying `paths-ignore`") is exactly that shape: one diagnosed
change, twenty repos, nothing tying the twenty together today.

## The explicit decision (#109 asked for one, not a default)

This is deliberately **not** built as part of #29's Foreman. Foreman is "one
objective + a task DAG + a budget" -- the general case, for work with real
edges between tasks. A campaign is the *degenerate* case: N independent tasks,
no edges. It needs none of the DAG machinery, and the 2026-09-13 fleet review
(`workspace/reviews/2026-09.md`) explicitly recommended *not* starting
Foreman's second phase until dispatch has run unattended for seven consecutive
days -- so building the DAG-general primitive now would be building on a loop
that has not yet proven itself. The campaign primitive needs no such proof: it
composes entirely out of the single-PR primitives that already exist
(`fleet_dispatch`, `accept`), one call per child, so it inherits their
soundness rather than adding a second execution path to trust.

## What this module owns, and what it deliberately does not

`expand()` turns a `Campaign` (a parent issue, a repo set, and one template
diff) into per-repo `ChildSpec`s, deterministically -- same input, same
output, every time, so re-running an expansion after a partial failure
produces the same children rather than a second, drifting set. The repo set is
an input, not something this module queries for: "selecting the repo set
should be a query, not a hand-maintained list" (#109, echoing #104's
`_KERNEL_REPOS` drift) is a real requirement, but the query itself is
repo-set-specific (#84's is "paths-ignore in ci.yml AND test required") and
belongs where that logic is actually run, not baked into the primitive that
every future campaign will reuse.

Likewise, this module does not file the child issues, cut branches, apply the
template diff, or open PRs -- those are exactly the single-PR primitives
`fleet_dispatch` and `my-coder` already own, and the whole point of a shared
template diff is that it is *applied*, not re-derived, by whichever of those
runs each child. `ChildSpec.body` carries the diff forward as the thing a
worker applies verbatim; a worker only re-derives a fix for a repo where it
does not apply cleanly.

## Set-level accounting

`accept.Verdict` (`ACCEPTED`/`REJECTED`/`NEEDS_HUMAN`) judges one open PR's
mergeability; `fleet_dispatch`'s attempt outcomes (`"blocked"`,
`"needs_human"`, ...) cover one child's dispatch lifecycle. Neither has a
vocabulary for the *set*: a campaign that lands 17 of 20 children is a success
with a remainder, not a failure, and nothing upstream can say that. `tally()`
answers it: `ChildState` is where one child stands (including states neither
`accept` nor a dispatch outcome tracks, like "no PR yet"), and
`CampaignVerdict` is the parent's own vocabulary -- `COMPLETE`, `PARTIAL`
(the #109 example: some landed, the rest permanently stuck), `BLOCKED` (none
landed, all stuck), or `IN_PROGRESS` (still moving).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Campaign:
    parent_issue: str  # "<repo>#<number>" naming the change and the repo set
    repos: tuple[str, ...]  # the query's result -- never a hand-maintained literal
    template_diff: str  # the one diff every child applies verbatim


@dataclass(frozen=True)
class ChildSpec:
    repo: str
    title: str
    body: str


def expand(campaign: Campaign) -> list[ChildSpec]:
    if not campaign.repos:
        raise ValueError(f"campaign {campaign.parent_issue} has no repos to expand into")
    if len(set(campaign.repos)) != len(campaign.repos):
        raise ValueError(f"campaign {campaign.parent_issue} lists a repo more than once")

    title = f"campaign: apply {campaign.parent_issue}'s change here"
    return [
        ChildSpec(repo=repo, title=title, body=_child_body(campaign, repo))
        for repo in sorted(campaign.repos)
    ]


def _child_body(campaign: Campaign, repo: str) -> str:
    return (
        f"One of {len(campaign.repos)} mechanically-identical children of "
        f"{campaign.parent_issue}. Apply the following patch verbatim -- re-derive a "
        "fix only if it does not apply cleanly here:\n\n"
        f"```diff\n{campaign.template_diff}\n```\n\n"
        f"campaign-parent: {campaign.parent_issue}"
    )


class ChildState(StrEnum):
    PENDING = "pending"  # child issue filed, no PR yet
    IN_PROGRESS = "in_progress"  # PR open, not yet accepted
    LANDED = "landed"  # PR merged
    BLOCKED = "blocked"  # blocked on another repo, per fleet_dispatch's outcome
    NEEDS_HUMAN = "needs_human"  # exhausted attempts, per fleet_dispatch's outcome


class CampaignVerdict(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CampaignTally:
    total: int
    landed: int
    counts: dict[ChildState, int]
    verdict: CampaignVerdict


def tally(states: list[ChildState]) -> CampaignTally:
    if not states:
        raise ValueError("cannot tally an empty campaign")

    counts = {state: states.count(state) for state in ChildState}
    total = len(states)
    landed = counts[ChildState.LANDED]
    stuck = counts[ChildState.BLOCKED] + counts[ChildState.NEEDS_HUMAN]

    if landed == total:
        verdict = CampaignVerdict.COMPLETE
    elif landed == 0 and stuck == total:
        verdict = CampaignVerdict.BLOCKED
    elif landed > 0 and landed + stuck == total:
        # every child that isn't landed is permanently stuck -- nothing left
        # to do unattended, but what landed, stays landed
        verdict = CampaignVerdict.PARTIAL
    else:
        verdict = CampaignVerdict.IN_PROGRESS

    return CampaignTally(total=total, landed=landed, counts=counts, verdict=verdict)
