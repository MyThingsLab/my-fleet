# MyThingsLab

A fleet of small, composable `My[X]` tools that develop GitHub repositories as
autonomously as possible — **continuous autonomous controlled development**
(CAD). Deterministic code does everything except the handful of steps that
genuinely need judgment, and at each of those exactly one `Engine` call is
made. Every repo lives under the `MyThingsLab` GitHub org and imports the
shared SDK, [`my-things-core`](my-things-core/).

"Controlled" is the load-bearing word. Autonomy here is not "an agent does
what it likes"; it is a loop whose every mutating step is gated by a mechanism
that can say no without asking an agent's opinion — a `Policy` rule, a branch
protection, a required check, a HALT marker, or a human's tap on a phone.

## Layout

The fleet root is **not itself a git repo**. It is a directory of sibling
checkouts plus a shared `.venv`:

```
MyThingsLab/
├── my-things-core/      the SDK: five contracts + build tooling
├── my-fleet/            fleet ops: the drivers that chain everything
├── my-<tool>/           one checkout per My[X] tool (~50)
├── .venv/               every repo installed editable
├── .my-fleet/           untracked runtime state: ledger, transcripts, HALT
├── README.md ─┐
├── AGENTS.md  ├─ symlinks into my-fleet/workspace/ (where they are versioned)
└── TODO.md   ─┘
```

`CLAUDE.md` and `GEMINI.md` are symlinks to `AGENTS.md`, so all three agent
harnesses read one file.

> The root used to be the `fleet-dispatch` repo. Its scripts moved to
> `my-fleet` and the repo is now **archived** — read-only, issues transferred.
> Anything still naming a `fleet-dispatch` repo or a `.fleet-dispatch/`
> directory is stale.

## The five contracts

Every tool is built on the same seams from `my-things-core`, which is what
makes them composable rather than merely co-located:

| Contract | What it owns |
|---|---|
| `ledger` | Append-only record of every action any tool takes. |
| `policy` | `Action` → `ALLOW` / `ASK` / `DENY`, before any side effect. |
| `engine` | The one judgment call, behind a swappable backend. |
| `github` | Issues, PRs, checks — the only path to `gh`. |
| `isolation` | Worktree sandboxing, so a worker cannot touch its neighbours. |

## Lanes

Work is classified by lane, and lane outranks priority when the dispatcher
ranks the queue (`mythings.labels`): a core bug is dispatched ahead of a
product P0, because everything downstream is built on it.

**`lane:core`** — [my-things-core](my-things-core/). The SDK. Authority: none
of its own; every other tool imports it.

**`lane:kernel`** — the eight repos the fleet is built *from*. These are the
ones `myfleet.red_main` watches, because a red `main` here stops the loop:

| Repo | Role | Authority |
|---|---|---|
| [my-guard](my-guard/) | Rule engine: evaluates an `Action` to allow/ask/deny. | policy for every `git`/`gh` side effect |
| [my-orchestrator](my-orchestrator/) | Picks the single next unit of work for the next available worker. | decides; never builds |
| [my-architect](my-architect/) | Decomposes one objective into an ordered set of dispatchable issues. | plans; never builds |
| [my-coder](my-coder/) | Takes one picked issue and closes it as a PR, via a headless session. | builds |
| [my-tester](my-tester/) | Finds one uncovered unit, opens a PR adding a test for it. | writes code (tests only) |
| [my-reporter](my-reporter/) | Digests the `Ledger` + every `dev-ledger` into a report. | read-only |
| [my-telegram-bot](my-telegram-bot/) | Ledger notifications; turns a `Policy` `ASK` into a real human confirmation. | comms only, fail-closed |
| [my-fleet](https://github.com/MyThingsLab/my-fleet) | Chains every tool's own CLI into the cycle. | external driver; not a My[X] tool |

**`lane:product`** — everything else: the ~40 My[X] tools the kernel exists to
build (`my-researcher`, `my-bibliography`, `my-raytracer`, `my-server`,
`my-glossary`, …). Each tool's own `README.md`/`AGENTS.md` is authoritative
for its internals; this page only narrates how they chain.

A handful of cycle participants sit between the two —
[my-planner](my-planner/), [my-projector](my-projector/),
[my-changelogger](my-changelogger/), [my-pipeline](my-pipeline/),
[my-dashboard](my-dashboard/) — they run every tick but are not in
`red_main`'s watch list, so a red `main` on one of them degrades the loop
silently. That gap is tracked, not fixed.

## The loop, as it actually runs

The stage graph is **data**, not code: [my-pipeline](my-pipeline/) owns
`workflows.json`, and `mypipeline.plan.build_waves()` groups stages with no
dependency between them into a single wave. `myfleet.fleet_cycle` walks those
waves — it decides *whether we can afford to tick now*, not what the order is.
Today that graph is:

| Wave | Stage | What it does |
|---|---|---|
| 0 | `myplanner` | Refresh the recommended sequence (one urgency signal for ranking). |
| 1 | `fleet-dispatch` | `myorchestrator` picks; workers close issues as PRs. |
| 2 | `myresearcher`, `mytester`, `mychangelogger` | Concurrent — none reads another's output. |
| 3 | `mydocs` | Refresh the docs site from each tool's README. |
| 4 | `mydashboard` | Render the org front page. |
| 5 | `myprojector` | Reconcile the Project board + tracking issues. |
| 6 | `myreporter` | Post a fleet-wide digest. |
| 7 | `mypipeline-sync` | Reconcile the graph against what actually ran. |
| 8 | `mytelegrambot` | Push everything since the last notify. |

**No tool calls another tool's CLI directly.** Each stage is its own
`gh`-attributed, ledger-recorded run; `my-fleet` is the only thing that
chains them, and it makes no `Engine` calls of its own.

Two systemd timers on the Pi drive it, split so the ~50-repo bookkeeping
fan-out never rides a tick that is mostly a no-op:

- **build tick**, every 6h — `myplanner` → `fleet_dispatch`, the only tick
  that spends money.
- **bookkeeping tick**, daily — everything else, no account needed.
- **heartbeat**, hourly — a dead-man's switch that alerts when either tick's
  last recorded heartbeat is older than its own cadence allows. It exists
  because `OnFailure=` only fires when `ExecStart` *runs* and fails; a masked
  or never-installed timer produces nothing to catch, and that exact gap
  caused 183 consecutive silent failures before anyone noticed.

See [my-fleet's README](https://github.com/MyThingsLab/my-fleet) for flags,
deployment and the module-by-module breakdown.

## The gate: issue → PR → green → merge

A worker's PR opens **ready for review** when my-coder's own in-worktree suite
passed, and as a **draft** when nothing was verified. Opening ready is what
makes CI run at all: `ci.yml` skips required checks while a PR is a draft, so
a PR born as a draft can never show a green check — and an earlier version of
the gate read that skip as a pass.

A PR may be merged autonomously only when **all four** hold, each established
by mechanism rather than by the merging agent's judgment:

1. Its required checks report `pass` — not `skipped`, not `none`.
2. It is not a draft.
3. The repo's `main` is branch-protected, so "required" means something.
4. Its diff stays inside the scope of the issue it closes.

Short of all four, it stays open for a human. What this explicitly does *not*
accept as evidence: a green check branch protection does not require, a suite
that passed without `pythonpath = ["src"]` (that tested the editable install,
not the diff), or an agent's confidence in a diff it wrote itself.

**Four kinds of change never ride the gate**, however green:

- The merge gate's own code and any `myfleet` merge path — a gate must never
  certify itself.
- The constraints on agents: `AGENTS.md`, `HARNESS.md`, CI workflows, branch
  protection, `.claude/settings*.json`.
- Credential and auth handling.
- Public API and schema migrations.

These are the changes where merging one bad instance destroys the ability to
catch the next one.

Branch protection makes the shape GitHub-enforced rather than tool discipline:
every shipped repo's `main` requires a PR (no direct or force pushes) and a
green `test` check, with no required review count (workers cannot approve
their own PRs anyway) and admin bypass only for the one empty `Initial commit`
a new tool pushes before its first PR exists.

## Control surfaces

The things that can stop the fleet, in order of bluntness:

- **HALT** — `python3 -m myfleet.fleet_dispatch --abort` touches
  `.my-fleet/HALT`. Every `--execute` run checks for it before launching a
  single session and refuses outright. `--clear-halt` disarms it.
- **The ASK channel** — `--ask-human` exports `MYTHINGS_ASK_CMD`, which every
  tool CLI and headless worker inherits, so a bare `Guard()` anywhere in the
  fleet escalates its `ASK`s to Telegram and blocks for the tap. **Exit 0 is
  the human's ALLOW; anything else — deny, timeout, crash — is a `DENY`.**
  Fail-closed throughout. Unattended without it, every `ASK` collapses to
  `DENY` and the human is simply never asked.
- **`Policy`** — every mutating side effect (`git push`, `gh pr create`,
  tracking-issue edits) is wrapped as an `Action` and routed through
  `my-guard` before it happens.
- **`isolation`** — each worker runs in its own git worktree sandbox.
- **Identity** — spawning real sessions requires an explicit identity choice:
  the permission-scoped GitHub App, or `--allow-personal-token` to accept the
  ambient personal `gh` token, which is scoped to every repo the account can
  write to and is therefore never the silent default.
- **`red_main`** — files a `prio:P0` when a kernel `main` goes red and closes
  it on recovery, so workers are not sent at a repo whose base is broken.

## Where CAD actually stands

Honest status, so the loop is not mistaken for more finished than it is.

**Working:** the stage graph as data; two-cadence timers with a heartbeat;
worktree isolation; the ASK channel, verified end-to-end on the Pi; branch
protection across shipped repos; lane/priority ranking; the label schema as
one canonical table; `red_main` as a module.

**The next milestones**, roughly in dependency order:

1. **`myfleet.accept`** — the deterministic acceptance gate as a *module*
   rather than a rule agents are asked to follow. A rule in a Markdown file is
   a request; a gate that refuses is a control. This is the single highest-
   leverage remaining piece.
2. **The Foreman** — refactor `fleet_dispatch` into a session loop over
   durable `TaskRecord`s, so a worker's progress survives a crash and a stuck
   worker asks a human instead of silently giving up.
3. **Lane allocation and session budget** — bound what the loop can spend per
   tick, with a spend tripwire that pushes at a threshold and offers Halt /
   Raise cap.
4. **Human-in-the-loop from Telegram** — approve/reorder the recommended
   sequence, and merge from chat. Merge approval is currently the throughput
   bottleneck: the fleet produces PRs faster than one human taps through them.
5. **A knowledge-graph engine for CAD** — pre-index each repo and cache by git
   SHA, so a worker starts with structure instead of re-deriving it.

**Known gaps, tracked and not yet fixed:**

- `red_main` has a resolver registered in `fleet_cycle` but **no node in
  `workflows.json`**, and the driver only walks the graph — so the red-main
  guard does not currently run in the cycle at all, despite being documented
  as wave 0.
- `myplanner`, `myprojector`, `mychangelogger`, `mypipeline` and
  `mydashboard` run every tick but are outside `red_main`'s watch list.
- A channel that dies after arming is indistinguishable from a human pressing
  Deny.
- CI never runs on a stacked PR, so it can never satisfy branch protection.
- `my-docs` and `my-designer` are archived on GitHub but still referenced as
  cycle stages.

`TODO.md` is the curated backlog, generated by [my-todo](my-todo/) from open
issues. It regenerates, so edit the issues, not the file.

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

Run the cross-repo test gate before trusting a change that spans repos:

```bash
python3 -m myfleet.fleet_test            # every tool's fast suite
python3 -m myfleet.fleet_test --include-slow
```

## License

Each repo carries its own MIT license.
