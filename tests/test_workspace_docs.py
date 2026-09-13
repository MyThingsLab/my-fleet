"""Tests for workspace documentation integrity."""

from pathlib import Path


def test_workspace_readme_documents_cad_tools() -> None:
    readme = Path(__file__).parents[1] / "workspace" / "README.md"
    assert readme.exists()
    content = readme.read_text(encoding="utf-8")

    assert "wave dispatching" in content
    assert "fleet_audit" in content
    assert "goal_digest" in content
    assert "ledger_audit" in content
