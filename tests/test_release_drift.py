from pathlib import Path

from mythings._release import check_version_changelog, release_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_vendored_release_matches_canonical() -> None:
    vendored = REPO_ROOT / "RELEASE.md"
    assert vendored.read_text(encoding="utf-8") == release_text(), (
        "RELEASE.md is stale — re-vendor from my-things-core (its release.md is canonical)"
    )


def test_version_and_changelog_agree() -> None:
    errors = check_version_changelog(REPO_ROOT)
    assert not errors, errors
