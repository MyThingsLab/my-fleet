#!/usr/bin/env python3
"""Apply `pythonpath = ["src"]` + its regression test across the product repos.

Deterministic on purpose. The obvious move is to dispatch my-coder at this --
24 repos, one identical two-line change -- and it is the wrong move: my-coder's
verification step is the thing being fixed, so a worker would certify its own
fix with the bug still active. Same reasoning as the kernel fan-out.

Every repo is verified load-bearing before it is committed: with `pythonpath`
disabled the new test must FAIL. A test that passes either way is decoration,
and decoration is what this whole exercise is about not shipping.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/lollinux/Desktop/MyThingsLab")
VENV = ROOT / ".venv/bin"
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fanout2")
BRANCH = "chore/test-this-checkouts-source"

ANCHOR = "[tool.pytest.ini_options]\n"
INSERT = '''pythonpath = ["src"]
'''
COMMENT = """# Prepended to sys.path, so a git-worktree checkout tests its OWN src rather
# than the editable install in the shared root .venv -- which points at the
# main checkout. A worker session runs in a worktree, and without this its
# suite silently exercises main's code instead of the change under review.
"""

TEST = '''from pathlib import Path

import {package}


def test_the_suite_imports_this_checkouts_source_not_the_editable_install() -> None:
    # Every repo is installed editable into the shared root .venv, and that
    # install points at its MAIN checkout. A worker session runs inside a git
    # WORKTREE, so without `pythonpath = ["src"]` in the pytest config the
    # editable install wins and the suite exercises main's code instead of the
    # change under review.
    #
    # The failure is asymmetric: a change that ADDS a symbol fails loudly on
    # ImportError, but a change that MODIFIES existing behaviour passes green
    # against source it never ran. The quiet case is the dangerous one, and it
    # is inherited by every automated worker that trusts a green suite as
    # evidence its diff is sound.
    repo_root = Path(__file__).resolve().parent.parent
    imported = Path({package}.__file__).resolve()
    assert imported.is_relative_to(repo_root), (
        f"tests imported {{imported}}, which is outside this checkout ({{repo_root}}). "
        "The editable install shadowed the worktree -- check `pythonpath` in "
        "[tool.pytest.ini_options]."
    )
'''


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def package_of(tree: Path) -> str | None:
    src = tree / "src"
    if not src.is_dir():
        return None
    pkgs = [p.name for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    return pkgs[0] if len(pkgs) == 1 else None


def apply(repo: str) -> str:
    tree = WORK / repo
    if tree.exists():
        return "SKIP already present"
    clone = run(["gh", "repo", "clone", f"MyThingsLab/{repo}", str(tree), "--", "-q"], WORK)
    if clone.returncode != 0:
        return f"FAIL clone: {clone.stderr.strip()[:80]}"
    run(["git", "checkout", "-q", "-b", BRANCH], tree)

    package = package_of(tree)
    if package is None:
        return "SKIP no single src/ package"

    pyproject = tree / "pyproject.toml"
    text = pyproject.read_text()
    if "pythonpath" in text:
        return "SKIP pythonpath already set"
    if text.count(ANCHOR) != 1:
        return f"SKIP anchor appears {text.count(ANCHOR)}x"
    pyproject.write_text(text.replace(ANCHOR, ANCHOR + COMMENT + INSERT, 1))

    test_file = tree / "tests" / "test_import_provenance.py"
    if test_file.exists():
        return "SKIP test already exists"
    test_file.write_text(TEST.format(package=package))

    # Load-bearing check: without pythonpath the new test MUST fail.
    without = run(
        [str(VENV / "python"), "-m", "pytest", "tests/test_import_provenance.py",
         "-o", "pythonpath=", "-q"],
        tree,
    )
    if without.returncode == 0:
        return "FAIL not load-bearing (passes without pythonpath)"

    full = run([str(VENV / "python"), "-m", "pytest", "-q"], tree)
    if full.returncode != 0:
        return f"FAIL suite red: {full.stdout.strip().splitlines()[-1][:70]}"

    lint = run([str(VENV / "ruff"), "check", "tests/test_import_provenance.py"], tree)
    if lint.returncode != 0:
        return f"FAIL ruff: {lint.stdout.strip()[:70]}"
    run([str(VENV / "ruff"), "format", "tests/test_import_provenance.py"], tree)

    return f"READY {package}"


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    repos = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    for repo in repos:
        print(f"{apply(repo):<50} {repo}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
