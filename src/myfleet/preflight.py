#!/usr/bin/env python3
"""Prove a dispatch account can actually work, before a candidate is spent on it.

An expired OAuth session is, from the fleet's side, indistinguishable from a
worker that had nothing to do: the session dies before it starts, the dispatcher
records a failure and moves on, and the cycle keeps ticking with a completion
rate of zero. The same is true of an untrusted workspace, which silently drops
the account's `permissions.allow` entries and leaves the worker unable to run
the commands its issue needs.

Both are cheap to detect and expensive to miss, so they are checked up front --
before any candidate issue is picked -- and reported as their own ledger
outcomes rather than folded into a generic session failure.

Two details worth keeping:

- `claude` prints "Failed to authenticate" on **stdout**, not stderr, and exits
  1. Code that reports only `proc.stderr` shows an empty reason, which is how
  this stayed invisible.
- The probe asserts on the *reply*, not the exit status. A process that exits 0
  having said something other than what it was asked is not a working account,
  and checking that a config directory exists proves nothing at all.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PROBE_PROMPT = "reply with exactly: ALIVE"
PROBE_TOKEN = "ALIVE"

OK = "ok"
CONFIG_MISSING = "config_missing"
AUTH_EXPIRED = "auth_expired"
PROBE_FAILED = "probe_failed"
WORKSPACE_UNTRUSTED = "workspace_untrusted"

# Substrings claude uses when the OAuth session cannot be refreshed. Matched
# against stdout and stderr together because it does not consistently pick one.
_AUTH_MARKERS = ("failed to authenticate", "oauth session expired", "invalid api key")

_GEMINI_AUTH_MARKERS = (
    "invalid api key",
    "unauthenticated",
    "permission denied",
    "authentication failed",
    "could not find credentials",
    "unauthorized",
)


@dataclass(frozen=True)
class AccountPreflight:
    config_dir: str
    outcome: str
    detail: str
    authenticated: bool = False
    trusted: bool = False

    @property
    def usable(self) -> bool:
        return self.outcome == OK


class PreflightProvider(Protocol):
    def check_account(
        self, config_dir: str, workspace: Path, *, timeout: float = 120.0
    ) -> AccountPreflight: ...


def workspace_is_trusted(config_dir: str, workspace: Path) -> bool:
    """Whether this account has accepted the trust dialog for `workspace`.

    An untrusted workspace does not fail loudly -- claude prints "Ignoring N
    permissions.allow entries" and carries on with a narrower allowlist than the
    worker was configured with, so the worker fails later on a permission it was
    supposed to have.
    """
    config = Path(os.path.expanduser(config_dir)) / ".claude.json"
    if not config.is_file():
        return False
    try:
        projects = json.loads(config.read_text()).get("projects", {})
    except (json.JSONDecodeError, OSError):
        return False
    entry = projects.get(str(workspace)) or {}
    return bool(entry.get("hasTrustDialogAccepted"))


class ClaudePreflightProvider:
    def check_account(
        self, config_dir: str, workspace: Path, *, timeout: float = 120.0
    ) -> AccountPreflight:
        expanded = os.path.expanduser(config_dir)
        if not os.path.isdir(expanded):
            return AccountPreflight(
                config_dir=expanded,
                outcome=CONFIG_MISSING,
                detail=f"config dir does not exist: {config_dir} (expanded: {expanded})",
            )

        trusted = workspace_is_trusted(expanded, workspace)
        env = {**os.environ, "CLAUDE_CONFIG_DIR": expanded}
        try:
            proc = subprocess.run(
                ["claude", "-p", PROBE_PROMPT, "--max-turns", "1"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"auth probe timed out after {timeout:.0f}s",
                trusted=trusted,
            )
        except OSError as exc:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"could not run claude: {exc}",
                trusted=trusted,
            )

        output = f"{proc.stdout}\n{proc.stderr}".strip()
        lowered = output.lower()
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return AccountPreflight(
                config_dir=expanded,
                outcome=AUTH_EXPIRED,
                detail=output.splitlines()[0][:200] if output else "authentication failed",
                trusted=trusted,
            )
        if proc.returncode != 0:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"claude exited {proc.returncode}: {output[:200]}",
                trusted=trusted,
            )
        if PROBE_TOKEN not in proc.stdout:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"probe replied without {PROBE_TOKEN!r}: {proc.stdout.strip()[:200]}",
                trusted=trusted,
            )

        if not trusted:
            return AccountPreflight(
                config_dir=expanded,
                outcome=WORKSPACE_UNTRUSTED,
                detail=(
                    f"{workspace} is not trusted for this account, so its permissions.allow "
                    "entries are ignored. Set projects[...].hasTrustDialogAccepted in the "
                    "account's .claude.json, or accept the dialog once interactively."
                ),
                authenticated=True,
            )
        return AccountPreflight(
            config_dir=expanded,
            outcome=OK,
            detail=f"authenticated, and {workspace} is trusted",
            authenticated=True,
            trusted=True,
        )


class GeminiPreflightProvider:
    def check_account(
        self, config_dir: str, workspace: Path, *, timeout: float = 120.0
    ) -> AccountPreflight:
        expanded = os.path.expanduser(config_dir)
        if not os.path.isdir(expanded):
            return AccountPreflight(
                config_dir=expanded,
                outcome=CONFIG_MISSING,
                detail=f"config dir does not exist: {config_dir} (expanded: {expanded})",
            )

        bin_name = os.environ.get("GEMINI_CLI_BIN", "agy")
        env = {**os.environ, "GEMINI_CONFIG_DIR": expanded}
        try:
            proc = subprocess.run(
                [bin_name, "--version"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"auth probe timed out after {timeout:.0f}s",
                trusted=True,
            )
        except OSError as exc:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"could not run {bin_name}: {exc}",
                trusted=True,
            )

        output = f"{proc.stdout}\n{proc.stderr}".strip()
        lowered = output.lower()
        if any(marker in lowered for marker in _GEMINI_AUTH_MARKERS):
            return AccountPreflight(
                config_dir=expanded,
                outcome=AUTH_EXPIRED,
                detail=output.splitlines()[0][:200] if output else "authentication failed",
                trusted=True,
            )
        if proc.returncode != 0:
            return AccountPreflight(
                config_dir=expanded,
                outcome=PROBE_FAILED,
                detail=f"{bin_name} exited {proc.returncode}: {output[:200]}",
                trusted=True,
            )

        return AccountPreflight(
            config_dir=expanded,
            outcome=OK,
            detail=f"authenticated ({bin_name}), and {workspace} is accessible",
            authenticated=True,
            trusted=True,
        )


PREFLIGHT_PROVIDERS: dict[str, PreflightProvider] = {
    "claude": ClaudePreflightProvider(),
    "gemini": GeminiPreflightProvider(),
}


def get_preflight_provider(provider: str = "claude") -> PreflightProvider:
    if provider not in PREFLIGHT_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}, expected one of {list(PREFLIGHT_PROVIDERS)}"
        )
    return PREFLIGHT_PROVIDERS[provider]


def check_account(
    config_dir: str, workspace: Path, *, timeout: float = 120.0, provider: str = "claude"
) -> AccountPreflight:
    return get_preflight_provider(provider).check_account(
        config_dir, workspace, timeout=timeout
    )


def select_accounts(
    config_dirs: list[str],
    workspace: Path,
    *,
    timeout: float = 120.0,
    provider: str = "claude",
) -> tuple[list[AccountPreflight], list[AccountPreflight]]:
    """Split accounts into (usable, blocked), preserving input order.

    One account's probe failing must never take out the batch -- the point is to
    lose exactly the accounts that cannot work and keep every other one.
    """
    usable: list[AccountPreflight] = []
    blocked: list[AccountPreflight] = []
    for config_dir in config_dirs:
        result = check_account(config_dir, workspace, timeout=timeout, provider=provider)
        (usable if result.usable else blocked).append(result)
    return usable, blocked


def main(argv: list[str] | None = None) -> int:
    from myfleet.fleet_dispatch import WORKSPACE_ROOT

    parser = argparse.ArgumentParser(
        description="Check that each dispatch account can authenticate and run in the workspace."
    )
    parser.add_argument(
        "--accounts", required=True, help="comma-separated config dir paths"
    )
    parser.add_argument(
        "--provider",
        choices=list(PREFLIGHT_PROVIDERS),
        default="claude",
        help="model provider for authentication probe (default: %(default)s)",
    )
    parser.add_argument("--workspace", default=str(WORKSPACE_ROOT))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--quiet", action="store_true", help="print only the usable config-dir list"
    )
    args = parser.parse_args(argv)

    config_dirs = [d.strip() for d in args.accounts.split(",") if d.strip()]
    usable, blocked = select_accounts(
        config_dirs,
        Path(args.workspace).resolve(),
        timeout=args.timeout,
        provider=args.provider,
    )

    # Same stream split as account_usage.main: stdout carries exactly one line
    # (the usable CSV, possibly empty) so a shell caller can capture it, and
    # every human-readable line goes to stderr.
    if not args.quiet:
        for result in usable + blocked:
            flag = "ok" if result.usable else result.outcome.upper()
            print(f"[{flag}] {result.config_dir}: {result.detail}", file=sys.stderr)

    print(",".join(r.config_dir for r in usable))
    return 0 if usable else 1



if __name__ == "__main__":
    raise SystemExit(main())
