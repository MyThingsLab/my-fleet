# my-fleet — agent instructions

You are developing **my-fleet**, the MyThingsLab fleet's own ops repo — **not**
a My[X] product tool.

**Inherited rules:** obey [`./HARNESS.md`](./HARNESS.md) in full — the vendored
MyThingsLab build-harness rules. Do not restate or override them. Anything not
covered here defers to `HARNESS.md`, then `my-things-core/docs/CONVENTIONS.md`.

## This tool

- **Purpose:** fleet orchestration/ops tooling — the pick-and-build dispatch
  loop (`fleet_dispatch.py`), the full autonomous build cycle and its study
  counterpart (`fleet_cycle.py`, `study_cycle.py`, `cycle_driver.py`), the
  cross-repo test gate (`fleet_test.py`), ASK-channel merge routing
  (`merge_ready_prs.py`, `merge_order_prs.py`, `fleet_ask.py`), and
  usage/account monitoring (`account_usage.py`, `fleet_usage.py`,
  `notify_usage.py`, `notify_systemd_status.py`).
- **The single Engine call:** none — deterministic, meta-tooling that
  orchestrates other My[X] tools rather than making judgment calls itself.
- **Invariants / rules:**
  - `WORKSPACE_ROOT` in every script must resolve to the MyThingsLab fleet
    root (the parent of this checkout), not this repo's own root — every
    sibling-repo path, ledger path, and subprocess invocation depends on it.
  - No tool calls another tool's CLI directly; this repo is the external
    driver that chains them, each as its own `gh`-attributed, ledger-recorded
    run.
  - Every mutating side effect routes through `Policy` (`my-guard`'s `Guard`);
    an `ASK` collapses to `DENY` unattended unless the ASK channel
    (`fleet_ask.py` + `my-telegram-bot`) is wired in.
  - A human always merges; nothing here calls `gh pr merge` on its own behalf.
- **Backlog label:** none — this repo is exempt from the standard My[X]
  backlog-label loop; issues here are fleet-ops housekeeping, not
  Engine-processed backlog items.

This repo is exempt from the standard 5-seam contract (`ledger`, `policy`,
`engine`, `github`, `isolation`) in one respect: it does not itself consume
`my-things-core`'s `Engine` — it has no judgment step to delegate. It still
uses `ledger`, `policy`, `github`, and `isolation` from `my-things-core` (and
`myorchestrator`, `myguard`, `mytelegrambot`) to do its orchestration.

## Testing

Fakes come from `mythings.testing` (opt-in via `pytest_plugins` in
`tests/conftest.py`; see `my-things-core/docs/CONVENTIONS.md`, "Shared test
fixtures"). Never copy fixture code into a conftest — only domain-specific
helpers live there.

`tests/test_integration_reporter.py` needs sibling checkouts and is excluded
from CI's file list; run it locally via `fleet_test.py --include-slow`.
