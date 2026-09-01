from __future__ import annotations

import pytest

from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.profile_tasks import ProfileTaskCoordinator


def test_same_profile_cannot_run_two_tasks_at_once(tmp_path) -> None:
    coordinator = ProfileTaskCoordinator(tmp_path)

    with coordinator.hold("http://127.0.0.1:54345", "profile-1"):
        with pytest.raises(CrawlerError) as raised:
            with coordinator.hold(
                "http://127.0.0.1:54345",
                "profile-1",
                timeout_seconds=0.01,
            ):
                pass

    assert raised.value.code == ErrorCode.SESSION_BUSY
    assert raised.value.retryable is True


def test_different_profiles_can_hold_independent_tasks(tmp_path) -> None:
    coordinator = ProfileTaskCoordinator(tmp_path)

    with coordinator.hold("http://127.0.0.1:54345", "profile-1"):
        with coordinator.hold("http://127.0.0.1:54345", "profile-2"):
            pass
