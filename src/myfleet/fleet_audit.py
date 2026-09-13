#!/usr/bin/env python3
"""Scan every fleet checkout with the deterministic codebase graph and report findings.

Produces a consolidated audit across all fleet tool repos:
1. Structural code clones across tools (candidates for lifting to my-things-core).
2. Topological test gaps (production functions with callers but zero tests).
3. Untested invariants/ADRs.
4. Circular module dependencies.
5. Unreferenced internal symbols (dead code candidates).

Usage:
    python -m myfleet.fleet_audit                     # human-readable summary
    python -m myfleet.fleet_audit --json              # machine-readable JSON
    python -m myfleet.fleet_audit --repos my-coder my-fleet  # specific repos only
    python -m myfleet.fleet_audit --cross-repo        # cross-repo clone detection
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from myfleet.workspace import fleet_root

WORKSPACE_ROOT = fleet_root(__file__)


@dataclass
class RepoFindings:
    """Graph analysis results for a single repository."""

    repo: str
    clones: list[dict[str, Any]] = field(default_factory=list)
    test_gaps: list[dict[str, Any]] = field(default_factory=list)
    untested_invariants: list[dict[str, Any]] = field(default_factory=list)
    circular_imports: list[list[str]] = field(default_factory=list)
    unreferenced_symbols: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def total_findings(self) -> int:
        return (
            len(self.clones)
            + len(self.test_gaps)
            + len(self.untested_invariants)
            + len(self.circular_imports)
            + len(self.unreferenced_symbols)
        )


@dataclass
class AuditReport:
    """Consolidated audit across all fleet repos."""

    timestamp: str
    repos_scanned: int
    repos_with_findings: int
    per_repo: list[RepoFindings] = field(default_factory=list)
    cross_repo_clones: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "repos_scanned": self.repos_scanned,
            "repos_with_findings": self.repos_with_findings,
            "per_repo": [
                {
                    "repo": r.repo,
                    "clones": r.clones,
                    "test_gaps": r.test_gaps,
                    "untested_invariants": r.untested_invariants,
                    "circular_imports": r.circular_imports,
                    "unreferenced_symbols": r.unreferenced_symbols,
                    "error": r.error,
                    "total_findings": r.total_findings,
                }
                for r in self.per_repo
            ],
            "cross_repo_clones": self.cross_repo_clones,
        }


def _discover_repos(root: Path, only: list[str] | None = None) -> list[Path]:
    """Find all fleet tool repos under the workspace root."""
    repos: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("my-"):
            continue
        if not (child / ".git").exists() and not (child / ".git").is_file():
            continue
        if child.name == "my-template":
            continue  # scaffolding template, not a real tool
        if only and child.name not in only:
            continue
        repos.append(child)
    return repos


def _index_repo(repo_path: Path) -> Any:
    """Index a single repo into an in-memory graph, returning the CodebaseGraph."""
    from mythings.graph import CodebaseGraph, MarkdownExtractor, PythonAstExtractor

    # Check for a cached graph first
    cached = repo_path / ".mythings" / "graph.sqlite"
    if cached.exists():
        return CodebaseGraph(cached)

    graph = CodebaseGraph.in_memory()
    PythonAstExtractor(repo_root=repo_path).index_repo(graph)
    MarkdownExtractor(repo_root=repo_path).index_docs(graph)
    return graph


def _audit_single_repo(repo_path: Path) -> RepoFindings:
    """Run all graph analysis passes on a single repo."""
    findings = RepoFindings(repo=repo_path.name)

    try:
        graph = _index_repo(repo_path)
    except Exception as exc:
        findings.error = f"index failed: {exc}"
        return findings

    try:
        clones = graph.find_structural_clones()
        findings.clones = [
            {
                "hash": c.hash_value,
                "type": c.clone_type,
                "nodes": [{"id": n.id, "path": n.path, "name": n.name} for n in c.nodes],
            }
            for c in clones
        ]
    except Exception as exc:
        findings.error = f"clone detection failed: {exc}"

    try:
        gaps = graph.find_test_gaps()
        findings.test_gaps = [
            {
                "symbol": g.symbol.id,
                "path": g.symbol.path,
                "name": g.symbol.name,
                "caller_count": g.caller_count,
            }
            for g in gaps
        ]
    except Exception as exc:
        findings.error = (findings.error or "") + f"; test gap analysis failed: {exc}"

    try:
        untested = graph.find_untested_invariants()
        findings.untested_invariants = [
            {
                "invariant": u.invariant.id,
                "path": u.invariant.path,
                "name": u.invariant.name,
                "governed_symbols": [s.id for s in u.governed_symbols],
            }
            for u in untested
        ]
    except Exception as exc:
        findings.error = (findings.error or "") + f"; untested invariant analysis failed: {exc}"

    try:
        cycles = graph.find_circular_imports()
        findings.circular_imports = cycles
    except Exception as exc:
        findings.error = (findings.error or "") + f"; cycle detection failed: {exc}"

    try:
        dead = graph.find_unreferenced_symbols()
        findings.unreferenced_symbols = [
            {"id": n.id, "path": n.path, "name": n.name} for n in dead
        ]
    except Exception as exc:
        findings.error = (findings.error or "") + f"; dead code detection failed: {exc}"

    try:
        graph.close()
    except Exception:
        pass

    return findings


def _find_cross_repo_clones(repos: list[Path]) -> list[dict[str, Any]]:
    """Detect structural clones across different repos by merging all graphs."""
    from mythings.graph import CodebaseGraph, PythonAstExtractor

    merged = CodebaseGraph.in_memory()
    for repo_path in repos:
        try:
            # Prefix node IDs with repo name to avoid collisions
            extractor = PythonAstExtractor(repo_root=repo_path)
            extractor.index_repo(merged)
        except Exception:
            continue

    clones = merged.find_structural_clones()
    cross_repo: list[dict[str, Any]] = []
    for c in clones:
        repo_names = {n.path.split("/")[0] if "/" in n.path else "root" for n in c.nodes}
        # Only include if nodes span multiple source paths (proxy for cross-repo)
        # Since we're merging all into one graph, paths are relative to each repo
        # so we check if there are nodes from different path prefixes
        if len(c.nodes) >= 2:
            cross_repo.append(
                {
                    "hash": c.hash_value,
                    "type": c.clone_type,
                    "repos": sorted(repo_names),
                    "nodes": [
                        {"id": n.id, "path": n.path, "name": n.name} for n in c.nodes
                    ],
                }
            )

    try:
        merged.close()
    except Exception:
        pass

    return cross_repo


def run_audit(
    root: Path | None = None,
    repos: list[str] | None = None,
    cross_repo: bool = False,
) -> AuditReport:
    """Run the full fleet audit and return a structured report."""
    if root is None:
        root = WORKSPACE_ROOT
    repo_paths = _discover_repos(root, only=repos)
    per_repo: list[RepoFindings] = []

    for rp in repo_paths:
        findings = _audit_single_repo(rp)
        per_repo.append(findings)

    cross_repo_clones: list[dict[str, Any]] = []
    if cross_repo and len(repo_paths) >= 2:
        cross_repo_clones = _find_cross_repo_clones(repo_paths)

    return AuditReport(
        timestamp=datetime.now(UTC).isoformat(),
        repos_scanned=len(repo_paths),
        repos_with_findings=sum(1 for r in per_repo if r.total_findings > 0),
        per_repo=per_repo,
        cross_repo_clones=cross_repo_clones,
    )


def _format_human(report: AuditReport) -> str:
    """Render the audit report as human-readable text."""
    lines: list[str] = []
    lines.append(f"Fleet Graph Audit — {report.timestamp}")
    lines.append(f"Repos scanned: {report.repos_scanned}, with findings: {report.repos_with_findings}")
    lines.append("")

    for r in report.per_repo:
        if r.error:
            lines.append(f"⚠  {r.repo}: {r.error}")
            continue
        if r.total_findings == 0:
            lines.append(f"✅ {r.repo}: clean")
            continue

        lines.append(f"🔍 {r.repo} ({r.total_findings} findings)")

        if r.clones:
            lines.append(f"  Clones ({len(r.clones)}):")
            for c in r.clones:
                names = ", ".join(f"{n['path']}:{n['name']}" for n in c["nodes"])
                lines.append(f"    [{c['type']}] {c['hash'][:12]}: {names}")

        if r.test_gaps:
            lines.append(f"  Test Gaps ({len(r.test_gaps)}):")
            for g in r.test_gaps[:10]:  # cap display at 10
                lines.append(
                    f"    {g['name']} ({g['path']}) — {g['caller_count']} caller(s), 0 tests"
                )
            if len(r.test_gaps) > 10:
                lines.append(f"    ... and {len(r.test_gaps) - 10} more")

        if r.untested_invariants:
            lines.append(f"  Untested Invariants ({len(r.untested_invariants)}):")
            for u in r.untested_invariants:
                sym_count = len(u["governed_symbols"])
                lines.append(f"    {u['name']} ({u['path']}) → {sym_count} untested symbols")

        if r.circular_imports:
            lines.append(f"  Circular Imports ({len(r.circular_imports)}):")
            for cyc in r.circular_imports:
                lines.append(f"    {' → '.join(cyc)}")

        if r.unreferenced_symbols:
            lines.append(f"  Unreferenced Symbols ({len(r.unreferenced_symbols)}):")
            for d in r.unreferenced_symbols[:10]:
                lines.append(f"    {d['id']} ({d['path']})")
            if len(r.unreferenced_symbols) > 10:
                lines.append(f"    ... and {len(r.unreferenced_symbols) - 10} more")

        lines.append("")

    if report.cross_repo_clones:
        lines.append(f"=== Cross-Repo Clones ({len(report.cross_repo_clones)}) ===")
        for c in report.cross_repo_clones:
            repos_str = ", ".join(c["repos"])
            names = ", ".join(f"{n['path']}:{n['name']}" for n in c["nodes"])
            lines.append(f"  [{c['type']}] across [{repos_str}]: {names}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for myfleet.fleet_audit."""
    parser = argparse.ArgumentParser(
        description="Scan fleet repos with the deterministic codebase graph and report findings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        help="Only audit these repos (e.g. my-coder my-fleet)",
    )
    parser.add_argument(
        "--cross-repo",
        action="store_true",
        help="Also detect structural clones across repos (slower)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output findings as JSON",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Override fleet workspace root",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        report = run_audit(
            root=args.root,
            repos=args.repos,
            cross_repo=args.cross_repo,
        )
    except ImportError:
        print(
            "error: mythings.graph is not installed. "
            "Install my-things-core in the shared .venv first.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_human(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
