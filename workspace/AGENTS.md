# MyThingsLab — workspace instructions

This directory holds the MyThingsLab tool fleet: `my-things-core/` (the SDK —
the five contracts plus build tooling) and one sibling repo per `My[X]` tool,
each scaffolded from `my-template/`. The fleet root is **not itself a git
repo**: this file and its neighbours are symlinks into
[`my-fleet/workspace/`](https://github.com/MyThingsLab/my-fleet), which is
where the workspace-level docs are versioned. `README.md` here narrates how the
fleet chains together; `TODO.md` is the curated org backlog.

Fleet ops live in `my-fleet/`: `myfleet.fleet_dispatch` (pick-and-build
workers) and `myfleet.fleet_cycle` (the full autonomous loop), plus the
cross-repo test gate, ASK-channel merge routing, and usage/account monitoring —
see `my-fleet/README.md` and `my-fleet/CLAUDE.md`. The dispatch loop's runtime
state (ledger, transcripts, the HALT kill-switch marker) is `.my-fleet/` at
this root: untracked local state, read/written by `my-fleet`'s scripts.

The root used to be the `fleet-dispatch` repo. Its scripts moved out in #58 and
the repo is now archived — read-only, history intact, issues transferred to
`my-fleet`. Anything still referring to a `fleet-dispatch` repo or a
`.fleet-dispatch/` directory is stale.

## Instruction hierarchy

When developing any tool, the most specific instruction wins:

1. That repo's `AGENTS.md` (and `CLAUDE.md`/`GEMINI.md` symlinks; purpose, its
   one Engine call, invariants) and its vendored `HARNESS.md` (fixed build
   rules, drift-checked in CI).
2. This file — cross-cutting workspace facts.
3. `my-things-core/docs/CONVENTIONS.md` — fleet-wide conventions, the
   Rule→Gate enforcement table, and "Starting a new tool".

Canonical homes: the harness lives at `my-things-core/src/mythings/harness.md`
(every repo's `HARNESS.md` is a vendored copy); architecture and provenance are
`my-things-core/docs/ARCHITECTURE.md` and `docs/PROVENANCE.md`.

## Cross-cutting facts

- All repos live under the **`MyThingsLab`** GitHub org, **public**, and are
  kept **entirely isolated from other ventures** (org account, not personal).
- Shared `.venv` at this root has every repo's package installed editable.
- Provenance goes in each repo's `dev-ledger/` (`python -m mythings._devledger`);
  runtime activity goes to the shared `Ledger`.
- To start a new tool, follow "Starting a new tool" in
  `my-things-core/docs/CONVENTIONS.md`.
- `.claude/HANDOFF.md` is the fleet resume brief, regenerated automatically on
  session start — read it before re-deriving state from git/ledger history.

## Session rules (apply to every session and dispatched worker)

- **Branch before commit.** Never commit on a local `main`; check out a branch
  immediately after syncing `main`. Every change lands via PR — `main` is
  branch-protected (PR + green `test` check required) in every shipped repo.
- **Merging is gated, not forbidden.** Autonomous merge is the point of CAD; an
  *unverified* merge is what the old "never merge" rule was actually guarding
  against. A session or worker may merge a PR when all of the following hold,
  each established by mechanism rather than by the merging agent's own judgement:
  its required checks report `pass` — not `skipped`, not `none`, see
  `myfleet.fleet_dispatch._checks_state`; it is not a draft; the repo's `main` is
  branch-protected, so "required" means something; and its diff stays inside the
  scope of the issue it closes. Short of all four, it stays open for a human.
  Note what this does *not* accept as evidence: a green check the branch
  protection does not require, a suite that passed without `pythonpath = ["src"]`
  (it tested the editable install, not the diff), or the merging agent's
  confidence in a diff it wrote.
- **Four kinds of change need a human however green they are.** The merge gate's
  own code and any `myfleet` merge path — a gate must never certify itself.
  The constraints on agents: this file, `HARNESS.md`, CI workflows, branch
  protection, `.claude/settings*.json`. Credential and auth handling. Public API
  and schema migrations. These are the changes where merging a bad one destroys
  the ability to catch the next one, so they do not get to ride the gate.
- **Never persist secrets.** No tokens on disk, in git, or in the ledger; use
  `gh secret set`. Treat any secret pasted into chat as exposed.
- **Re-check live state before acting.** An external multi-worker dispatcher
  runs against these same repos; issues, PRs, and branches move between
  sessions. `git fetch` and check `gh` before trusting a local checkout.
- **Read a file before editing it, every session.** The Edit/Write tools
  require a prior Read in the *current* session — a file existing on disk
  from an earlier session or worker doesn't satisfy this. Fresh worker
  sessions and post-compaction context both need a fresh Read before any
  Edit/Write, even for files you already know the contents of.
