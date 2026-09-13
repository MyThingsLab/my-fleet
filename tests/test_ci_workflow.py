from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _trigger_block(name: str) -> str:
    # Cheap enough for a two-trigger file: slice from "  <name>:" up to the
    # next top-level ("  <word>:") line or EOF, so each assertion reads only
    # its own trigger's lines rather than the whole `on:` block.
    lines = CI_YML.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("  ") and not lines[i].startswith("    ") and lines[i].strip():
            end = i
            break
    return "\n".join(lines[start:end])


def test_pull_request_trigger_has_no_base_branch_filter() -> None:
    # #52: `branches: [main]` on `pull_request` meant a PR based on another
    # branch (a stacked PR) produced no `test` run at all, so it could never
    # satisfy the check main's branch protection requires -- unmergeable
    # regardless of how good the code was.
    assert "branches" not in _trigger_block("pull_request")


def test_push_trigger_still_scoped_to_main() -> None:
    assert "branches: [main]" in _trigger_block("push")
