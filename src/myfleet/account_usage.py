"""Per-account Claude Code subscription usage monitor and rotation.

Not a fleet `My[X]` tool -- a sibling module to fleet_dispatch.py, same
workspace-tooling status as fleet_usage.py.

Queries each account's *subscription session usage* (the rolling 5-hour
window Claude Code enforces per account) via a headless `claude -p "/usage"`
call under that account's own CLAUDE_CONFIG_DIR, and filters the account list
down to the ones still under a configurable ceiling (default 90%) so
fleet_dispatch.py never dispatches a worker on an account that's about to hit
its cap mid-run.

This is a live probe, not a stored counter: each call spends no tokens
(`/usage` is a metadata reply, not a generation) but does cost one real
headless CLI invocation per account, so callers should poll on a cadence
(a systemd timer), not per-issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass


class UsageCheckError(RuntimeError):
    """The `claude -p "/usage"` call itself failed (not a parse failure)."""


@dataclass(frozen=True)
class AccountUsage:
    config_dir: str
    session_pct: int
    session_resets: str
    week_pct: int
    week_resets: str
    # Set when the probe itself failed (network blip, stale auth, timeout)
    # rather than returning a real reading. session_pct is forced to 100 in
    # that case so the account still sorts into "over" (unusable this cycle)
    # without a caller needing to check this field separately.
    error: str = ""
    provider: str = "claude"

    @property
    def over(self) -> bool:
        return self.session_pct >= 90


_SESSION_RE = re.compile(r"Current session:\s*(\d+)%\s*used(?:\s*·\s*resets\s*([^\n]+))?")
_WEEK_RE = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used(?:\s*·\s*resets\s*([^\n]+))?")


def _run_usage_probe(config_dir: str, timeout: float = 30.0) -> str:
    expanded = os.path.expanduser(config_dir)
    if not os.path.isdir(expanded):
        raise UsageCheckError(f"config dir does not exist: {config_dir} (expanded: {expanded})")
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = expanded
    proc = subprocess.run(
        ["claude", "-p", "/usage", "--output-format", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # claude puts "Failed to authenticate: OAuth session expired" on stdout,
        # not stderr, so reporting stderr alone gave an empty reason -- the whole
        # diagnosis was thrown away at the one point it was available. See
        # myfleet.preflight, which is what should catch this case first.
        reason = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[:200]
        raise UsageCheckError(f"claude -p /usage failed for {config_dir}: {reason}")
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise UsageCheckError(
            f"unparseable /usage output for {config_dir}: {proc.stdout[:200]}"
        ) from exc
    result = obj.get("result", "")
    if not result:
        raise UsageCheckError(f"empty /usage result for {config_dir}")
    return result


class ClaudeUsageProvider:
    def check_account(self, config_dir: str, timeout: float = 30.0) -> AccountUsage:
        config_dir = os.path.expanduser(config_dir)
        text = _run_usage_probe(config_dir, timeout=timeout)
        session = _SESSION_RE.search(text)
        week = _WEEK_RE.search(text)
        if not session:
            raise UsageCheckError(
                f"could not find session usage in reply for {config_dir}: {text!r}"
            )
        return AccountUsage(
            config_dir=config_dir,
            session_pct=int(session.group(1)),
            session_resets=(session.group(2) or "").strip(),
            week_pct=int(week.group(1)) if week else -1,
            week_resets=(week.group(2) or "").strip() if week else "",
            provider="claude",
        )


class GeminiUsageProvider:
    def check_account(self, config_dir: str, timeout: float = 30.0) -> AccountUsage:
        config_dir = os.path.expanduser(config_dir)
        if not os.path.isdir(config_dir):
            raise UsageCheckError(f"config dir does not exist: {config_dir}")
        bin_name = os.environ.get("GEMINI_CLI_BIN", "agy")
        env = os.environ.copy()
        env["GEMINI_CONFIG_DIR"] = config_dir
        try:
            proc = subprocess.run(
                [bin_name, "--version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                reason = (
                    proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
                )[:200]
                raise UsageCheckError(f"{bin_name} --version failed for {config_dir}: {reason}")
        except FileNotFoundError as exc:
            raise UsageCheckError(f"CLI binary not found: {bin_name}") from exc

        return AccountUsage(
            config_dir=config_dir,
            session_pct=0,
            session_resets="",
            week_pct=-1,
            week_resets="",
            provider="gemini",
        )


PROVIDERS = {
    "claude": ClaudeUsageProvider(),
    "gemini": GeminiUsageProvider(),
}


def check_account(
    config_dir: str, timeout: float = 30.0, provider: str = "claude"
) -> AccountUsage:
    p = PROVIDERS.get(provider)
    if not p:
        raise UsageCheckError(f"unknown provider: {provider}")
    return p.check_account(config_dir, timeout=timeout)


def check_all(config_dirs: list[str], provider: str = "claude") -> list[AccountUsage]:
    return [check_account(d, provider=provider) for d in config_dirs]


def select_accounts(
    config_dirs: list[str], max_session_pct: int = 90, provider: str = "claude"
) -> tuple[list[AccountUsage], list[AccountUsage]]:
    """Split accounts into (usable, over-threshold), preserving input order.

    A single account's probe failing (network blip, stale auth, a hung
    probe call) must not take out the whole batch -- callers
    like run_fleet_cycle.sh depend on this function degrading gracefully
    (excluding just the unreachable account) rather than raising, the same
    way fleet_dispatch.py's own per-account dispatch loop never lets one
    account's crash stop it from collecting every other account's outcome.
    """
    usable, over = [], []
    for config_dir in config_dirs:
        try:
            usage = check_account(config_dir, provider=provider)
        except UsageCheckError as exc:
            usage = AccountUsage(
                config_dir=os.path.expanduser(config_dir),
                session_pct=100,
                session_resets="",
                week_pct=-1,
                week_resets="",
                error=str(exc),
                provider=provider,
            )
        (over if usage.session_pct >= max_session_pct else usable).append(usage)
    return usable, over


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check AI subscription session usage per account and pick usable ones."
    )
    parser.add_argument(
        "--accounts", required=True, help="comma-separated config directory paths"
    )
    parser.add_argument(
        "--provider",
        choices=("claude", "gemini"),
        default="claude",
        help="usage provider (default: %(default)s)",
    )
    parser.add_argument("--max-session-pct", type=int, default=90)
    parser.add_argument(
        "--quiet", action="store_true", help="print only the usable config-dir list"
    )
    args = parser.parse_args(argv)

    config_dirs = [d.strip() for d in args.accounts.split(",") if d.strip()]
    usable, over = select_accounts(
        config_dirs, max_session_pct=args.max_session_pct, provider=args.provider
    )

    # Diagnostics go to stderr, deliberately: stdout carries exactly one line
    # (the usable CSV, possibly empty) for callers like run_fleet_cycle.sh that
    # capture it via `USABLE="$(... )"`. Mixing a human-readable [ERROR]/[OVER]
    # line onto the same stream a caller parses meant `$(cmd | tail -1)` could
    # pick a diagnostic line instead of the CSV, depending on how many trailing
    # newlines command substitution happened to strip that run.
    if not args.quiet:
        for u in usable + over:
            if u.error:
                print(f"[ERROR] {u.config_dir}: probe failed: {u.error}", file=sys.stderr)
                continue
            flag = "OVER" if u.session_pct >= args.max_session_pct else "ok"
            print(
                f"[{flag}] {u.config_dir} ({u.provider}): session {u.session_pct}% (resets {u.session_resets}), "
                f"week {u.week_pct}% (resets {u.week_resets})",
                file=sys.stderr,
            )

    print(",".join(u.config_dir for u in usable))
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
