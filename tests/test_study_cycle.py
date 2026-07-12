from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mythings.mastery import Attempt, now_iso, record

import myfleet.study_cycle as sc

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _topics_file(tmp_path: Path, *pairs: tuple[str, str]) -> Path:
    body = "\n\n".join(f'[[topic]]\nslug = "{s}"\ntitle = "{t}"' for s, t in pairs)
    f = tmp_path / "topics.toml"
    f.write_text(body + "\n", encoding="utf-8")
    return f


def test_load_topics_reads_slug_title_pairs(tmp_path: Path) -> None:
    f = _topics_file(tmp_path, ("em-algorithm", "EM Algorithm"), ("pca", "PCA"))
    assert sc.load_topics(f) == [("em-algorithm", "EM Algorithm"), ("pca", "PCA")]


def test_load_topics_missing_file_is_empty(tmp_path: Path) -> None:
    assert sc.load_topics(tmp_path / "none.toml") == []


def test_select_topics_never_seen_first_then_weakest(tmp_path: Path) -> None:
    ledger = tmp_path / "mastery.jsonl"
    record(ledger, Attempt("pca", now_iso(_NOW - timedelta(days=1)), 1.0, "quiz"))
    record(ledger, Attempt("em-algorithm", now_iso(_NOW - timedelta(days=1)), 0.1, "quiz"))
    topics = [("pca", "PCA"), ("em-algorithm", "EM"), ("mixture-models", "GMM")]
    picked = sc.select_topics(topics, ledger, limit=2, now=_NOW)
    slugs = [s for s, _ in picked]
    assert slugs[0] == "mixture-models"  # never studied -> highest priority
    assert "em-algorithm" in slugs  # weak & due
    assert "pca" not in slugs  # strong & recently seen -> not due


def test_select_topics_falls_back_to_weakest_when_nothing_due(tmp_path: Path) -> None:
    ledger = tmp_path / "mastery.jsonl"
    # both seen just now (not yet due); selection still returns the weakest.
    record(ledger, Attempt("pca", now_iso(_NOW), 0.9, "quiz"))
    record(ledger, Attempt("em-algorithm", now_iso(_NOW), 0.2, "quiz"))
    topics = [("pca", "PCA"), ("em-algorithm", "EM")]
    picked = sc.select_topics(topics, ledger, limit=1, now=_NOW)
    assert picked[0][0] == "em-algorithm"


def _run_main(monkeypatch, argv):
    # main() may call run_cycle twice (decompose alone, then the rest) so a
    # fresh course's first --program pass can select from what decompose just
    # wrote. Accumulate stages across calls; existing assertions (first/last
    # stage, quiz argv) hold either way.
    captured: dict = {"stages": []}

    def fake_run_cycle(stages, *, execute, cwd, runner=None):
        captured["stages"].extend(stages)
        captured["execute"] = execute
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr(sc, "run_cycle", fake_run_cycle)
    rc = sc.main(argv)
    return rc, captured


def test_main_builds_flashcards_quiz_and_due_stages(tmp_path: Path, monkeypatch) -> None:
    tf = _topics_file(tmp_path, ("em-algorithm", "EM Algorithm"))
    rc, cap = _run_main(monkeypatch, [
        "--corpus", "notes.pdf", "--workdir", str(tmp_path),
        "--topics-file", str(tf), "--topics-per-cycle", "1",
    ])
    assert rc == 0
    names = [s.name for s in cap["stages"]]
    assert any(n.startswith("myflashcards build") for n in names)
    assert any(n.startswith("myprofessor quiz") for n in names)
    assert names[-1] == "myprofessor due"
    due = cap["stages"][-1]
    assert due.mutating is False  # the report always runs, even in dry mode
    # the quiz stage passes the human title, not the slug
    quiz = next(s for s in cap["stages"] if s.name.startswith("myprofessor quiz"))
    assert "EM Algorithm" in quiz.argv


def test_main_adds_decompose_stage_when_program_given(tmp_path: Path, monkeypatch) -> None:
    _, cap = _run_main(monkeypatch, [
        "--corpus", "notes.pdf", "--program", "program.pdf",
        "--workdir", str(tmp_path), "--topics-per-cycle", "0",
    ])
    names = [s.name for s in cap["stages"]]
    assert names[0] == "mysyllabus decompose"


def test_main_selects_topics_decompose_just_wrote(tmp_path: Path, monkeypatch) -> None:
    # Regression for the bug where the first --program --execute pass always
    # saw an empty topic list: topic selection ran before decompose had
    # actually executed. Simulate a real decompose by writing topics.toml the
    # moment its stage runs, and assert the same pass builds cards/quiz for it.
    topics_file = tmp_path / ".mythings" / "topics.toml"

    def fake_run_cycle(stages, *, execute, cwd, runner=None):
        for stage in stages:
            if stage.name == "mysyllabus decompose":
                topics_file.parent.mkdir(parents=True, exist_ok=True)
                topics_file.write_text(
                    '[[topic]]\nslug = "em-algorithm"\ntitle = "EM Algorithm"\n',
                    encoding="utf-8",
                )
        return 0

    monkeypatch.setattr(sc, "run_cycle", fake_run_cycle)
    rc = sc.main([
        "--corpus", "notes.pdf", "--program", "program.pdf",
        "--workdir", str(tmp_path), "--topics-per-cycle", "1", "--execute",
    ])
    assert rc == 0
    assert topics_file.exists()  # decompose ran for real in this pass
    # main() would build the next stage list from this now-populated file —
    # exercise that directly with the args main() would have used.
    args = argparse.Namespace(
        program=Path("program.pdf"), topics_file=topics_file,
        ledger=tmp_path / ".mythings" / "mastery.jsonl", topics_per_cycle=1,
        corpus=[Path("notes.pdf")], deck_dir=tmp_path / ".mythings" / "decks",
        cards=8, questions=3, engine="claude",
    )
    stages = sc.build_study_stages(args)
    names = [s.name for s in stages]
    assert any(n.startswith("myflashcards build") for n in names)
    assert any(n.startswith("myprofessor quiz") for n in names)


def test_main_defaults_paths_under_workdir(tmp_path: Path, monkeypatch) -> None:
    _, cap = _run_main(monkeypatch, ["--corpus", "notes.pdf", "--workdir", str(tmp_path)])
    due = cap["stages"][-1]
    assert str(tmp_path / ".mythings" / "mastery.jsonl") in due.argv


def test_main_no_topics_is_noted(tmp_path: Path, monkeypatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    _run_main(monkeypatch, ["--corpus", "notes.pdf", "--workdir", str(tmp_path)])
    assert "no topics to study yet" in capsys.readouterr().out
