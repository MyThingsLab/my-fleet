#!/usr/bin/env python3
"""Where the MyThingsLab fleet root is, for scripts that must not guess wrong.

Every module here climbed out of its own source file to find the fleet root:

    WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

which is correct for `<root>/my-fleet/src/myfleet/<mod>.py` and wrong wherever
the checkout is not directly under the root. `mythings.isolation.Workspace`
puts it at `<prefix>/tree`, so `parents[3]` becomes `<prefix>` -- a scratch dir
under `/tmp` that is deleted when the worktree is torn down.

That is not a cosmetic mistake. `fleet_ask.BOT_LEDGER` is derived from it, and
is also the `--ledger` handed to `mytelegrambot ask`, which blocks polling that
file for the human's Allow/Deny. The daemon writes the callback to the *real*
ledger, so an ASK raised from a worktree waits on a path nobody writes, times
out, and MyGuard reads the non-zero exit as DENY. The human is never asked and
the run cannot tell that apart from a refusal (my-fleet#48).

`fleet_dispatch` already owns the worker's environment -- it arms
`MYTHINGS_ASK_CMD` there -- so it can simply say where the root is instead of
letting each worker re-derive it from a path that moved.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_ENV = "MYTHINGS_WORKSPACE_ROOT"

# Runtime state for the fleet loop. Named after this repo; it was
# `.fleet-dispatch/` back when the scripts lived in the workspace-root repo of
# that name, now archived. It lives here rather than in fleet_dispatch.py
# because the writer is not the only module that needs it and the reader must
# not import the writer: heartbeat.py deliberately avoids fleet_dispatch (which
# drags in myorchestrator), so it spelled the path out a second time -- and when
# #62 renamed the directory, only one of the two copies moved. The dead-man's
# switch then read a file nobody writes and reported both ticks as "never
# recorded" on every run, which is precisely the false-alive state it exists to
# detect.
RUNTIME_DIR_NAME = ".my-fleet"

# A fleet root holds one checkout per tool. These two are the ones every
# derivation above ultimately reaches for -- the SDK and this repo -- so their
# presence is what distinguishes a real root from a plausible-looking path.
_MARKERS = ("my-things-core", "my-fleet")


def looks_like_fleet_root(path: Path) -> bool:
    return all((path / marker).is_dir() for marker in _MARKERS)


def fleet_root(module_file: str) -> Path:
    override = os.environ.get(ROOT_ENV, "").strip()
    if override:
        return Path(override).resolve()
    file_path = Path(module_file).resolve()
    for parent in [file_path, *file_path.parents]:
        if looks_like_fleet_root(parent):
            return parent
    raise RuntimeError(
        f"Cannot resolve MyThingsLab fleet root from {module_file}. "
        f"Set {ROOT_ENV} or ensure the path is within a valid fleet root."
    )


def runtime_dir(root: Path) -> Path:
    return root / RUNTIME_DIR_NAME


def ledger_path(root: Path) -> Path:
    """The fleet's shared runtime ledger -- what fleet_cycle writes heartbeats to."""
    return runtime_dir(root) / "ledger.jsonl"
