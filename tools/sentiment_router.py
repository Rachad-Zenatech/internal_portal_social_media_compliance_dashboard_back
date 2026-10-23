"""Sentiment analysis quality tools for reviewers: which kinds of finding are noise, and the test set that prompt
and model changes are checked against (see scripts/sentiment_eval.py)."""

from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from models.sentiment_model import EvalCase, EvalCaseIn, FeedbackSummaryRow
from services import sentiment_service
from services import submission_service as svc
from services.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/sentiment", tags=["Sentiment Quality"])


async def _reviewer(user_id: Annotated[UUID, Depends(get_current_user_id_dependency)]) -> UUID:
    if not await svc._can_review(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only reviewers can see this")
    return user_id


Reviewer = Annotated[UUID, Depends(_reviewer)]


@router.get("/feedback-summary", response_model=List[FeedbackSummaryRow])
async def feedback_summary(_user_id: Reviewer):
    """How reviewers rated each kind of finding, noisiest (most often "not an issue") first."""
    return await sentiment_service.feedback_summary()


@router.get("/eval-cases", response_model=List[EvalCase])
async def list_eval_cases(_user_id: Reviewer):
    return await sentiment_service.list_eval_cases()


@router.post("/eval-cases", response_model=EvalCase, status_code=status.HTTP_201_CREATED)
async def create_eval_case(data: EvalCaseIn, user_id: Reviewer):
    """Save an analysed post as a test case, with the verdict the reviewer agrees is right."""
    return await sentiment_service.create_eval_case(data, user_id)


@router.delete("/eval-cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_eval_case(case_id: UUID, _user_id: Reviewer):
    await sentiment_service.delete_eval_case(case_id)
