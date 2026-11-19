"""In-app notifications for submission review workflows, shared by posts and scripts."""
import logging
from typing import List
from uuid import UUID
from models.notifications.notification_model import NotificationCreate

logger = logging.getLogger(__name__)


async def notify_submission(user_ids: List[UUID], title: str, message: str, link: str, submission_id: UUID) -> None:
    if not user_ids:
        return
    try:
        import asyncio
        from services.notifications.notification_service import create_notification
        await asyncio.gather(*(
            create_notification(NotificationCreate(
                user_id=str(uid), type="SUBMISSION", title=title, message=message, link_url=link,
                entity_type="content_submission", entity_id=str(submission_id),
            ))
            for uid in user_ids
        ))
    except Exception:
        logger.warning("Could not send review notifications for %s", submission_id, exc_info=True)
