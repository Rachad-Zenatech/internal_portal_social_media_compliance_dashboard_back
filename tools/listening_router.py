"""Listening topics: what social listening tracks (Sprout Social; sample data until connected), and the team's
tuning of how posts are matched to it."""

from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.public_mood_model import ListeningTopic, ListeningTopicUpdate, MatchTestRequest, MatchTestResult
from services import public_mood_service as svc
from services.auth_service import get_current_user_id_dependency, require_permission

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
