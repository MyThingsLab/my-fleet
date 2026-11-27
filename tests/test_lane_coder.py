from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mythings import gaps, paths
from mythings.testing import ScriptedEngine, make_git_repo

from myfleet import lane_coder, local_lane

_CORE_ENGINE = (
    "import json\n\n\n"
    "def parse_json_object(text):\n"
    "    if not text.strip():\n"
    "        return {}\n"
    "    try:\n"
    "        value = json.loads(text)\n"
    "    except ValueError:\n"
    "        return None\n"
    "    return value if isinstance(value, dict) else None\n"
)
_TOOL = (
    "import json\n"
    "from pathlib import Path\n\n\n"
    "def _parse_json_object(text):\n"
    "    try:\n"
    "        value = json.loads(text)\n"
    "    except ValueError:\n"
    "        return None\n"
    "    return value if isinstance(value, dict) else None\n\n\n"
    "def read(path: Path):\n"
    "    return _parse_json_object(path.read_text())\n"
)
_TEST = (
    "from tool.reader import read\n\n\n"
    "def test_read(tmp_path):\n"
    "    (tmp_path / 'a.json').write_text('{\"a\": 1}')\n"
    "    assert read(tmp_path / 'a.json') == {'a': 1}\n"
)
_YES = json.dumps({"reason": "same for a JSON object", "replaceable": True})
_NO = json.dumps({"reason": "the copy rejects padded text", "replaceable": False})
_CAND = lane_coder.Candidate(
    "tool", "tool.reader._parse_json_object", "mythings.engine.parse_json_object"
)


@pytest.fixture(autouse=True)
def lab_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "lab_python", lambda: Path(sys.executable))
    # The fake core shadows the real `mythings` package; the inner run keeps the fake and
    # drops pytest's `mythings.testing` plugin, which cannot import through it.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:mythings.testing")


def _core(root: Path) -> Path:
    files = {
        "src/mythings/__init__.py": "",
        "src/mythings/engine.py": _CORE_ENGINE,
        "src/mythings/github.py": "def gh(argv):\n    return ''\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _tool(tmp_path: Path, test: str = _TEST) -> Path:
    files = {"src/tool/__init__.py": "", "src/tool/reader.py": _TOOL, "tests/test_reader.py": test}
    return make_git_repo(tmp_path / "tool", files=files).path


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
    ).stdout


def test_the_graph_s_copy_and_namesake_gaps_become_candidates_and_others_do_not() -> None:
    found = [
        gaps.Gap("copy", "mythings.github.gh", ("my-a:mya.x.gh",), 5, "import mythings.github.gh"),
        gaps.Gap("copy", "mythings.tool._run", ("my-a:mya.x._run",), 5, "make it public"),
        gaps.Gap(
            "promote",
            "_parse_json_object",
            ("my-a:mya.y._parse_json_object", "my-b:myb.z._parse_json_object"),
            11,
            "core has mythings.engine.parse_json_object: check it does the same and import it",
        ),
        gaps.Gap("promote", "_git", ("my-a:mya.y._git", "my-b:myb.z._git"), 5, "move one _git"),
    ]

    by_repo = lane_coder.candidates(found)

    assert by_repo == {
        "my-a": [
            lane_coder.Candidate("my-a", "mya.x.gh", "mythings.github.gh"),
            lane_coder.Candidate(
                "my-a", "mya.y._parse_json_object", "mythings.engine.parse_json_object"
            ),
        ],
        "my-b": [
            lane_coder.Candidate(
                "my-b", "myb.z._parse_json_object", "mythings.engine.parse_json_object"
            )
        ],
    }


def test_adopting_deletes_the_copy_and_imports_core_s_under_its_name() -> None:
    node = lane_coder.top_level_function(_TOOL, "_parse_json_object")

    out = lane_coder.adopt(_TOOL, node, "_parse_json_object", "mythings.engine.parse_json_object")

    assert out == (
        "import json\n"
        "from pathlib import Path\n"
        "from mythings.engine import parse_json_object as _parse_json_object\n\n\n"
        "def read(path: Path):\n"
        "    return _parse_json_object(path.read_text())\n"
    )
    ast.parse(out)


def test_a_method_is_never_a_candidate_for_the_edit() -> None:
    source = "class A:\n    def _parse_json_object(self, text):\n        return None\n"

    assert lane_coder.top_level_function(source, "_parse_json_object") is None


def test_the_prompt_holds_both_bodies_and_every_call_and_a_yes_makes_the_edit(
    tmp_path: Path,
) -> None:
    tree = _tool(tmp_path)
    engine = ScriptedEngine(_YES)
    coder = lane_coder.Coder(engine, _core(tmp_path / "core"), [])

    assert coder.apply(tree, [_CAND]) == ["src/tool/reader.py"]

    (asked,) = engine.calls
    assert asked.schema == lane_coder.VERDICT
    assert "value = json.loads(text)\n" in asked.prompt  # the tool's copy
    assert "        return {}" in asked.prompt  # core's
    assert (
        "src/tool/reader.py:13:\ndef read(path: Path):\n    return _parse_json_object(path.read_text())"
        in asked.prompt
    )
    assert "def _parse_json_object" not in (tree / "src/tool/reader.py").read_text()
    assert coder.decided == [(_CAND, True, "same for a JSON object")]


def test_a_no_or_a_reply_that_is_not_a_verdict_leaves_the_copy(tmp_path: Path) -> None:
    tree = _tool(tmp_path)
    for reply in (_NO, "I think so"):
        coder = lane_coder.Coder(ScriptedEngine(reply), _core(tmp_path / "core"), [])

        assert coder.apply(tree, [_CAND]) == []

    assert "def _parse_json_object" in (tree / "src/tool/reader.py").read_text()
    assert coder.decided[0][2].startswith("no verdict")


def test_the_lane_commits_an_adoption_the_suite_keeps(tmp_path: Path) -> None:
    repo = _tool(tmp_path)
    coder = lane_coder.Coder(ScriptedEngine(_YES), _core(tmp_path / "core"), [])

    outcome = local_lane.change_one(repo, coder.core, lane_coder.task(coder, [_CAND]))

    assert outcome.outcome == "success"
    message = _git(repo, "log", "-1", "--format=%B", "lane/adopt-core")
    assert "_parse_json_object -> mythings.engine.parse_json_object" in message
    assert "def _parse_json_object" not in _git(repo, "show", "lane/adopt-core:src/tool/reader.py")


def test_a_wrong_yes_is_caught_by_the_suite_and_leaves_no_branch(tmp_path: Path) -> None:
    # Core's version reads empty text as an empty object; the copy says None, and a test pins it.
    pinned = _TEST + (
        "\n\ndef test_empty_text_is_no_object():\n"
        "    from tool.reader import _parse_json_object\n\n"
        "    assert _parse_json_object('') is None\n"
    )
    repo = _tool(tmp_path, pinned)
    coder = lane_coder.Coder(ScriptedEngine(_YES), _core(tmp_path / "core"), [])

    outcome = local_lane.change_one(repo, coder.core, lane_coder.task(coder, [_CAND]))

    assert outcome.outcome == "failed"
    assert outcome.new_failures == ("tests/test_reader.py::test_empty_text_is_no_object",)
    assert "lane/adopt-core" not in _git(repo, "branch")


def test_the_helpers_a_function_uses_are_shown_whole_and_nothing_else() -> None:
    source = (
        "import re\n\n_WORD = re.compile(r'\\w+')\n_OTHER = 1\n\n\n"
        "def _clean(text):\n    return text.strip()\n\n\n"
        "def tokenize(text):\n    return set(_WORD.findall(_clean(text)))\n"
    )
    node = lane_coder.top_level_function(source, "tokenize")

    assert lane_coder.helpers(source, node) == [
        "_WORD = re.compile(r'\\w+')",
        "def _clean(text):\n    return text.strip()",
    ]


def test_a_use_as_a_value_and_a_module_level_use_are_shown_too(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        "def _gh(argv):\n    return ''\n\n\n"
        "def run(runner=_gh):\n    return runner(['x'])\n\n\nDEFAULT = _gh\n"
    )

    shown = lane_coder.uses(tmp_path, "_gh")

    assert shown == [
        "src/m.py:5:\ndef run(runner=_gh):\n    return runner(['x'])",
        "src/m.py:9: DEFAULT = _gh",
    ]


def test_a_published_prompt_is_used_and_a_broken_one_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "adopt-prompt.json"
    assert lane_coder.tuned(path) == (lane_coder.SYSTEM, lane_coder.QUESTION)

    path.write_text(
        json.dumps({"system": "Be strict.", "instruction": "Can `{local}` go for `{target}`?"})
    )
    assert lane_coder.tuned(path) == ("Be strict.", "Can `{local}` go for `{target}`?")

    path.write_text(json.dumps({"system": "Be strict.", "instruction": "Can it go?"}))
    assert lane_coder.tuned(path) == (lane_coder.SYSTEM, lane_coder.QUESTION)


def test_the_coder_asks_with_the_prompt_it_was_given(tmp_path: Path) -> None:
    tree = _tool(tmp_path)
    engine = ScriptedEngine(_NO)
    coder = lane_coder.Coder(engine, _core(tmp_path / "core"), [], "Be strict.", "Swap `{local}`?")

    coder.apply(tree, [_CAND])

    (asked,) = engine.calls
    assert asked.system == "Be strict." and asked.prompt.endswith("Swap `_parse_json_object`?")
