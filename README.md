# my-fleet

Fleet orchestration/ops tooling for the [MyThingsLab](https://github.com/MyThingsLab)
tool fleet. This repo is **not** a My[X] product tool — it's the fleet's own
ops repo, the external driver that chains every other tool's CLI into an
autonomous build loop, plus a matching study loop, a cross-repo test gate,
ASK-channel merge routing, and usage/account monitoring. It makes no `Engine`
calls of its own; everything here is deterministic.

## The autonomous cycle

No tool calls another tool's CLI directly — each run is its own
`gh`-attributed, ledger-recorded action, per every tool's own invariants. Two
modules in this package are the external drivers that chain them:

- **[`myfleet.fleet_dispatch`](src/myfleet/fleet_dispatch.py)** — the
  pick-and-build step: imports `Orchestrator` as a library to rank
  candidates, then fans them out across one or more `claude -p` accounts,
  each in its own git-worktree sandbox, with resume/recover across attempts
  (durable branches, cross-repo blocker protocol, `needs_human` after
  repeated failures).
- **[`myfleet.fleet_cycle`](src/myfleet/fleet_cycle.py)** — the full loop, in
  order:

  1. `myplanner plan` — refresh the recommended sequence (feeds
     `myorchestrator`'s ranking as one more urgency signal).
  2. `fleet_dispatch` — `myorchestrator` picks the next unit(s); workers
     close them as PRs.
  3. `mytester run` (per repo) — add coverage for one uncovered unit.
  4. `mychangelogger update` (per repo) — fold new ledger entries into
     `CHANGELOG.md`.
  5. `mydocs sync` — refresh the fleet docs site from each tool's
     `README.md`/`CLAUDE.md` (deterministic hash check; opens, never merges,
     one PR when pages are stale).
  6. `myprojector sync` — reconcile the org Project board + tracking-issue
     checklist.
  7. `myreporter post` — post a fleet-wide digest on the tracking issue.
  8. `mytelegrambot notify` — push everything since the last notify.

  The per-repo steps auto-discover every checkout with a `pyproject.toml`
  (except `my-template`), so a newly scaffolded tool joins the cycle without
  editing the script.

  `fleet_cycle --loop` keeps re-running that sequence instead of exiting
  after one pass — meant for an always-on host, not an interactive session.
  Each iteration re-derives the usable account pool
  (`account_usage.select_accounts`, polled on a cadence rather than every
  iteration) and backs off between iterations that dispatch nothing. It's
  meant to be launched as a long-lived process (e.g. a systemd user service)
  with `Restart=on-failure` handling crash recovery, not driven by this
  module's own `--max-duration-min`/`--max-cycle-budget-usd`, which exist for
  bounded manual runs instead.

Every mutating side effect along the way — `git push`, `gh pr create`,
tracking-issue edits — is wrapped as an `Action` routed through `Policy`
(`my-guard`'s `Guard`, or a tool's own default). An `ASK` collapses to `DENY`
unattended (in CI, or with no `my-telegram-bot` wired in); with
`TelegramPolicy` wrapping it, an `ASK` becomes a real Allow/Deny prompt sent
to Telegram and blocks for a reply instead.

## Issue → PR → draft → ready → green → merge

Every worker's PR follows the same shape (`fleet_dispatch`'s
`_finalize_pr`): open **draft**, promote to **ready for review** only once
the PR body's readiness checklist holds *and* CI is green, and never merge —
a human always does that last step.

```bash
# One full cycle, dry-run (default): reports what each step would do, no
# mutating subcommands run and fleet_dispatch never spawns billed sessions.
python3 -m myfleet.fleet_cycle --accounts ~/.claude-lorenzoliuzzo,~/.claude-mythingslab

# For real: mutating subcommands run, and fleet_dispatch spawns real sessions.
python3 -m myfleet.fleet_cycle --accounts ~/.claude-lorenzoliuzzo,~/.claude-mythingslab \
  --execute --dispatch-execute
```

`--execute` and `--dispatch-execute` are separate flags on purpose:
`fleet_dispatch`'s sessions are billed API usage, while the rest of the cycle
(tester/changelogger/projector/reporter/telegram) is not — you can run the
bookkeeping half of the loop freely and opt into spawning workers separately.

Spawning real sessions also requires an identity choice: authenticate as the
permission-scoped GitHub App (`--app-id`/`--app-installation-id`/
`--app-private-key` on `fleet_dispatch`), or pass `--allow-personal-token`
(both drivers) to explicitly accept running workers on the ambient personal
`gh` token — which is scoped to every repo the account can write to, not just
this org, so it is never the silent default.

## Asking a human (`--ask-human`)

MyGuard answers `ASK` when an action needs a human's blessing. Unattended
there was nobody to ask, so every caller's `PolicyResult.under(unattended=True)`
collapsed it to `DENY` — correct, but the human was *never actually asked*,
and MyTelegramBot's whole reason for existing (turning an `ASK` into a real
Allow/Deny prompt) sat unplugged with zero callers.

`--ask-human` on either driver arms the channel:

```bash
python3 -m myfleet.fleet_cycle --accounts ... --execute --ask-human
python3 -m myfleet.fleet_dispatch --accounts ... --execute --ask-human
```

It exports `MYTHINGS_ASK_CMD`, which every tool CLI and headless worker
inherits, so a bare `Guard()` anywhere in the fleet escalates its `ASK`s to
Telegram and honours the tap. **Exit 0 is the human's ALLOW; anything else —
deny, timeout, crash — is a `DENY`.** Fail-closed throughout; unset the
variable and behavior is exactly what it was.

## Kill switch

To stop `fleet_dispatch --execute` from launching anything — right now,
across every account, until you say otherwise:

```bash
python3 -m myfleet.fleet_dispatch --abort        # arm it: no --accounts needed
python3 -m myfleet.fleet_dispatch --clear-halt   # disarm it once it's safe to resume
```

`--abort` touches a marker file (`.fleet-dispatch/HALT` under the fleet
root); every `--execute` run checks for it before launching a single session
and refuses outright if it's there (a dry run still reports normally, just
with a note). Since `fleet_cycle` shells out to `fleet_dispatch` for its
dispatch step, arming the marker halts that path too.

## Other modules

- **[`myfleet.fleet_test`](src/myfleet/fleet_test.py)** — the cross-repo test
  gate: runs each tool's fast suite (or the whole fleet) and reports pass/fail.
- **[`myfleet.merge_ready_prs`](src/myfleet/merge_ready_prs.py)** /
  **[`myfleet.merge_order_prs`](src/myfleet/merge_order_prs.py)** — find PRs
  that are green and ready, route the actual merge through MyGuard's
  `pr-merge` ASK rule, and order merges across a PR dependency DAG.
- **[`myfleet.account_usage`](src/myfleet/account_usage.py)** /
  **[`myfleet.fleet_usage`](src/myfleet/fleet_usage.py)** — poll Claude Code
  account session usage and worker transcripts to decide which accounts are
  safe to dispatch on.
- **[`myfleet.notify_usage`](src/myfleet/notify_usage.py)** /
  **[`myfleet.notify_systemd_status`](src/myfleet/notify_systemd_status.py)**
  — push Telegram alerts on usage thresholds and systemd unit health.
- **[`myfleet.study_cycle`](src/myfleet/study_cycle.py)** /
  **[`myfleet.cycle_driver`](src/myfleet/cycle_driver.py)** — the study-loop
  counterpart of `fleet_cycle`, and the shared stage-running driver both
  cycles are built on.

## Install (development)

This repo lives as a sibling checkout under the MyThingsLab fleet root,
alongside `my-things-core` and every `My[X]` tool. Its scripts locate the
fleet root (and sibling repos' ledgers) via `WORKSPACE_ROOT`, computed from
this package's own file location — so it must stay checked out directly under
the fleet root as `my-fleet/`.

```bash
pip install -e ".[dev]"
```

## Deploy: the bookkeeping timer

Every fleet instrument (`TODO.md`, the docs site, the dashboard, the resume
handoff brief) is refresh-on-run — nothing keeps them current while the
dispatch loop is idle. `systemd/fleet-bookkeeping.{service,timer}` run
`fleet_cycle.py --execute --skip-dispatch --brief-count 0 --engine noop`
daily: every step except dispatch and research briefs (planner, tester,
projector, reporter, docs sync, dashboard render) executes for free, with no
billed Engine calls and no worker sessions spawned. Switch `--engine` to
`claude-cli` once a run has confirmed the noop path end to end.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/fleet-bookkeeping.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-bookkeeping.timer
```

The unit files assume the fleet root is checked out at
`~/Desktop/MyThingsLab`; edit the paths in `fleet-bookkeeping.service` first
if yours differs. `FLEET_ACCOUNTS` in the service file is unused by this cycle
(dispatch is skipped) but still required by `fleet_cycle.py`'s CLI — leave the
default unless `--accounts` parsing itself needs a real path.

## License

MIT.
