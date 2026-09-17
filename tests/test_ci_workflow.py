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


def test_checkout_fetches_full_history_for_the_changed_files_diff() -> None:
    # The `Check changed files` step decides whether this job tests anything at
    # all, and it diffs against a base SHA a shallow clone does not have. With
    # the default fetch-depth that diff failed on every run and the job skipped
    # Install, Ruff and Tests while still reporting `success` -- so the one
    # check branch protection requires, and the only evidence `myfleet.accept`
    # is allowed to read, was green without running.
    text = CI_YML.read_text(encoding="utf-8")
    assert "fetch-depth: 0" in text


def test_an_unresolvable_diff_runs_the_job_rather_than_skipping_it() -> None:
    # Fail closed. `|| true` on the diff itself turned "I could not tell what
    # changed" into "nothing changed", which is the cheap answer and the unsafe
    # one at the same time. A force-push or a GC'd base SHA can still make the
    # diff unresolvable with full history, so the handling has to be explicit.
    text = CI_YML.read_text(encoding="utf-8")
    assert "if ! DIFF=$(git diff --name-only" in text
    assert 'git diff --name-only "$BASE" "$HEAD" | grep' not in text
