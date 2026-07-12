#!/usr/bin/env python3
"""Run the learn-loop across several courses at once and rank what to study first.

`study_cycle.py` runs one pass for one course (one `--workdir`). Courses are
already isolated from each other today since every path (topics, decks, the
mastery ledger) lives under that course's own `<workdir>/.mythings/` — running
two courses from two workdirs never collides. What's missing when several
exams are being crammed in parallel is orchestration: a registered list of
active courses, one command to pass all of them, and a single ranked view of
what's most urgent across all of them (not just weakest-per-course, but
weakest-adjusted-for-how-soon-that-course's-exam-is).

Courses are declared in a small local TOML registry, e.g.:

    [[course]]
    name = "unsupervised-learning"
    workdir = "~/study/ul"
    corpus = ["~/study/ul/notes.pdf", "~/study/ul/slides.pdf"]
    program = "~/study/ul/syllabus.pdf"   # optional
    exam_date = "2026-07-28"              # optional, YYYY-MM-DD

Each course gets its own `study_cycle.main()` pass (same dry-run/--execute
gating, same per-topic Engine billing). The cross-course ranking is computed
directly from each course's mastery ledger afterwards — no schema change to
`mythings.mastery`, no new ledger format.
"""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from mythings.mastery import Mastery, due, load, rollup

import myfleet.study_cycle as study_cycle


@dataclass(frozen=True)
class Course:
    name: str
    workdir: Path
    corpus: tuple[Path, ...]
    program: Path | None = None
    exam_date: str | None = None  # ISO date, YYYY-MM-DD


@dataclass(frozen=True)
class Standing:
    course: str
    topic: str
    score: float
    next_due: str | None
    exam_date: str | None
    priority: float


def load_courses(path: str | Path) -> list[Course]:
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[Course] = []
    for row in data.get("course", []):
        name = str(row["name"])
        workdir = Path(str(row["workdir"])).expanduser()
        corpus = tuple(Path(str(p)).expanduser() for p in row["corpus"])
        program = Path(str(row["program"])).expanduser() if row.get("program") else None
        exam_date = str(row["exam_date"]) if row.get("exam_date") else None
        out.append(Course(name, workdir, corpus, program, exam_date))
    return out


def days_until(exam_date: str | None, now: datetime | None = None) -> float | None:
    if exam_date is None:
        return None
    now = now or datetime.now(UTC)
    target = datetime.combine(date.fromisoformat(exam_date), datetime.min.time(), tzinfo=UTC)
    return (target - now).total_seconds() / 86400.0


def course_priority(score: float, days_left: float | None, *, horizon_days: float = 30.0) -> float:
    # Weakest-first is the base signal (same as myprofessor due). An exam date
    # sharpens it: a course whose exam is close gets its score scaled down so
    # it sorts ahead of an equally-weak topic in a course that's weeks out.
    # Clamped so an overdue exam date can't invert the ranking, only compress it.
    if days_left is None:
        return score
    factor = max(min(days_left / horizon_days, 1.0), 0.05)
    return score * factor


def _ledger_path(course: Course) -> Path:
    return course.workdir / ".mythings" / "mastery.jsonl"


def build_report(
    courses: list[Course], *, now: datetime | None = None, horizon_days: float = 30.0
) -> list[Standing]:
    standings: list[Standing] = []
    for course in courses:
        masteries: list[Mastery] = rollup(load(_ledger_path(course)), now=now)
        for m in due(masteries, now=now):
            standings.append(
                Standing(
                    course=course.name,
                    topic=m.topic,
                    score=m.score,
                    next_due=m.next_due,
                    exam_date=course.exam_date,
                    priority=course_priority(
                        m.score, days_until(course.exam_date, now), horizon_days=horizon_days
                    ),
                )
            )
    standings.sort(key=lambda s: (s.priority, s.exam_date or "9999-99-99", s.course, s.topic))
    return standings


def _render_report(standings: list[Standing]) -> str:
    if not standings:
        return "nothing due across any registered course"
    header = f"{'course':<24} {'topic':<24} {'score':>5}  exam-date   next-due"
    lines = [header, "-" * len(header)]
    for s in standings:
        lines.append(
            f"{s.course[:24]:<24} {s.topic[:24]:<24} {s.score:5.2f}  "
            f"{(s.exam_date or ''):<10}  {(s.next_due or '')[:10]}"
        )
    return "\n".join(lines)


def _course_argv(course: Course, args: argparse.Namespace) -> list[str]:
    argv = ["--workdir", str(course.workdir)]
    for path in course.corpus:
        argv += ["--corpus", str(path)]
    if course.program is not None:
        argv += ["--program", str(course.program)]
    argv += [
        "--engine",
        args.engine,
        "--topics-per-cycle",
        str(args.topics_per_cycle),
        "--questions",
        str(args.questions),
        "--cards",
        str(args.cards),
        "--max-topics",
        str(args.max_topics),
    ]
    if args.execute:
        argv.append("--execute")
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--courses",
        type=Path,
        required=True,
        help="course registry TOML (see module docstring for the shape)",
    )
    parser.add_argument("--engine", choices=["noop", "claude"], default="noop")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the billed Engine steps for real (otherwise dry run)",
    )
    parser.add_argument("--topics-per-cycle", type=int, default=3)
    parser.add_argument("--questions", type=int, default=3)
    parser.add_argument("--cards", type=int, default=8)
    parser.add_argument("--max-topics", type=int, default=40)
    parser.add_argument(
        "--horizon-days",
        type=float,
        default=30.0,
        help="days-to-exam beyond which urgency no longer sharpens the ranking",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="cap the combined standing report to this many rows"
    )
    args = parser.parse_args(argv)

    courses = load_courses(args.courses)
    if not courses:
        print(f"(no courses registered in {args.courses})")
        return 0

    worst = 0
    for course in courses:
        print(f"\n=== {course.name} ===")
        rc = study_cycle.main(_course_argv(course, args))
        worst = worst or rc

    print("\n=== combined standing (all courses, weakest / most urgent first) ===")
    report = build_report(courses, horizon_days=args.horizon_days)
    if args.limit is not None:
        report = report[: args.limit]
    print(_render_report(report))

    if not args.execute:
        print(
            "\n(dry run — pass --execute to run each course's decompose/build/quiz steps for real)"
        )
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
