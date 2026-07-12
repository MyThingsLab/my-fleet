#!/usr/bin/env python3
"""Run one pass of the learn-loop by chaining the study tools' own CLIs.

The build-loop counterpart of `fleet_cycle.py`, on the same `cycle_driver`
machinery. One pass:

  1. mysyllabus decompose  - a course program -> an ordered topic list (once, if
                              --program is given and no topic list exists yet).
  2. (select)              - pick the topics to study this pass from that list,
                              weakest / never-seen first, from the mastery ledger.
  3. myflashcards build    - per selected topic, a fresh deck from the corpus.
  4. myprofessor quiz      - per selected topic, cited questions from the corpus.
  5. myprofessor due       - the mastery standing: what to study next.

Like fleet_cycle, no tool calls another's CLI directly — this script is the
external driver that chains them. Reads and the `due` report always run; the
billed Engine steps (decompose / build / quiz) run only under --execute, and
otherwise print what they would do.

The topic list (`topics.toml`), decks, and the mastery ledger are local files —
the learn-loop is a personal cram loop, not an issue/PR pipeline (see
my-professor / my-flashcards CLAUDE.md).
"""

from __future__ import annotations

import argparse
import tomllib
from datetime import datetime
from pathlib import Path

from mythings.mastery import due as mastery_due
from mythings.mastery import load, rollup

from myfleet.cycle_driver import Stage, run_cycle


def load_topics(path: Path) -> list[tuple[str, str]]:
    # (slug, title) pairs from a mysyllabus topics.toml, order preserved.
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for row in data.get("topic", []):
        slug = str(row.get("slug", "")).strip()
        title = str(row.get("title", "")).strip() or slug
        if slug:
            out.append((slug, title))
    return out


def select_topics(
    topics: list[tuple[str, str]],
    ledger_path: Path,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    # Order by mastery: topics never studied come first, then those that are due,
    # weakest first — the same weakest-first signal my-professor/my-flashcards use.
    masteries = {m.topic: m for m in rollup(load(ledger_path), now=now)}
    due_slugs = {m.topic for m in mastery_due(list(masteries.values()), now=now)}

    def rank(pair: tuple[str, str]) -> tuple[int, float]:
        m = masteries.get(pair[0])
        return (0, 0.0) if m is None else (1, m.score)

    ranked = sorted(topics, key=rank)
    # Never-seen and due topics come first; then top up to `limit` with the
    # next-weakest so a "drill N topics" request delivers N when they exist,
    # rather than under-filling a session just because nothing else is due yet.
    priority = [p for p in ranked if p[0] not in masteries or p[0] in due_slugs]
    rest = [p for p in ranked if p not in priority]
    return (priority + rest)[:limit]


def build_stages(args: argparse.Namespace) -> list[Stage]:
    topics_file = args.topics_file
    stages: list[Stage] = []

    # 1. Decompose the program into a topic list, if asked and not already done.
    if args.program:
        program_args = ["--program", str(args.program)]
        stages.append(
            Stage(
                name="mysyllabus decompose",
                argv=[
                    "mysyllabus", "decompose", *program_args,
                    "--engine", args.engine, "--out", str(topics_file),
                    "--max-topics", str(args.max_topics),
                ],
                skip=None if not topics_file.exists() else "topic list already exists",
            )
        )

    # 2. Select the topics to study this pass.
    topics = load_topics(topics_file)
    selected = select_topics(topics, args.ledger, limit=args.topics_per_cycle)

    if not topics:
        note = (
            "run --program … --execute first to build one"
            if args.program
            else f"provide --topics-file (none at {topics_file})"
        )
        print(f"(no topics to study yet — {note})")

    # 3-4. Per selected topic: a fresh deck and a cited quiz from the corpus.
    corpus_args: list[str] = []
    for path in args.corpus:
        corpus_args += ["--corpus", str(path)]
    for slug, title in selected:
        stages.append(
            Stage(
                name=f"myflashcards build [{slug}]",
                argv=[
                    "myflashcards", "build", title, *corpus_args,
                    "--deck", str(args.deck_dir / f"{slug}.toml"),
                    "--count", str(args.cards), "--engine", args.engine,
                ],
            )
        )
        stages.append(
            Stage(
                name=f"myprofessor quiz [{slug}]",
                argv=[
                    "myprofessor", "quiz", title, *corpus_args,
                    "--questions", str(args.questions), "--engine", args.engine,
                ],
            )
        )

    # 5. The mastery standing — always runs (read-only, free).
    stages.append(
        Stage(
            name="myprofessor due",
            argv=["myprofessor", "due", "--all", "--ledger", str(args.ledger)],
            mutating=False,
        )
    )
    return stages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, action="append", required=True,
                        help="study material for grounding (repeatable): PDFs, notes, slides")
    parser.add_argument("--program", type=Path, default=None,
                        help="course program / syllabus to decompose into topics (optional)")
    parser.add_argument("--workdir", type=Path, default=Path.cwd(),
                        help="where the local topics/decks/ledger live (default: cwd)")
    parser.add_argument("--topics-file", type=Path, default=None,
                        help="topic list TOML (default: <workdir>/.mythings/topics.toml)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="mastery ledger (default: <workdir>/.mythings/mastery.jsonl)")
    parser.add_argument("--deck-dir", type=Path, default=None,
                        help="where flashcard decks are written (default: <workdir>/.mythings/decks)")
    parser.add_argument("--engine", choices=["noop", "claude"], default="noop",
                        help="Engine backend for the billed steps (decompose/build/quiz)")
    parser.add_argument("--execute", action="store_true",
                        help="run the billed Engine steps for real (otherwise dry run)")
    parser.add_argument("--topics-per-cycle", type=int, default=3,
                        help="how many topics to drill this pass (weakest/never-seen first)")
    parser.add_argument("--questions", type=int, default=3, help="questions per quiz")
    parser.add_argument("--cards", type=int, default=8, help="flashcards per deck")
    parser.add_argument("--max-topics", type=int, default=40, help="cap on decomposed topics")
    args = parser.parse_args(argv)

    workdir = args.workdir.resolve()
    mythings_dir = workdir / ".mythings"
    args.topics_file = args.topics_file or mythings_dir / "topics.toml"
    args.ledger = args.ledger or mythings_dir / "mastery.jsonl"
    args.deck_dir = args.deck_dir or mythings_dir / "decks"

    stages = build_stages(args)
    rc = run_cycle(stages, execute=args.execute, cwd=workdir)

    if not args.execute:
        print("\n(dry run — pass --execute to run the decompose/build/quiz steps for real)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
