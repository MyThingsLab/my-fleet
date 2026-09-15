from __future__ import annotations

import pytest

from myfleet.campaign import (
    Campaign,
    CampaignVerdict,
    ChildState,
    expand,
    tally,
)


def _campaign(repos: tuple[str, ...] = ("my-coder", "my-fleet", "my-guard")) -> Campaign:
    return Campaign(
        parent_issue="my-fleet#84",
        repos=repos,
        template_diff="--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n",
    )


class TestExpand:
    def test_one_child_per_repo(self) -> None:
        found = expand(_campaign())
        assert [c.repo for c in found] == ["my-coder", "my-fleet", "my-guard"]

    def test_deterministic_regardless_of_input_order(self) -> None:
        a = expand(_campaign(("my-guard", "my-coder", "my-fleet")))
        b = expand(_campaign(("my-fleet", "my-guard", "my-coder")))
        assert a == b

    def test_child_body_carries_the_template_diff_and_parent_link(self) -> None:
        (child,) = expand(_campaign(("my-fleet",)))
        assert "ci.yml" in child.body
        assert "campaign-parent: my-fleet#84" in child.body

    def test_children_share_the_same_title(self) -> None:
        found = expand(_campaign())
        assert len({c.title for c in found}) == 1

    def test_empty_repo_set_rejected(self) -> None:
        with pytest.raises(ValueError, match="no repos"):
            expand(_campaign(()))

    def test_duplicate_repo_rejected(self) -> None:
        with pytest.raises(ValueError, match="more than once"):
            expand(_campaign(("my-fleet", "my-fleet")))


class TestTally:
    def test_all_landed_is_complete(self) -> None:
        found = tally([ChildState.LANDED] * 3)
        assert found.verdict is CampaignVerdict.COMPLETE
        assert found.landed == 3
        assert found.total == 3

    def test_some_landed_rest_permanently_stuck_is_partial(self) -> None:
        # the #109 example: 17 land, 3 block -- a success with a remainder
        found = tally([ChildState.LANDED] * 17 + [ChildState.BLOCKED] * 3)
        assert found.verdict is CampaignVerdict.PARTIAL
        assert found.landed == 17
        assert found.counts[ChildState.BLOCKED] == 3

    def test_none_landed_all_stuck_is_blocked(self) -> None:
        found = tally([ChildState.NEEDS_HUMAN, ChildState.BLOCKED])
        assert found.verdict is CampaignVerdict.BLOCKED
        assert found.landed == 0

    def test_still_moving_is_in_progress(self) -> None:
        found = tally([ChildState.LANDED, ChildState.PENDING, ChildState.IN_PROGRESS])
        assert found.verdict is CampaignVerdict.IN_PROGRESS

    def test_all_pending_is_in_progress_not_blocked(self) -> None:
        found = tally([ChildState.PENDING] * 5)
        assert found.verdict is CampaignVerdict.IN_PROGRESS
        assert found.landed == 0

    def test_one_stuck_child_among_pending_others_is_still_in_progress(self) -> None:
        # not every remaining child is stuck yet, so the set can still move
        found = tally([ChildState.BLOCKED, ChildState.PENDING, ChildState.PENDING])
        assert found.verdict is CampaignVerdict.IN_PROGRESS

    def test_empty_tally_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            tally([])
