"""Tests for myfleet.fleet_audit — fleet-wide graph audit command."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from myfleet.fleet_audit import (
    AuditReport,
    RepoFindings,
    _audit_single_repo,
    _discover_repos,
    _format_human,
    main,
    run_audit,
)


def _make_repo(tmp_path: Path, name: str, *, src_code: str | None = None) -> Path:
    """Create a minimal repo directory with a .git marker and optional source."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()  # just needs to exist as dir
    if src_code is not None:
        src_dir = repo / "src" / name.replace("-", "")
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("")
        (src_dir / "core.py").write_text(src_code)
    return repo


class TestDiscoverRepos:
    def test_finds_my_repos(self, tmp_path: Path):
        _make_repo(tmp_path, "my-alpha")
        _make_repo(tmp_path, "my-beta")
        (tmp_path / "not-a-tool").mkdir()  # not my- prefix
        repos = _discover_repos(tmp_path)
        names = [r.name for r in repos]
        assert names == ["my-alpha", "my-beta"]

    def test_skips_template(self, tmp_path: Path):
        _make_repo(tmp_path, "my-template")
        _make_repo(tmp_path, "my-real")
        repos = _discover_repos(tmp_path)
        assert [r.name for r in repos] == ["my-real"]

    def test_only_filter(self, tmp_path: Path):
        _make_repo(tmp_path, "my-alpha")
        _make_repo(tmp_path, "my-beta")
        _make_repo(tmp_path, "my-gamma")
        repos = _discover_repos(tmp_path, only=["my-beta"])
        assert [r.name for r in repos] == ["my-beta"]

    def test_skips_non_git_dirs(self, tmp_path: Path):
        d = tmp_path / "my-nongit"
        d.mkdir()
        # No .git directory
        repos = _discover_repos(tmp_path)
        assert repos == []


class TestAuditSingleRepo:
    def test_clean_repo(self, tmp_path: Path):
        repo = _make_repo(
            tmp_path,
            "my-clean",
            src_code=textwrap.dedent("""\
                def public_func():
                    return 42
            """),
        )
        findings = _audit_single_repo(repo)
        assert findings.repo == "my-clean"
        assert findings.error is None

    def test_handles_import_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = _make_repo(tmp_path, "my-broken")
        # Corrupt .git to cause issues but still pass discovery
        # The key test is that it catches exceptions gracefully
        monkeypatch.setattr(
            "myfleet.fleet_audit._index_repo",
            lambda rp: (_ for _ in ()).throw(RuntimeError("index boom")),
        )
        findings = _audit_single_repo(repo)
        assert findings.error is not None
        assert "index" in findings.error


class TestRunAudit:
    def test_scans_repos(self, tmp_path: Path):
        _make_repo(
            tmp_path,
            "my-alpha",
            src_code=textwrap.dedent("""\
                def alpha_func():
                    return 1
            """),
        )
        _make_repo(
            tmp_path,
            "my-beta",
            src_code=textwrap.dedent("""\
                def beta_func():
                    return 2
            """),
        )
        report = run_audit(root=tmp_path)
        assert report.repos_scanned == 2
        assert isinstance(report.per_repo, list)
        assert len(report.per_repo) == 2

    def test_filter_repos(self, tmp_path: Path):
        _make_repo(tmp_path, "my-alpha", src_code="def a(): pass\n")
        _make_repo(tmp_path, "my-beta", src_code="def b(): pass\n")
        report = run_audit(root=tmp_path, repos=["my-alpha"])
        assert report.repos_scanned == 1
        assert report.per_repo[0].repo == "my-alpha"


class TestRepoFindings:
    def test_total_findings(self):
        f = RepoFindings(
            repo="test",
            clones=[{"hash": "a", "type": "exact", "nodes": []}],
            test_gaps=[{"symbol": "b", "path": "c", "name": "d", "caller_count": 1}],
        )
        assert f.total_findings == 2

    def test_empty_findings(self):
        f = RepoFindings(repo="test")
        assert f.total_findings == 0


class TestAuditReport:
    def test_to_dict(self):
        r = AuditReport(
            timestamp="2024-01-01T00:00:00+00:00",
            repos_scanned=1,
            repos_with_findings=0,
            per_repo=[RepoFindings(repo="my-test")],
        )
        d = r.to_dict()
        assert d["repos_scanned"] == 1
        assert d["per_repo"][0]["repo"] == "my-test"
        assert d["per_repo"][0]["total_findings"] == 0


class TestFormatHuman:
    def test_clean_output(self):
        report = AuditReport(
            timestamp="2024-01-01T00:00:00+00:00",
            repos_scanned=1,
            repos_with_findings=0,
            per_repo=[RepoFindings(repo="my-clean")],
        )
        text = _format_human(report)
        assert "my-clean" in text
        assert "clean" in text

    def test_findings_output(self):
        report = AuditReport(
            timestamp="2024-01-01T00:00:00+00:00",
            repos_scanned=1,
            repos_with_findings=1,
            per_repo=[
                RepoFindings(
                    repo="my-messy",
                    test_gaps=[{"symbol": "s:fn", "path": "a.py", "name": "fn", "caller_count": 3}],
                )
            ],
        )
        text = _format_human(report)
        assert "my-messy" in text
        assert "Test Gaps" in text
        assert "fn" in text

    def test_error_output(self):
        report = AuditReport(
            timestamp="2024-01-01T00:00:00+00:00",
            repos_scanned=1,
            repos_with_findings=0,
            per_repo=[RepoFindings(repo="my-bad", error="index failed: boom")],
        )
        text = _format_human(report)
        assert "⚠" in text
        assert "boom" in text


class TestCLI:
    def test_main_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        _make_repo(tmp_path, "my-test", src_code="def f(): pass\n")
        rc = main(["--root", str(tmp_path), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["repos_scanned"] == 1

    def test_main_human(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        _make_repo(tmp_path, "my-test", src_code="def f(): pass\n")
        rc = main(["--root", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Fleet Graph Audit" in out

    def test_main_repos_filter(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        _make_repo(tmp_path, "my-alpha", src_code="def a(): pass\n")
        _make_repo(tmp_path, "my-beta", src_code="def b(): pass\n")
        rc = main(["--root", str(tmp_path), "--repos", "my-alpha", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["repos_scanned"] == 1
        assert data["per_repo"][0]["repo"] == "my-alpha"
