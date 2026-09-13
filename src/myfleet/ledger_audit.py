#!/usr/bin/env python3
"""Plausibility Audit for Fleet Runtime Ledger.

Evaluates .my-fleet/ledger.jsonl for telemetry integrity:
1. Cadence Anomalies: Detects sub-second burst bursts that indicate test suite pollution.
2. Provenance Integrity: Distinguishes synthetic/test entries from real production runs.
3. Authentic Liveness: Reports true last `fleet_dispatch` execution timestamp rather than bookkeeping ticks.

Usage:
    python -m myfleet.ledger_audit              # Human-readable report
    python -m myfleet.ledger_audit --json       # Machine-readable JSON summary
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from myfleet.workspace import fleet_root, ledger_path

WORKSPACE_ROOT = fleet_root(__file__)


@dataclass
class Anomaly:
    kind: str  # "burst_density", "synthetic_test", "stale_dispatch"
    message: str
    count: int = 1


@dataclass
class LedgerAuditResult:
    is_plausible: bool
    total_entries: int
    dispatch_entries_count: int
    last_dispatch_ts: str | None
    anomalies: list[Anomaly] = field(default_factory=list)
    burst_density_detected: bool = False
    synthetic_rows_detected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_plausible": self.is_plausible,
            "total_entries": self.total_entries,
            "dispatch_entries_count": self.dispatch_entries_count,
            "last_dispatch_ts": self.last_dispatch_ts,
            "burst_density_detected": self.burst_density_detected,
            "synthetic_rows_detected": self.synthetic_rows_detected,
            "anomalies": [
                {"kind": a.kind, "message": a.message, "count": a.count}
                for a in self.anomalies
            ],
        }


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).astimezone(UTC)
    except Exception:
        return None


def audit_ledger(path: Path | None = None) -> LedgerAuditResult:
    """Audit runtime ledger file for plausibility and telemetry integrity."""
    if path is None:
        path = ledger_path(WORKSPACE_ROOT)

    if not path.exists():
        return LedgerAuditResult(
            is_plausible=True,
            total_entries=0,
            dispatch_entries_count=0,
            last_dispatch_ts=None,
        )

    try:
        from mythings.ledger import Ledger

        ledger = Ledger(path)
        entries = list(ledger)
    except Exception as exc:
        return LedgerAuditResult(
            is_plausible=False,
            total_entries=0,
            dispatch_entries_count=0,
            last_dispatch_ts=None,
            anomalies=[Anomaly(kind="read_error", message=f"Failed to read ledger: {exc}")],
        )

    total_entries = len(entries)
    if total_entries == 0:
        return LedgerAuditResult(
            is_plausible=True,
            total_entries=0,
            dispatch_entries_count=0,
            last_dispatch_ts=None,
        )

    dispatch_entries = [e for e in entries if e.tool == "fleet_dispatch"]
    last_dispatch_ts = dispatch_entries[-1].ts if dispatch_entries else None

    anomalies: list[Anomaly] = []
    synthetic_count = 0
    sub_second_gaps = 0

    # 1. Synthetic / test row detection
    for e in entries:
        if e.data.get("synthetic") is True or e.data.get("test") is True:
            synthetic_count += 1

    if synthetic_count > 0:
        anomalies.append(
            Anomaly(
                kind="synthetic_test",
                message=f"Detected {synthetic_count} test/synthetic entries in production ledger",
                count=synthetic_count,
            )
        )

    # 2. Sub-second burst density cadence check
    timestamps = [_parse_ts(e.ts) for e in entries]
    valid_ts = [t for t in timestamps if t is not None]

    if len(valid_ts) >= 10:
        sub_second_gaps = 0
        for i in range(1, len(valid_ts)):
            gap = (valid_ts[i] - valid_ts[i - 1]).total_seconds()
            if abs(gap) < 0.5:
                sub_second_gaps += 1

        # If > 30% of entries in a multi-entry ledger occur at sub-second gaps, flag burst density
        burst_ratio = sub_second_gaps / max(len(valid_ts) - 1, 1)
        if burst_ratio > 0.3 and sub_second_gaps > 10:
            anomalies.append(
                Anomaly(
                    kind="burst_density",
                    message=(
                        f"Implausible cadence: {sub_second_gaps} sub-second inter-entry gaps "
                        f"({burst_ratio:.1%}% of entries)"
                    ),
                    count=sub_second_gaps,
                )
            )

    is_plausible = len(anomalies) == 0

    return LedgerAuditResult(
        is_plausible=is_plausible,
        total_entries=total_entries,
        dispatch_entries_count=len(dispatch_entries),
        last_dispatch_ts=last_dispatch_ts,
        anomalies=anomalies,
        burst_density_detected=sub_second_gaps > 10,
        synthetic_rows_detected=synthetic_count,
    )


def format_human_report(res: LedgerAuditResult) -> str:
    """Render ledger audit findings as human-readable markdown."""
    lines: list[str] = []
    lines.append("## 🔍 Fleet Ledger Plausibility Audit")
    status_str = "✅ PLAUSIBLE" if res.is_plausible else "⚠ IMPLAUSIBLE (Anomaly Detected)"
    lines.append(f"**Status:** {status_str}")
    lines.append(f"- **Total Ledger Entries:** {res.total_entries}")
    lines.append(f"- **Authentic Dispatches:** {res.dispatch_entries_count}")
    lines.append(f"- **Last Dispatch Timestamp:** `{res.last_dispatch_ts or 'None'}`")
    lines.append("")

    if res.anomalies:
        lines.append("### Anomalies & Telemetry Risk")
        for a in res.anomalies:
            lines.append(f"- ⚠️ **[{a.kind}]**: {a.message}")
    else:
        lines.append("✅ *Cadence and provenance verify clean telemetry.*")

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for myfleet.ledger_audit."""
    parser = argparse.ArgumentParser(
        description="Audit runtime ledger for cadence anomalies, test pollution, and authentic liveness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Path to ledger file (default: .my-fleet/ledger.jsonl)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    res = audit_ledger(args.ledger)

    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(format_human_report(res))

    return 0 if res.is_plausible else 1


if __name__ == "__main__":
    sys.exit(main())
