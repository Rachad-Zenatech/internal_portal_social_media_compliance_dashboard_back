"""Manual cancellation of background post analyses."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from models.compliance.sentiment_model import SentimentAnalysis
from services.compliance.sentiment.cancellation import stop_analysis
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.post("/{submission_id}/sentiment/{analysis_id}/stop", response_model=SentimentAnalysis)
async def stop_sentiment(submission_id: UUID, analysis_id: UUID, user_id: CurrentUser):
    return await stop_analysis(submission_id, analysis_id, user_id)
