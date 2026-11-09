"""Listening topics: what social listening tracks (Sprout Social or X/Twitter JSON feed), trend discovery,
and the team's tuning of how posts and scripts are matched to it."""

from typing import Annotated, List
from uuid import UUID

import asyncio
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, Query

from models.compliance.public_mood_model import (
    ListeningTopic,
    ListeningTopicUpdate,
    MatchTestRequest,
    MatchTestResult,
    XIngestRequest,
    XTrendOpportunity,
)
from services.compliance.analytics import public_mood_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency, require_permission

router = APIRouter(prefix="/listening")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
# Tuning sits with editing the compliance rulebook and the messaging context.
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


@router.get("/topics", response_model=List[ListeningTopic])
async def list_topics(user_id: CurrentUser):
    """Every topic with its current mood, the team's keyword tuning and how often it was matched wrongly."""
    return await svc.list_topics()


@router.post("/topics/sync", response_model=List[ListeningTopic])
async def sync_topics(user_id: ManageUser):
    """Pull the topic list from the listening source now instead of waiting for the daily sync."""
    return await svc.list_topics(refresh=True)


@router.put("/topics/{topic_id}", response_model=ListeningTopic)
async def update_topic(topic_id: UUID, data: ListeningTopicUpdate, user_id: ManageUser):
    return await svc.update_topic(topic_id, data, user_id)


@router.post("/match-test", response_model=MatchTestResult)
async def match_test(data: MatchTestRequest, user_id: CurrentUser):
    """Which topics this text would match and why, including the near misses. Nothing is saved."""
    return await svc.match_test(data.text)


@router.get("/trends", response_model=List[XTrendOpportunity])
async def get_trends(user_id: CurrentUser, refresh: bool = Query(False)):
    """What is trending right now, whether sentiment is positive or negative, and whether the team can jump on
    the trend (POST), wait (WAIT), or avoid/escalate due to crisis/backlash (HOLD)."""
    return await svc.list_trend_opportunities(refresh=refresh)


@router.post("/x-ingest", response_model=List[XTrendOpportunity])
async def ingest_x_comments(data: XIngestRequest, user_id: ManageUser):
    """Ingest scraped or collected X (Twitter) topics and comments, classify each comment as POSITIVE/NEGATIVE/NEUTRAL
    with confidence, save to `.data/x_listening_feed.json`, and sync into the listening pipeline."""
    return await svc.ingest_x_json(data)

logger = logging.getLogger("listening_router")


async def _run_scrape_and_refresh() -> None:
    try:
        from scripts import scrape_x_playwright as sxp
        await sxp.build_live_x_listening_json()
        await svc.list_topics(refresh=True)
        await svc.list_trend_opportunities(refresh=True)
    except Exception as exc:
        logger.warning("Scrape background task failed: %s", exc)


@router.post("/scrape")
async def trigger_live_scrape(
    background_tasks: BackgroundTasks,
    user_id: CurrentUser,
):
    """Trigger live scraping of public X posts and news trends via Playwright + VADER,
    updating the listening feed in the background. Designed to transition smoothly to live API."""
    background_tasks.add_task(_run_scrape_and_refresh)
    return {
        "status": "started",
        "message": "Scraping live X (Twitter) posts and news sentiment in the background...",
        "source": "Playwright Scraper (Global News & Industry X Trends) + VADER Sentiment",
    }
