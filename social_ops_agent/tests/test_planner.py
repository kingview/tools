from __future__ import annotations

import pytest

from social_ops_agent import ConversationalPlanner, PlanningError, SelectedSession


DOUYIN_SESSION = SelectedSession(
    session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
    platform="douyin",
    profile_name="抖音账号 01",
)


def test_plans_keyword_search_and_one_hundred_downloads() -> None:
    planner = ConversationalPlanner()

    plan = planner.create_plan(
        "通过关键词“web3”在抖音上搜索并下载前100个帖子",
        DOUYIN_SESSION,
    )

    assert plan.platform == "douyin"
    assert plan.query == "web3"
    assert plan.limit == 100
    assert plan.download is True
    assert plan.download_batch_size == 20
    assert plan.tool_call_budget == 6
    assert plan.requires_confirmation is True


def test_follow_up_can_adjust_previous_plan() -> None:
    planner = ConversationalPlanner()
    initial = planner.create_plan("在抖音搜索关键词“web3”并下载前100个帖子", DOUYIN_SESSION)

    changed = planner.create_plan("改成前50个", DOUYIN_SESSION, initial)

    assert changed.query == "web3"
    assert changed.limit == 50
    assert changed.download is True
    assert changed.tool_call_budget == 4


def test_rejects_platform_that_does_not_match_selected_session() -> None:
    planner = ConversationalPlanner()
    with pytest.raises(PlanningError, match="session_ref"):
        planner.create_plan("在小红书搜索关键词“web3”", DOUYIN_SESSION)


def test_rejects_external_write_actions() -> None:
    planner = ConversationalPlanner()
    with pytest.raises(PlanningError, match="只允许浏览和下载"):
        planner.create_plan("搜索 web3 并给前10条帖子点赞", DOUYIN_SESSION)


def test_plans_watermark_processing_as_an_explicit_extra_tool_step() -> None:
    planner = ConversationalPlanner()

    plan = planner.create_plan(
        "在抖音搜索关键词“web3”并下载前100个帖子，有水印就去水印",
        DOUYIN_SESSION,
    )

    assert plan.remove_watermark is True
    assert plan.download is True
    assert plan.tool_call_budget == 11
