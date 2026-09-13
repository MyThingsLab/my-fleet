from pathlib import Path

import pytest

from myfleet import workspace


def test_the_climb_lands_in_the_scratch_dir_from_a_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The bug, stated as arithmetic. A Workspace checkout lives at <prefix>/tree,
    # so climbing three parents out of <prefix>/tree/src/myfleet/<mod>.py reaches
    # <prefix> -- a scratch dir under /tmp, not the fleet root.
    monkeypatch.delenv(workspace.ROOT_ENV, raising=False)
    prefix = tmp_path / "mythings-ws-abc123"
    module = prefix / "tree" / "src" / "myfleet" / "fleet_ask.py"
    module.parent.mkdir(parents=True)
    module.touch()

    assert workspace.fleet_root(str(module)) == prefix


def test_an_explicit_root_wins_over_the_climb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_root = tmp_path / "MyThingsLab"
    (real_root / "my-things-core").mkdir(parents=True)
    (real_root / "my-fleet").mkdir()
    module = tmp_path / "mythings-ws-abc123" / "tree" / "src" / "myfleet" / "fleet_ask.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setenv(workspace.ROOT_ENV, str(real_root))

    assert workspace.fleet_root(str(module)) == real_root


def test_a_blank_override_is_not_an_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unset var and one set to "" reach this differently but mean the same
    # thing; `Path("")` is `.`, which would silently root the fleet at the cwd.
    module = tmp_path / "root" / "my-fleet" / "src" / "myfleet" / "fleet_ask.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setenv(workspace.ROOT_ENV, "   ")

    assert workspace.fleet_root(str(module)) == tmp_path / "root"


def test_looks_like_fleet_root_wants_both_markers(tmp_path: Path) -> None:
    assert not workspace.looks_like_fleet_root(tmp_path)
    (tmp_path / "my-things-core").mkdir()
    assert not workspace.looks_like_fleet_root(tmp_path)
    (tmp_path / "my-fleet").mkdir()
    assert workspace.looks_like_fleet_root(tmp_path)


def test_a_scratch_prefix_does_not_look_like_a_fleet_root(tmp_path: Path) -> None:
    # What the climb returns from a worktree: the sibling a tool wrote there
    # (my-telegram-bot/.mythings/) is not enough to pass for the real thing.
    prefix = tmp_path / "mythings-ws-abc123"
    (prefix / "tree").mkdir(parents=True)
    (prefix / "my-telegram-bot" / ".mythings").mkdir(parents=True)

    assert not workspace.looks_like_fleet_root(prefix)
