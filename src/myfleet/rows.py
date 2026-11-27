from __future__ import annotations

from dataclasses import replace
from typing import Any

from mythings.ledger import Ledger, LedgerEntry, canonical

# my-fleet's rows on the lab's one ledger. A row's tool is the repository's name, the component
# that wrote it (fleet_dispatch, fleet_cycle, ...) is data, and a word core's legacy map has no
# kind for keeps its old word as the type. Rows written before the move name the component as
# their tool and use the old outcome words; read() still returns them, in core's outcome words.
TOOL = "my-fleet"
KIND_OF = {"cycle": "dispatch", "heartbeat_alert": "alert"}
OUTCOMES = {
    "ok": "success",
    "recovered": "success",
    "failed": "failure",
    "stage_failed": "failure",
    "merge_failed": "failure",
    "stale": "failure",
    "config_missing": "failure",
    "auth_expired": "failure",
    "probe_failed": "failure",
    "workspace_untrusted": "failure",
    "deferred": "paused",
    "denied": "deny",
    "halted_critical": "halted",
    "disarmed": "halted",
    "rearmed": "started",
    "backlog_empty": "empty",
    "no_dispatchable_candidates": "empty",
    "human_approved": "accepted",
    "human_approved_and_merged": "merged",
    "filed": "open",
    "already-open": "open",
}


def record(
    ledger: Ledger, component: str, word: str, outcome: str, detail: str = "", **data: Any
) -> LedgerEntry:
    core = OUTCOMES.get(outcome, outcome)
    if core != outcome:
        data["outcome_was"] = outcome
    return ledger.record(
        tool=TOOL,
        kind=KIND_OF.get(word) or canonical(word),
        outcome=core,
        detail=detail,
        type=word,
        component=component,
        **data,
    )


def component_of(entry: LedgerEntry) -> str:
    return entry.data.get("component", "") if entry.tool == TOOL else entry.tool


def word_of(entry: LedgerEntry) -> str:
    # The outcome the writer said, before it was named in core's words.
    return entry.data.get("outcome_was", entry.outcome)


def _is(entry: LedgerEntry, word: str) -> bool:
    kind = KIND_OF.get(word) or canonical(word)
    return entry.kind == word or (entry.data.get("type") == word and entry.kind == kind)


def _core(entry: LedgerEntry) -> LedgerEntry:
    core = OUTCOMES.get(entry.outcome, entry.outcome)
    if core == entry.outcome:
        return entry
    return replace(entry, outcome=core, data={"outcome_was": entry.outcome, **entry.data})


def read(ledger: Ledger, component: str, word: str) -> list[LedgerEntry]:
    return [_core(e) for e in ledger if component_of(e) == component and _is(e, word)]
