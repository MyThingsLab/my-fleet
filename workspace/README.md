# MyThingsLab

A fleet of small, composable `My[X]` tools that develop GitHub repositories as
autonomously as possible — deterministic code for everything except the
handful of steps that genuinely need judgment, where exactly one `Engine` call
is made. Every repo lives under the `MyThingsLab` GitHub org and imports the
shared SDK, [`my-things-core`](my-things-core/).

## The fleet

| Repo | Role | Authority |
|---|---|---|
| [my-things-core](my-things-core/) | SDK: `ledger`, `policy`, `engine`, `github`, `isolation` contracts. | none — every other tool imports it |
| [my-guard](my-guard/) | Rule engine: evaluates an `Action` to allow/ask/deny. | policy for every `git`/`gh` side effect |
| [my-planner](my-planner/) | Priority-ordered, multi-item plan across the whole backlog. | recommends a sequence; never dispatches |
| [my-orchestrator](my-orchestrator/) | Picks the single next unit of work for the next available worker. | decides; never builds, never chains into another tool's CLI |
| *(worker)* | A headless `claude -p` session, dispatched by [my-fleet](https://github.com/MyThingsLab/my-fleet)'s `fleet_dispatch`, closes the picked issue as a PR. | builds |
| [my-fleet](https://github.com/MyThingsLab/my-fleet) | Chains every tool's own CLI into the autonomous cycle (dispatch loop, test gate, ASK-channel merge routing, usage monitoring). | external driver; not a `My[X]` product tool |
| [my-tester](my-tester/) | Finds one uncovered unit, opens a PR adding a test for it. | writes code (tests only) |
| [my-changelogger](my-changelogger/) | Folds new `dev-ledger` entries into `CHANGELOG.md`. | writes docs (changelog only) |
| [my-projector](my-projector/) | Reconciles the org Project board + tracking-issue checklist to live repo state. | bookkeeping, no priority judgment |
| [my-reporter](my-reporter/) | Digests the `Ledger` + every repo's `dev-ledger` into a report; can comment it on the tracking issue. | read-only |
| [my-telegram-bot](my-telegram-bot/) | Pushes ledger notifications to Telegram; turns a `Policy` `ASK` into a real human confirmation. | comms only, fail-closed |

The table lists the tools that drive the autonomous cycle; the fleet has more
(`my-docs`, `my-researcher`, `my-todo`, `my-server`, `my-typster`, …) — every
sibling `my-*/` directory is one tool, and the
[fleet docs site](https://mythingslab.github.io) carries a page per tool,
kept in sync by `my-docs`. Each tool's own `README.md`/`CLAUDE.md` is
authoritative for its internals. This page narrates how they chain into one
loop.

## The autonomous cycle

No tool calls another tool's CLI directly — each run is its own
`gh`-attributed, ledger-recorded action, per every tool's own invariants. The
external driver that chains them — the pick-and-build dispatch loop, the full
build/study cycle, the cross-repo test gate, ASK-channel merge routing, and
usage/account monitoring — lives in its own repo,
[**my-fleet**](https://github.com/MyThingsLab/my-fleet), not at this root.
See its README for the cycle's step order, the kill switch, and the
`--ask-human` channel.

## Issue → PR → draft → ready → green → merge

Every worker's PR follows the same shape (`my-fleet`'s `fleet_dispatch`
`_finalize_pr`): open **draft**, promote to **ready for review** only once
the PR body's readiness checklist holds *and* CI is green, and never merge —
a human always does that last step.

This is now a GitHub-enforced invariant, not just tool discipline: every
shipped repo's `main` has branch protection requiring a PR (no direct or
force pushes) and a green `test` status check before the merge button
unlocks, with no required review count (workers can't approve their own
PRs anyway) and admin/owner bypass enabled for genesis bootstrapping (the
one empty `Initial commit` every new tool pushes straight to `main` before
its first PR exists). So even a manual push or a misbehaving tool can't
land on `main` without going through the same gate the fleet already
enforces on itself.

## Provenance

Every tool records its own build history under its `dev-ledger/`
(`python -m mythings._devledger`), and its *runtime* activity (dispatches,
tests written, PRs opened, reports posted, notifications sent) to the shared
`Ledger` each contract reads and writes. `myreporter digest --handoff` renders
either into a resume-context brief for picking work back up in a fresh
session, without re-deriving state from raw git/ledger history.

## Install (development)

Each repo has its own `pyproject.toml`; a shared `.venv` at this root has
every package installed editable:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e "my-things-core[dev]"
for repo in my-*/; do
  [ "$repo" = "my-template/" ] || [ ! -f "$repo/pyproject.toml" ] || \
    pip install -e "${repo%/}[dev]"
done
```

## License

Each repo carries its own MIT license.
