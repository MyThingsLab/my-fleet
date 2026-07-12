from __future__ import annotations

import base64

import pytest

import myfleet.merge_order_prs as mo
from myfleet.merge_ready_prs import PR


def pr(number: int, *, base: str = "main", head: str = "") -> PR:
    return PR(
        repo="o/r",
        number=number,
        title=f"pr {number}",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=[],
        base=base,
        head=head or f"branch-{number}",
    )


def test_repo_order_puts_dependencies_first() -> None:
    graph = {
        "my-tool": {"my-things-core", "my-guard"},
        "my-guard": {"my-things-core"},
        "my-things-core": set(),
    }
    assert mo.repo_order(sorted(graph), graph) == ["my-things-core", "my-guard", "my-tool"]


def test_repo_order_ignores_dependencies_outside_the_repo_list() -> None:
    graph = {"my-tool": {"not-checked-out"}}
    assert mo.repo_order(["my-tool"], graph) == ["my-tool"]


def test_repo_order_breaks_cycles_deterministically() -> None:
    graph = {"a": {"b"}, "b": {"a"}}
    assert mo.repo_order(["a", "b"], graph) == ["a", "b"]


def test_pr_order_merges_stack_base_first() -> None:
    base_pr = pr(1, head="feat/base")
    stacked = pr(2, base="feat/base", head="feat/top")
    assert [p.number for p in mo.pr_order_within_repo([stacked, base_pr])] == [1, 2]


def test_pr_order_unstacked_prs_come_in_number_order() -> None:
    assert [p.number for p in mo.pr_order_within_repo([pr(9), pr(3)])] == [3, 9]


def test_pr_order_survives_a_stack_cycle() -> None:
    a = pr(1, base="b-head", head="a-head")
    b = pr(2, base="a-head", head="b-head")
    ordered = mo.pr_order_within_repo([a, b])
    assert sorted(p.number for p in ordered) == [1, 2]


def _ci_yaml_response(content: str):
    encoded = base64.b64encode(content.encode()).decode()

    def _fake_run(argv: list[str]) -> str:
        assert argv[:2] == ["gh", "api"]
        return encoded

    return _fake_run


def test_sibling_dependencies_parses_ci_and_applies_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = (
        'pip install "mythings-core @ git+https://github.com/o/mythings-core@main"\n'
        'pip install "my-guard @ git+https://github.com/o/my-guard@main"\n'
    )
    monkeypatch.setattr(mo, "_run", _ci_yaml_response(ci))
    assert mo.sibling_dependencies("o", "my-tool") == {"my-things-core", "my-guard"}


def test_sibling_dependencies_never_lists_the_repo_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = 'pip install "my-guard @ git+https://github.com/o/my-guard@main"\n'
    monkeypatch.setattr(mo, "_run", _ci_yaml_response(ci))
    assert mo.sibling_dependencies("o", "my-guard") == set()


def test_sibling_dependencies_missing_workflow_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(argv: list[str]) -> str:
        raise RuntimeError("404")

    monkeypatch.setattr(mo, "_run", _boom)
    assert mo.sibling_dependencies("o", "my-tool") == set()


def test_main_prints_cross_repo_then_stack_order(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(mo, "list_org_repos", lambda org: ["my-tool", "my-things-core"])
    monkeypatch.setattr(
        mo,
        "sibling_dependencies",
        lambda org, repo: {"my-things-core"} if repo == "my-tool" else set(),
    )
    prs = {
        "my-things-core": [pr(7)],
        "my-tool": [pr(2, base="feat/base"), pr(1, head="feat/base")],
    }
    monkeypatch.setattr(mo, "list_open_prs", lambda repo: prs[repo])
    assert mo.main([]) == 0
    out = capsys.readouterr().out
    assert out.index("my-things-core") < out.index("my-tool")
    assert out.index("#1") < out.index("#2")
    assert "[stacked on feat/base]" in out
