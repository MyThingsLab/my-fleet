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

## Issue → PR → green → merge

Every worker's PR follows the same shape. my-coder opens it **ready for
review** when its own in-worktree suite passed, and as a **draft** when
nothing was verified. `fleet_dispatch._finalize_pr` then only *observes*:
"success" means my-coder verified it and CI independently agrees, so a human
can merge it. It promotes nothing and merges nothing.

The gate is the merge, and a human always performs it. It used to be the
promotion, which could not work: `ci.yml` skips required checks while a PR is
a draft, so a PR born as a draft can never show a green check — and the old
gate read that skip as a pass (#32). Opening ready is what makes CI run at
all.

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

`--abort` touches a marker file (`.my-fleet/HALT` under the fleet
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
- **[`myfleet.heartbeat`](src/myfleet/heartbeat.py)** — dead-man's-switch for
  the build/bookkeeping timers (#28): alerts when a tick's last recorded
  heartbeat is older than its own cadence allows.
- **[`myfleet.red_main`](src/myfleet/red_main.py)** — watches every kernel
  repo's `main`. A red one becomes a `prio:P0` issue (priority via
  `mythings.labels.escalate`, not a local opinion) and is closed again on
  recovery. Runs in wave 0 of the cycle, ahead of dispatch, so workers are not
  sent at a repo whose base is already broken.

## Install (development)

This repo lives as a sibling checkout under the MyThingsLab fleet root,
alongside `my-things-core` and every `My[X]` tool. Its scripts locate the
fleet root (and sibling repos' ledgers) via `WORKSPACE_ROOT`, computed from
this package's own file location — so it must stay checked out directly under
the fleet root as `my-fleet/`.

```bash
pip install -e ".[dev]"
```

## Deploy: the supervised loop (the Pi)

Two timers drive `fleet_cycle.py` on two different cadences (#28), so that
the ~50-repo `mytester`/`mychangelogger` fan-out (`BOOKKEEPING_STAGES`) never
rides along on a tick that's mostly a no-op:

- `systemd/fleet-cycle.{service,timer}` — the frequent **build tick**
  (`myplanner` → `fleet_dispatch`), every 6 hours, via `run_fleet_cycle.sh
  --skip-bookkeeping`.
- `systemd/fleet-bookkeeping.{service,timer}` — the daily **bookkeeping
  tick** (`mytester`, `mychangelogger`, `mydocs`, `mydashboard`,
  `myprojector`, `myreporter`, `mypipeline sync`, `mytelegrambot`), via
  `fleet_cycle.py --execute --skip-dispatch --brief-count 0 --engine noop`
  directly — presentation/provenance, so it needs no account and spawns no
  billed Engine calls or worker sessions. Switch `--engine` to `claude-cli`
  once a run has confirmed the noop path end to end.
- `systemd/fleet-heartbeat.{service,timer}` — hourly dead-man's-switch:
  `myfleet.heartbeat` alerts (and exits nonzero) if either tick's last
  recorded heartbeat is older than its own cadence allows, closing the gap a
  unit that silently stops firing leaves behind (`OnFailure=` only fires when
  the `ExecStart` itself runs and fails — a masked or never-installed timer
  produces nothing to catch). See `myfleet.heartbeat`'s module docstring for
  the incident that motivated it.

`systemd/fleet-usage.{service,timer}`, `mytelegrambot.service(.d/testers.conf)`
and `telegram-alert@.service` round out the deployment: usage polling and the
`OnFailure=telegram-alert@%n.service` alert every one of the above fires on a
failed run.

All of these are checked-in reference copies of what actually runs,
unprivileged, under `lollinuxpi-server`'s system-wide systemd
(`/etc/systemd/system/`, not `~/.config/systemd/user/`). They exist so a path
move (like #58's `fleet-dispatch` → `my-fleet` rename) is a diffable,
reviewable change instead of invisible drift between the live
`/etc/systemd/system/*.service` files and this repo — that exact drift caused
183 consecutive silent `fleet-cycle` failures (2026-08-03 → 2026-08-26) before
anyone noticed. **Whenever a unit is edited live on the Pi, copy the change
back here in the same session** — these files are documentation of a manual
`sudo cp` + `daemon-reload`, not something `fleet-cycle.py` deploys itself.

```bash
sudo cp systemd/fleet-cycle.{service,timer} systemd/fleet-bookkeeping.{service,timer} \
        systemd/fleet-heartbeat.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-cycle.timer fleet-bookkeeping.timer fleet-heartbeat.timer
```

To confirm a timer actually fires rather than trusting `enable --now`, run its
service once by hand and check the journal, then watch for its heartbeat:

```bash
sudo systemctl start fleet-cycle.service && journalctl -u fleet-cycle.service -n 50
sudo systemctl start fleet-bookkeeping.service && journalctl -u fleet-bookkeeping.service -n 50
python3 -m myfleet.heartbeat  # "heartbeats fresh: bookkeeping, build" once both have run
```

## License

MIT.
