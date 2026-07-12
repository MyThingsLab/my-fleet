"""Parse a headless `claude -p --output-format stream-json` transcript into a
per-run usage report, attributing each permission denial to the turn and the
reasoning text that led to it.

Not a fleet `My[X]` tool — a sibling module to fleet_dispatch.py, same
workspace-tooling status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# A denied Bash command whose base command already belongs to one of these
# families is safe to auto-approve a wider phrasing for (fleet_dispatch.py
# widens its own allowlist to the family's full pattern set). Anything else
# stays a friction signal for a human to look at — no auto-widen.
#
# The read-only families (ls/cat/head/tail/wc/grep) are pure inspection: they
# don't mutate the tree, so widening them unattended is safe. `find` is
# deliberately NOT here (its -delete/-exec can mutate) and neither is anything
# that runs code (python -c), installs (pip), or removes (rm) — those stay
# friction. Workers are told to prefer their native Read/Glob/Grep tools
# anyway (see _prompt_for); these entries just keep an insistent shell caller
# from dead-ending on a denial instead of getting the work done.
SAFE_FAMILY_PATTERNS: dict[str, list[str]] = {
    "pytest": ["Bash(pytest*)", "Bash(python -m pytest*)", "Bash(python3 -m pytest*)"],
    "ruff": ["Bash(ruff*)", "Bash(python -m ruff*)", "Bash(python3 -m ruff*)"],
    "git": ["Bash(git *)"],
    "gh": ["Bash(gh issue view*)", "Bash(gh pr create*)"],
    "ls": ["Bash(ls*)"],
    "cat": ["Bash(cat*)"],
    "head": ["Bash(head*)"],
    "tail": ["Bash(tail*)"],
    "wc": ["Bash(wc*)"],
    "grep": ["Bash(grep*)"],
}

_READ_ONLY_FAMILIES = ("ls", "cat", "head", "tail", "wc", "grep")


def family_for(command: str) -> str | None:
    stripped = command.strip()
    # rtk rewrites `<cmd>` -> `rtk <cmd>`; classify by the underlying command so
    # a denied `rtk git ...` / `rtk ls ...` maps to the same family as the bare
    # form (the old startswith("git ") check missed the rtk-prefixed variants).
    if stripped.startswith("rtk "):
        stripped = stripped[len("rtk ") :].lstrip()
    if "pytest" in command:
        return "pytest"
    if "ruff" in command:
        return "ruff"
    first = stripped.split(maxsplit=1)[0] if stripped else ""
    if first == "git":
        return "git"
    if first == "gh":
        return "gh"
    if first in _READ_ONLY_FAMILIES:
        return first
    return None


@dataclass(frozen=True)
class Denial:
    tool_name: str
    command: str
    turn: int  # -1 if the originating turn couldn't be matched
    preceding_reasoning: str  # the assistant's text right before the denied tool_use
    wasted_output_tokens: int  # output tokens spent generating the denied turn


@dataclass(frozen=True)
class UsageReport:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    num_turns: int = 0
    denials: list[Denial] = field(default_factory=list)
    # The headless session's final `result` string. When a worker gives up
    # without doing the work (e.g. "please approve `ls` so I can continue"),
    # this is the only record of *why*, so fleet_dispatch surfaces it on a
    # no-changes outcome instead of silently calling the run a success.
    final_message: str = ""

    @property
    def wasted_output_tokens(self) -> int:
        return sum(d.wasted_output_tokens for d in self.denials)


def parse_transcript(lines: list[str]) -> UsageReport:
    tool_use_index: dict[str, dict] = {}
    turn = 0
    result_obj: dict | None = None

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "assistant":
            turn += 1
            message = obj.get("message", {})
            output_tokens = message.get("usage", {}).get("output_tokens", 0)
            content = message.get("content", [])
            preceding_text = " ".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            ).strip()
            for block in content:
                if block.get("type") == "tool_use":
                    tool_use_index[block["id"]] = {
                        "turn": turn,
                        "preceding_reasoning": preceding_text,
                        "output_tokens": output_tokens,
                    }
        elif obj.get("type") == "result":
            result_obj = obj

    if result_obj is None:
        return UsageReport(num_turns=turn)

    usage = result_obj.get("usage", {})
    denials = []
    for d in result_obj.get("permission_denials", []):
        info = tool_use_index.get(d.get("tool_use_id", ""))
        tool_input = d.get("tool_input", {})
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)
        denials.append(
            Denial(
                tool_name=d.get("tool_name", ""),
                command=command,
                turn=info["turn"] if info else -1,
                preceding_reasoning=(info["preceding_reasoning"][:200] if info else ""),
                wasted_output_tokens=info["output_tokens"] if info else 0,
            )
        )

    return UsageReport(
        cost_usd=result_obj.get("total_cost_usd", 0.0),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        num_turns=result_obj.get("num_turns", turn),
        denials=denials,
        final_message=str(result_obj.get("result", "")),
    )
