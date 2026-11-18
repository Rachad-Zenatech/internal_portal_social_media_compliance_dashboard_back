"""Reviewer feedback on retrieved conversations (unrelated, wrong sentiment, recommendation corrections).

Each action records the feedback, then returns the refreshed public mood. Re-exported by public_mood_service."""

from uuid import UUID

from models.compliance.public_mood_model import CorrectionIn, PublicMood, SentimentCorrectionIn, UnrelatedIn
from services.compliance.analytics import conversation_feedback


async def get_public_mood(submission_id: UUID, user_id: UUID) -> PublicMood:
    # Imported late: public_mood_service re-exports this module.
    from services.compliance.analytics import public_mood_service

    return await public_mood_service.get_public_mood(submission_id, user_id)


async def mark_conversation_unrelated(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: UUID,
                                      data: UnrelatedIn) -> PublicMood:
    """A reviewer says a retrieved item is not about this post: it is left out of this draft's assessments."""
    await conversation_feedback.mark_unrelated(submission_id, assessment_id, item_id, user_id, data.reason_code, data.note)
    return await get_public_mood(submission_id, user_id)


async def restore_conversation_item(submission_id: UUID, item_id: UUID, user_id: UUID) -> PublicMood:
    await conversation_feedback.restore_item(submission_id, item_id)
    return await get_public_mood(submission_id, user_id)


async def correct_conversation_recommendation(submission_id: UUID, assessment_id: UUID, user_id: UUID,
                                              data: CorrectionIn) -> PublicMood:
    """A reviewer says the assessment's recommendation was wrong, and why (recorded; the assessment is unchanged)."""
    await conversation_feedback.correct_recommendation(
        submission_id, assessment_id, user_id, data.recommendation, data.reason_code, data.note)
    return await get_public_mood(submission_id, user_id)


async def confirm_conversation_recommendation(submission_id: UUID, assessment_id: UUID, user_id: UUID) -> PublicMood:
    """A reviewer says the assessment's recommendation is right."""
    await conversation_feedback.confirm_recommendation(submission_id, assessment_id, user_id)
    return await get_public_mood(submission_id, user_id)


async def resolve_conversation_decision(submission_id: UUID, assessment_id: UUID, user_id: UUID,
                                        data: CorrectionIn) -> PublicMood:
    """A lead reviewer settles a disagreement between reviewers about an assessment's recommendation."""
    await conversation_feedback.correct_recommendation(
        submission_id, assessment_id, user_id, data.recommendation, data.reason_code, data.note, resolution=True)
    return await get_public_mood(submission_id, user_id)


async def mark_conversation_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID, user_id: UUID,
                                      data: SentimentCorrectionIn) -> PublicMood:
    await conversation_feedback.mark_wrong_sentiment(
        submission_id, assessment_id, item_id, user_id, data.correct_sentiment, data.note)
    return await get_public_mood(submission_id, user_id)


async def clear_conversation_sentiment(submission_id: UUID, assessment_id: UUID, item_id: UUID,
                                       user_id: UUID) -> PublicMood:
    await conversation_feedback.clear_wrong_sentiment(submission_id, assessment_id, item_id, user_id)
    return await get_public_mood(submission_id, user_id)


async def clear_conversation_correction(submission_id: UUID, assessment_id: UUID, user_id: UUID) -> PublicMood:
    await conversation_feedback.clear_correction(submission_id, assessment_id, user_id)
    return await get_public_mood(submission_id, user_id)
