from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mythings.mastery import Attempt, now_iso, record

import myfleet.study_all as sa

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _courses_toml(tmp_path: Path, *rows: dict) -> Path:
    body = "\n\n".join(
        "[[course]]\n"
        + "\n".join(
            f"{k} = {v!r}" if k != "corpus" else f"{k} = {v!r}".replace("'", '"')
            for k, v in row.items()
        )
        for row in rows
    )
    f = tmp_path / "courses.toml"
    f.write_text(body + "\n", encoding="utf-8")
    return f


def test_load_courses_reads_all_fields(tmp_path: Path) -> None:
    f = _courses_toml(
        tmp_path,
        {
            "name": "ul",
            "workdir": str(tmp_path / "ul"),
            "corpus": [str(tmp_path / "notes.pdf")],
            "program": str(tmp_path / "syllabus.pdf"),
            "exam_date": "2026-07-28",
        },
    )
    courses = sa.load_courses(f)
    assert len(courses) == 1
    c = courses[0]
    assert c.name == "ul"
    assert c.workdir == tmp_path / "ul"
    assert c.corpus == (tmp_path / "notes.pdf",)
    assert c.program == tmp_path / "syllabus.pdf"
    assert c.exam_date == "2026-07-28"


def test_load_courses_program_and_exam_date_optional(tmp_path: Path) -> None:
    f = _courses_toml(
        tmp_path, {"name": "algo", "workdir": str(tmp_path / "algo"), "corpus": ["notes.pdf"]}
    )
    c = sa.load_courses(f)[0]
    assert c.program is None
    assert c.exam_date is None


def test_days_until_none_without_exam_date() -> None:
    assert sa.days_until(None, now=_NOW) is None


def test_days_until_counts_forward() -> None:
    days = sa.days_until("2026-07-21", now=_NOW)
    assert days is not None
    assert 9.4 < days < 9.6


def test_course_priority_no_exam_date_is_plain_score() -> None:
    assert sa.course_priority(0.4, None) == 0.4


def test_course_priority_scales_down_as_exam_nears() -> None:
    near = sa.course_priority(0.5, days_left=2.0, horizon_days=30.0)
    far = sa.course_priority(0.5, days_left=29.0, horizon_days=30.0)
    assert near < far < 0.5 + 1e-9


def test_course_priority_clamped_never_inverts_ranking() -> None:
    overdue = sa.course_priority(0.5, days_left=-5.0, horizon_days=30.0)
    assert overdue == 0.5 * 0.05


def test_build_report_ranks_nearer_exam_ahead_of_farther_equal_score(tmp_path: Path) -> None:
    near = sa.Course("near-exam", tmp_path / "near", (tmp_path / "n.pdf",), None, "2026-07-14")
    far = sa.Course("far-exam", tmp_path / "far", (tmp_path / "f.pdf",), None, "2026-08-20")
    for course in (near, far):
        ledger = course.workdir / ".mythings" / "mastery.jsonl"
        record(ledger, Attempt("topic-a", now_iso(_NOW - timedelta(days=2)), 0.3, "quiz"))

    report = sa.build_report([near, far], now=_NOW)
    assert [s.course for s in report] == ["near-exam", "far-exam"]


def test_build_report_no_courses_due_is_empty(tmp_path: Path) -> None:
    course = sa.Course("empty", tmp_path / "empty", (tmp_path / "n.pdf",))
    assert sa.build_report([course], now=_NOW) == []


def test_render_report_empty_message() -> None:
    assert "nothing due" in sa._render_report([])


def test_render_report_includes_course_and_topic() -> None:
    standing = sa.Standing("ul", "em-algorithm", 0.2, "2026-07-12", "2026-07-28", 0.1)
    rendered = sa._render_report([standing])
    assert "ul" in rendered
    assert "em-algorithm" in rendered


def _run_main(monkeypatch, argv):
    calls: list[list[str]] = []

    def fake_study_cycle_main(cycle_argv):
        calls.append(cycle_argv)
        return 0

    monkeypatch.setattr(sa.study_cycle, "main", fake_study_cycle_main)
    rc = sa.main(argv)
    return rc, calls


def test_main_runs_one_study_cycle_pass_per_course(tmp_path: Path, monkeypatch) -> None:
    f = _courses_toml(
        tmp_path,
        {"name": "ul", "workdir": str(tmp_path / "ul"), "corpus": ["notes.pdf"]},
        {"name": "algo", "workdir": str(tmp_path / "algo"), "corpus": ["notes2.pdf"]},
    )
    rc, calls = _run_main(monkeypatch, ["--courses", str(f)])
    assert rc == 0
    assert len(calls) == 2
    assert "--workdir" in calls[0]
    assert str(tmp_path / "ul") in calls[0]
    assert str(tmp_path / "algo") in calls[1]


def test_main_no_courses_registered(tmp_path: Path, monkeypatch, capsys) -> None:
    f = tmp_path / "empty.toml"
    f.write_text("", encoding="utf-8")
    rc, calls = _run_main(monkeypatch, ["--courses", str(f)])
    assert rc == 0
    assert calls == []
    assert "no courses registered" in capsys.readouterr().out


def test_main_passes_execute_flag_through(tmp_path: Path, monkeypatch) -> None:
    f = _courses_toml(
        tmp_path, {"name": "ul", "workdir": str(tmp_path / "ul"), "corpus": ["notes.pdf"]}
    )
    _, calls = _run_main(monkeypatch, ["--courses", str(f), "--execute", "--engine", "claude"])
    assert "--execute" in calls[0]
    assert "claude" in calls[0]


def test_main_prints_combined_standing_section(tmp_path: Path, monkeypatch, capsys) -> None:
    f = _courses_toml(
        tmp_path, {"name": "ul", "workdir": str(tmp_path / "ul"), "corpus": ["notes.pdf"]}
    )
    _run_main(monkeypatch, ["--courses", str(f)])
    out = capsys.readouterr().out
    assert "combined standing" in out
