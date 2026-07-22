# Changelog

All notable changes to `my-fleet` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semver](https://semver.org/), per the rules in `RELEASE.md`.

## [2.0.0] - 2026-07-22

### Changed

- `fleet_dispatch.py`'s worker role now shells out to `mycoder build --json`
  instead of spawning its own inline `claude -p` session. my-coder owns the
  Workspace worktree, branch naming/resume, prompt construction, and the
  single push + draft-PR side effect; `fleet_dispatch.py` picks which
  candidate to run and translates the JSON result into the same
  resume/recover outcome vocabulary (`success`/`needs_review`/`no_changes`/
  `failed`/`blocked`/`deferred`/`needs_human`), plus two outcomes new to this
  repo's ledger (`denied`, `skipped`) that mirror my-coder's own vocabulary.
- `_finalize_pr`'s readiness gate now trusts my-coder's own structured
  `tests_passed` signal instead of re-parsing the PR body for a checked
  readiness-checklist box.
- Branch naming for a dispatched issue changed from `fleet-dispatch/<repo>-
  <issue>` to `mycoder/<repo>-<issue>` (my-coder's own convention). Any
  `needs_review` checkpoint already in flight under the old name restarts
  fresh under the new one rather than being resumed — a one-time transition
  cost, not a recurring behavior.

### Removed

- **`--rtk` output-compression flag**, and the whole self-widening-allowlist
  mechanism (`.fleet-dispatch/allowed_tools.json`, the `self_edit`/`friction`
  ledger kinds' auto-widen behavior) it and the plain worker path shared.
  Neither has a working equivalent once my-coder owns its own fixed,
  hand-maintained tool allowlist rather than a runtime-mutable file this repo
  used to self-edit. This is a real capability loss (adaptive allowlist
  recovery, detailed token/denial-friction telemetry), not just a rename —
  accepted as the cost of retiring ~500 lines of duplicated worker logic;
  revisit if my-coder needs the same self-widening behavior later.
  Deliberately skips the usual deprecate-first-in-a-MINOR window: the
  mechanism `--rtk` depended on is gone, not just the flag, so there's no
  "still working" state to deprecate into.

## [1.0.0] - 2026-07-20

First stable release. Baseline of the fleet ops tooling as it already
existed: the dispatch loop, `fleet_cycle`/`cycle_driver`, the cross-repo test
gate, ASK-channel merge routing, and usage/account monitoring. No behavior
changes in this release. Adopts the v1 release contract (`RELEASE.md`) and
pins its `my-things-core` and `my-guard` dependencies to `@v1.0.0` instead of
floating on `@main`. `my-orchestrator` and `my-telegram-bot` stay on `@main`
— still v0.
