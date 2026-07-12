#!/usr/bin/env python3
"""Run every sibling repo's pytest suite in one command — the local pre-ship gate.

HARNESS.md tells every agent to run the suite the way CI will see it
(`GITHUB_ACTIONS=true pytest`) before shipping, but until now that rule had no
gate: nothing ran the fleet's suites together, so a core change that broke a
downstream tool surfaced only when that tool's own CI happened to run. This
script is the gate: it discovers every checkout with a `tests/` dir and runs
its suite in place, against the shared venv's editable core.

Deliberately NOT run in this repo's CI — the workflow checkout has no sibling
repos. Run it locally (or on the Pi) before shipping core or template changes:

    python -m myfleet.fleet_test                      # fast suites, whole fleet
    python -m myfleet.fleet_test --unattended         # as CI sees it (ASK -> DENY)
    python -m myfleet.fleet_test --only my-reporter my-archivist
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root).
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# (argv, cwd, env) -> exit code. Tests inject a fake so the pytest subprocess
# is the only mocked boundary.
Runner = Callable[[list[str], Path, dict[str, str]], int]


@dataclass(frozen=True)
class RepoResult:
    name: str
    passed: bool
    duration: float


def test_repos(root: Path, only: list[str] | None = None) -> list[Path]:
    repos = sorted(
        p.parent for p in root.glob("*/pyproject.toml") if (p.parent / "tests").is_dir()
    )
    if only:
        known = {r.name: r for r in repos}
        missing = [name for name in only if name not in known]
        if missing:
            raise SystemExit(f"no such test repo(s): {', '.join(missing)}")
        repos = [known[name] for name in only]
    return repos


def check_editable_core(root: Path) -> str | None:
    # The suites import mythings from the shared venv; if that resolves to a
    # site-packages copy (or another checkout), every run tests the wrong core
    # and the gate is a lie. Same trap _compat.check_environment guards.
    try:
        import mythings
    except ImportError:
        return "mythings is not importable — activate the shared venv first"
    resolved = Path(mythings.__file__).resolve().parent
    expected = (root / "my-things-core" / "src" / "mythings").resolve()
    if resolved != expected:
        return (
            f"mythings resolves to {resolved}, not the workspace checkout "
            f"{expected} — pip install -e {root / 'my-things-core'} first"
        )
    return None


def default_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
    return subprocess.run(argv, cwd=cwd, env=env).returncode


def run_suites(
    repos: list[Path],
    *,
    unattended: bool = False,
    include_slow: bool = False,
    runner: Runner | None = None,
) -> list[RepoResult]:
    # Resolved at call time so tests can monkeypatch default_runner.
    runner = runner or default_runner
    results: list[RepoResult] = []
    for repo in repos:
        argv = [sys.executable, "-m", "pytest", "tests", "-q"]
        if not include_slow:
            argv += ["-m", "not slow"]
        env = dict(os.environ)
        if unattended:
            env["GITHUB_ACTIONS"] = "true"
        # The checkout under test must win over the venv's editable install,
        # which can point at another checkout (found live: myarchivist resolved
        # to a stale ~/.claude/jobs tmp clone, silently testing old source).
        src = repo / "src"
        if src.is_dir():
            prior = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(src) + (os.pathsep + prior if prior else "")
        start = time.monotonic()
        code = runner(argv, repo, env)
        results.append(RepoResult(repo.name, code == 0, time.monotonic() - start))
    return results


def summarize(results: list[RepoResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = [
        f"{r.name:<{width}}  {'ok' if r.passed else 'FAIL':<4}  {r.duration:6.1f}s"
        for r in results
    ]
    failed = [r.name for r in results if not r.passed]
    total = sum(r.duration for r in results)
    lines.append(f"{len(results) - len(failed)}/{len(results)} suites green in {total:.1f}s")
    if failed:
        lines.append(f"FAILED: {', '.join(failed)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every sibling repo's pytest suite — the local pre-ship gate."
    )
    parser.add_argument("--root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--only", nargs="+", metavar="REPO")
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="run with GITHUB_ACTIONS=true, the way CI sees the suite (ASK -> DENY)",
    )
    parser.add_argument("--include-slow", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    error = check_editable_core(root)
    if error:
        print(error, file=sys.stderr)
        return 2
    repos = test_repos(root, args.only)
    if not repos:
        print(f"no repos with tests under {root}", file=sys.stderr)
        return 2

    results = run_suites(
        repos,
        unattended=args.unattended,
        include_slow=args.include_slow,
    )
    print(summarize(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
