# The local lane's coder: a small local model (qwen3.5:4b) adopting core's functions in place of
# a tool's copies. The code graph (mythings.gaps) finds the pairs: a tool function with a core
# function's syntax-tree shape ("copy"), or functions copied across tools that core has a
# namesake of ("promote", such as ten tools' _parse_json_object and core's
# engine.parse_json_object). Whether one can stand for the other is a judgement, so the model
# makes it, and only it: the prompt holds both functions whole and every line of the tool that
# calls its copy, a few hundred tokens, and the reply is one typed verdict. The edit that follows
# a yes is code (delete the copy, import core's under the same local name), and the repository's
# own suite decides whether it stands (myfleet.local_lane). A small model is reliable at a
# narrow question with everything in front of it; it is not yet at writing the edit.
from __future__ import annotations

import ast
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mythings import gaps, steward
from mythings.engine import (
    CheckedEngine,
    Engine,
    EngineRequest,
    OllamaEngine,
    TracingEngine,
    approx_tokens,
    num_ctx,
    parse_json_object,
)
from mythings.ledger import Ledger

from myfleet.local_lane import Task

MODEL = "qwen3.5:4b"
USES = 6  # functions shown that use the copy
USE_LINES = 40
HELPERS = 6
REPLY_TOKENS = 1024
SYSTEM = (
    "You review Python code. You answer only from the code shown, and you say no when the code "
    "shown does not settle the question."
)
# The instruction every question ends with; myoptimizer.lane_adopt tunes it on the lane's
# labelled questions, so it is one string with the two names as placeholders.
QUESTION = (
    "Could `{local}` be deleted and `{target}` imported in its place, under the name `{local}`, "
    "with every use above still correct? It must take the same arguments, and for each use give "
    "what the caller relies on: an extra input the library accepts is fine, one the caller "
    "relies on that it rejects is not. If the two return different types (a list and a set, "
    "say), the answer is yes only if every caller shown would behave the same, including the "
    "order and repeats of items. Give the reason first, naming the input where they differ if "
    "they do."
)
VERDICT = {
    "type": "object",
    "required": ["reason", "replaceable"],
    "properties": {
        "reason": {"type": "string"},
        "replaceable": {"type": "boolean"},
    },
}


@dataclass(frozen=True)
class Candidate:
    repo: str
    symbol: str  # the tool's function, dotted: mytool.module._parse_json_object
    target: str  # core's, dotted: mythings.engine.parse_json_object

    @property
    def module(self) -> str:
        return self.symbol.rsplit(".", 1)[0]

    @property
    def local(self) -> str:
        return self.symbol.rsplit(".", 1)[1]


def _target(gap: gaps.Gap) -> str | None:
    # The public core function a gap says to adopt, or None when it names none.
    if gap.kind == "copy" and not gap.what.rsplit(".", 1)[1].startswith("_"):
        return gap.what
    if gap.kind == "promote" and gap.fix.startswith("core has "):
        return gap.fix.removeprefix("core has ").split(":", 1)[0]
    return None


def candidates(found: Sequence[gaps.Gap]) -> dict[str, list[Candidate]]:
    by_repo: dict[str, list[Candidate]] = defaultdict(list)
    for gap in found:
        if (target := _target(gap)) is None:
            continue
        for where in gap.where:
            repo, symbol = where.split(":", 1)
            by_repo[repo].append(Candidate(repo, symbol, target))
    return dict(sorted(by_repo.items()))


def module_file(root: Path, module: str) -> Path | None:
    base = root / "src" / Path(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def top_level_function(source: str, name: str) -> ast.FunctionDef | None:
    # Only a module-level def: a method's copy is bound to its class, and the edit cannot import it.
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _segment(source: str, node: ast.FunctionDef) -> str:
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    return "".join(source.splitlines(keepends=True)[start - 1 : node.end_lineno]).rstrip()


def helpers(source: str, node: ast.FunctionDef) -> list[str]:
    # The module-level definitions a function uses (`_strip_code_fence`, `_TOKEN_RE`), whole: a
    # verdict on a body that calls something it cannot see is a guess.
    tree = ast.parse(source)
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - {node.name}
    found = []
    for stmt in tree.body:
        names = _defined(stmt)
        if names & used:
            found.append(ast.get_source_segment(source, stmt) or "")
    return found[:HELPERS]


def _defined(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    targets = stmt.targets if isinstance(stmt, ast.Assign) else []
    if isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def _enclosing(tree: ast.Module, line: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node  # the innermost function around the use
    return best


def uses(tree: Path, local: str, limit: int = USES) -> list[str]:
    # Every function of the tool that uses the copy, whole (a call, or the name passed as a value),
    # so the model sees what each caller does with the result: whether it relies on its order.
    shown: list[str] = []
    for path in sorted([*tree.glob("src/**/*.py"), *tree.glob("tests/**/*.py")]):
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source)
        seen: set[int] = set()
        names = [n for n in ast.walk(module) if isinstance(n, ast.Name) and n.id == local]
        for name in sorted(names, key=lambda n: (n.lineno, n.col_offset)):
            around = _enclosing(module, name.lineno)
            if around is None or around.name == local or id(around) in seen:
                if around is None:
                    line = source.splitlines()[name.lineno - 1].strip()
                    shown.append(f"{path.relative_to(tree)}:{name.lineno}: {line}")
                continue
            seen.add(id(around))
            body = ast.get_source_segment(source, around) or ""
            if len(body.splitlines()) > USE_LINES:
                body = "\n".join(body.splitlines()[:USE_LINES] + ["    ..."])
            shown.append(f"{path.relative_to(tree)}:{around.lineno}:\n{body}")
    return shown[:limit]


@dataclass(frozen=True)
class Pair:
    mine: str
    my_helpers: list[str]
    theirs: str
    their_helpers: list[str]


def prompt(cand: Candidate, pair: Pair, sites: Sequence[str], question: str = QUESTION) -> str:
    listed = "\n\n".join(sites) or "(nothing in this repository uses it)"
    return "\n\n".join(
        [
            f"The tool has its own `{cand.local}`, in {cand.module}:",
            pair.mine,
            "The definitions it uses, from the same module:",
            "\n\n".join(pair.my_helpers) or "(none)",
            f"The shared library has `{cand.target}`:",
            pair.theirs,
            "The definitions it uses, from the same library module:",
            "\n\n".join(pair.their_helpers) or "(none)",
            f"Every place in the tool that uses `{cand.local}`, with the function around it:",
            listed,
            question.replace("{local}", cand.local).replace("{target}", cand.target),
        ]
    )


def judge(engine: Engine, text: str, system: str = SYSTEM) -> tuple[bool, str]:
    request = EngineRequest(
        prompt=text, system=system, schema=VERDICT, context={"tool": "local_lane", "task": "adopt"}
    )
    result = engine.run(request)
    reply = parse_json_object(result.text)
    if reply is None or "replaceable" not in reply:
        return False, f"no verdict: {result.data.get('schema_problem') or result.text[:80]!r}"
    return bool(reply.get("replaceable")), str(reply.get("reason", ""))


def adopt(source: str, node: ast.FunctionDef, local: str, target: str) -> str:
    # The module with its copy deleted and core's function imported under the copy's name, after
    # the module's last top-level import. Unused imports the copy leaves are the lane's to tidy.
    lines = source.splitlines(keepends=True)
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end = node.end_lineno or node.lineno
    while end < len(lines) and not lines[end].strip():
        end += 1  # the blank lines after it, so the spacing around the gap stays two lines
    del lines[start - 1 : end]
    module, name = target.rsplit(".", 1)
    line = f"from {module} import {name}" + ("" if name == local else f" as {local}")
    imports = [s for s in ast.parse(source).body if isinstance(s, (ast.Import, ast.ImportFrom))]
    at = max((s.end_lineno or s.lineno for s in imports), default=0)
    lines.insert(at, line + "\n")
    return "".join(lines).rstrip("\n") + "\n"


TUNED = (
    Path.home() / ".local" / "share" / "mythingslab" / "optimizer" / "lane" / "adopt-prompt.json"
)


def tuned(path: Path = TUNED) -> tuple[str, str]:
    # The system text and instruction myoptimizer.lane_adopt published, when it found a better
    # pair (higher on held-out questions, no more wrong yes); the built-in ones otherwise. An
    # instruction that lost a placeholder would ask about no function at all, so it is refused.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SYSTEM, QUESTION
    system, question = data.get("system"), data.get("instruction")
    if not (isinstance(system, str) and isinstance(question, str)):
        return SYSTEM, QUESTION
    if "{local}" not in question or "{target}" not in question:
        return SYSTEM, QUESTION
    return system, question


@dataclass
class Coder:
    engine: Engine
    core: Path
    decided: list[tuple[Candidate, bool, str]]
    system: str = SYSTEM
    question: str = QUESTION

    def apply(self, tree: Path, cands: Sequence[Candidate]) -> list[str]:
        changed = []
        for cand in cands:
            if (path := self._adopt_one(tree, cand)) is not None:
                changed.append(str(path.relative_to(tree)))
        return sorted(set(changed))

    def _adopt_one(self, tree: Path, cand: Candidate) -> Path | None:
        path = module_file(tree, cand.module)
        core_path = module_file(self.core, cand.target.rsplit(".", 1)[0])
        if path is None or core_path is None:
            return None
        source = path.read_text(encoding="utf-8")
        core_source = core_path.read_text(encoding="utf-8")
        mine = top_level_function(source, cand.local)
        theirs = top_level_function(core_source, cand.target.rsplit(".", 1)[1])
        if mine is None or theirs is None:
            return None
        pair = Pair(
            _segment(source, mine),
            helpers(source, mine),
            _segment(core_source, theirs),
            helpers(core_source, theirs),
        )
        text = prompt(cand, pair, uses(tree, cand.local), self.question)
        if approx_tokens(self.system + text) > num_ctx() - REPLY_TOKENS:
            self.decided.append((cand, False, "the question does not fit the model's context"))
            return None
        yes, reason = judge(self.engine, text, self.system)
        self.decided.append((cand, yes, reason))
        if not yes:
            return None
        path.write_text(adopt(source, mine, cand.local, cand.target), encoding="utf-8")
        return path


def task(coder: Coder, cands: Sequence[Candidate]) -> Task:
    def how() -> str:
        adopted = sorted(
            {f"{c.local} -> {c.target}" for c, yes, _ in coder.decided if yes and c in cands}
        )
        return f"Judged interchangeable by {MODEL} from both bodies and every call: " + (
            ", ".join(adopted) + "."
        )

    return Task(
        name="adopt-core",
        subject="Use core's functions in place of this tool's copies",
        how=how,
        apply=lambda tree: coder.apply(tree, cands),
    )


def engine(traces: Path) -> Engine:
    # Temperature 0 and a fixed seed, so a verdict is reproducible; every prompt and reply is kept
    # (traces are private: a path outside every repository) for my-optimizer to learn from.
    model = OllamaEngine(
        model=MODEL,
        options={"temperature": 0, "seed": 0},
        busy=steward.busy("the local lane's coder"),
    )
    return TracingEngine(CheckedEngine(model), Ledger(traces), tool="local_lane")
