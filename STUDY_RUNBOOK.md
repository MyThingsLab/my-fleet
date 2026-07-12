# Study runbook — running the learn-loop on a real exam

Operational guide for cramming a course with the MyThingsLab study tools
(`my-syllabus`, `my-glossary`, `my-professor`, `my-flashcards`, `my-grader`) and
the `study_cycle.py` driver. Worked example: the Unsupervised Learning exam.

## 0. One-time prerequisites

1. **The 5 PRs are merged** (or just run from the local editable installs — the
   shared `.venv` already has every tool). Sanity check:
   ```bash
   source ~/Desktop/MyThingsLab/.venv/bin/activate
   mysyllabus --help && myprofessor --help && myflashcards --help && mygrader --help
   ```
2. **Materials on disk**, as *text* PDFs (not scans — image-only PDFs ingest as
   near-empty, core#96). Verify a file has extractable text:
   ```bash
   pdftotext -l 2 course-program.pdf - | head        # non-empty => good
   ```
   You want three kinds of input:
   - **Program / syllabus** — the official course outline (→ topics).
   - **Corpus** — lecture notes, slides, textbook chapters (→ grounding for
     quizzes, definitions, flashcards, grading). Repeatable.
   - **Past papers** — questions you answer, for summative grading (optional).

3. **Pick a workdir** for this exam. All local state (topic list, decks, the
   mastery ledger) lives under `<workdir>/.mythings/`:
   ```bash
   mkdir -p ~/study/unsupervised-learning && cd ~/study/unsupervised-learning
   ```

Throughout: `--engine noop` (the default) is **free but produces no real
questions/grades** — it only degrades to showing source excerpts. Use
`--engine claude` for every real study action. Each quiz / grade / build /
decompose is **one billed Engine call**. Add `--cache .mythings/cache` to any
command with `--corpus` to memoise PDF text extraction (~1200× faster on repeat).

---

## 1. Decompose the syllabus into topics

```bash
mysyllabus decompose --program course-program.pdf --engine claude \
  --out .mythings/topics.toml
# human-readable outline instead:
mysyllabus decompose --program course-program.pdf --engine claude --format md
```

`topics.toml` is the study set the rest of the tools work from. **Open it and
edit** — trim, rename, reorder. Titles are what you'll quiz on; keep them stable
so mastery rolls up per topic.

---

## 2. The one-command pass (recommended daily driver)

`study_cycle.py` decomposes (if needed), picks your **weakest / never-seen**
topics from the mastery ledger, and generates fresh flashcards + a quiz for each,
then prints your standing:

```bash
python3 ~/Desktop/MyThingsLab/study_cycle.py \
  --workdir ~/study/unsupervised-learning \
  --corpus ~/study/unsupervised-learning/notes.pdf \
  --corpus ~/study/unsupervised-learning/slides.pdf \
  --program course-program.pdf \
  --engine claude --execute --topics-per-cycle 3
```

- Drop `--execute` first to **dry-run** — it prints exactly which commands it
  would bill, without spending anything.
- `--topics-per-cycle N` — how many topics to drill this pass (weakest first).
- Re-run each day: as you record grades, the selection shifts to what you're
  weakest on and what's due for review.

Then answer the printed quiz questions and record them (step 3).

---

## 3. Individual tools (finer control)

**Quiz yourself on one topic:**
```bash
myprofessor quiz "EM algorithm" --corpus notes.pdf --engine claude --questions 3
```

**Answer, and record the grade** (this is what moves your mastery):
```bash
myprofessor grade "EM algorithm" \
  --answer "EM alternates an E-step (responsibilities) and an M-step (maximise the expected complete-data log-likelihood); it never decreases the likelihood." \
  --corpus notes.pdf --engine claude --ledger .mythings/mastery.jsonl
```

**Define a term** (quick lookup, cited to the source):
```bash
myglossary define "variational lower bound" --corpus notes.pdf --engine claude
```

**Flashcards — build once, then drill free:**
```bash
myflashcards build "EM algorithm" --corpus notes.pdf --engine claude \
  --deck .mythings/decks/em.toml --count 8
myflashcards review --deck .mythings/decks/em.toml --ledger .mythings/mastery.jsonl
myflashcards grade "EM algorithm" --score 0.7 --ledger .mythings/mastery.jsonl   # 0=forgot .. 1=easy
```
Only `build` bills; `review` and `grade` are instant and free — drill as much as
you like.

**Grade a whole past paper** (summative — the strongest signal). Write your
answers as a TOML exam file:
```toml
# paper-2023.toml
[[answer]]
topic = "EM algorithm"
question = "State the two EM steps and why the likelihood never decreases."
answer = "E-step computes responsibilities; M-step maximises the expected log-likelihood; each step is a lower-bound tightening/ascent so L never decreases."

[[answer]]
topic = "PCA"
question = "What does PCA maximise, and what are the principal directions?"
answer = "Variance; the leading eigenvectors of the covariance matrix."
```
```bash
mygrader grade --exam paper-2023.toml --corpus notes.pdf --engine claude \
  --ledger .mythings/mastery.jsonl
```
One Engine call grades the whole paper and records one attempt per question.

---

## 4. Check your standing / what to study next

```bash
myprofessor due --ledger .mythings/mastery.jsonl          # what's due now, weakest first
myprofessor due --all --ledger .mythings/mastery.jsonl    # full standing, every topic
```

All five tools write to this **one** ledger, so quizzes, flashcard recalls, and
exam questions all roll into a single per-topic mastery picture.

---

## 5. A sensible daily rhythm

1. `study_cycle.py … --execute --topics-per-cycle 3` → today's quizzes for your
   weakest topics.
2. Answer each → `myprofessor grade …` (records the result).
3. Drill the matching flashcard decks (`review` + `grade`, free).
4. A few days before the exam: `mygrader grade` a past paper → per-topic gap report.
5. `myprofessor due --all` → confirm the weak topics are turning green; repeat.

---

## 6. Several exams in parallel

Courses are already isolated from each other: every course's topics, decks,
and mastery ledger live under its own `--workdir`, so two exams never
collide. `study_all.py` adds the orchestration on top — a registered list of
courses and one combined "what's most urgent, across all of them" report.

Declare your active courses in a small TOML registry:

```toml
# ~/study/courses.toml
[[course]]
name = "unsupervised-learning"
workdir = "~/study/ul"
corpus = ["~/study/ul/notes.pdf", "~/study/ul/slides.pdf"]
program = "~/study/ul/syllabus.pdf"   # optional
exam_date = "2026-07-28"              # optional, YYYY-MM-DD — sharpens ranking

[[course]]
name = "algorithms"
workdir = "~/study/algo"
corpus = ["~/study/algo/notes.pdf"]
exam_date = "2026-08-04"
```

Then run one pass across every course:

```bash
python3 -m myfleet.study_all --courses ~/study/courses.toml \
  --engine claude --execute --topics-per-cycle 3
```

This runs a normal `study_cycle` pass per course (same dry-run/`--execute`
gating, same per-topic billing), then prints a combined standing ranked
weakest-first — but a course whose `exam_date` is close scales that course's
scores down, so its weak topics surface ahead of an equally-weak topic in a
course that's weeks out. Drop `--execute` to see the plan without billing
anything; `--horizon-days` controls how far out an exam date still sharpens
the ranking (default 30).

---

## Gotchas

- **`--engine noop` is the default and does nothing useful** — always pass
  `--engine claude` for real study.
- **Retrieval can surface a table-of-contents or bibliography entry** for some
  queries (core#90). If a quiz/definition cites a reference list rather than the
  body, ignore it — known, tracked.
- **Image-only / scanned PDFs read as empty** (core#96) — check with `pdftotext`
  first; re-export or OCR if needed.
- **Keep topic names consistent** — mastery keys on a slug of the topic string,
  so "EM algorithm" and "EM Algorithm" merge, but "Expectation-Maximisation"
  is a different bucket. Prefer the titles in `topics.toml`.
- **Account / cost** — the tools use the ambient `claude` CLI auth. To bill a
  specific account or use a cheaper model, set `CLAUDE_CONFIG_DIR` in the
  environment before the command (same mechanism `fleet_cycle` uses).
