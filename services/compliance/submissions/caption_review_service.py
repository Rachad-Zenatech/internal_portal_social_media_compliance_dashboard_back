"""Persist review-time caption edits atomically with an attributed before/after event."""
import json
from services.compliance.submissions.review_caption_format import saved_html
from uuid import UUID

from fastapi import HTTPException

from models.compliance.caption_review_model import CaptionEditRequest, CaptionReview, CaptionRevision
from postgresql_db.database import fetch_all, get_pool


def _submissions():
    from services.compliance.submissions import submission_service
    return submission_service


async def get_caption_review(submission_id: UUID, user_id: UUID) -> CaptionReview:
    from services.compliance.submissions.review_handoff import may_edit_text
    subs = _submissions()
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    events = await fetch_all(
        """SELECT e.id, e.data, e.actor_user_id, e.created_at,
                  coalesce(u.full_name, 'Former user') AS actor_name
           FROM content_submission_events e LEFT JOIN users u ON u.id = e.actor_user_id
           WHERE e.submission_id = $1 AND e.event_type = 'CAPTION_EDITED'
           ORDER BY e.created_at DESC, e.id DESC""", submission_id,
    )
    revisions = []
    for event in events:
        data = subs._json(event['data']) or {}
        revisions.append(CaptionRevision(
            id=event['id'], before=data['before'], after=data['after'],
            before_html=data.get('before_html'), after_html=data.get('after_html'),
            platform=data.get('platform'), version=data['version'],
            actor_id=event['actor_user_id'], actor_name=event['actor_name'], created_at=event['created_at'],
        ))
    return CaptionReview(
        caption_text=row.get('caption_text') or '', caption_html=row.get('caption_html'),
        platform_captions=subs._platform_captions(row),
        platforms=list(row.get('platforms') or []),
        can_edit=await may_edit_text(row, user_id), revisions=revisions,
        needs_recheck=bool((subs._json(row.get('latest_check')) or {}).get('review_caption_edited')),
    )


async def save_caption(submission_id: UUID, user_id: UUID, data: CaptionEditRequest) -> CaptionReview:
    subs = _submissions()
    await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if not await subs._can_suggest(user_id):
        raise HTTPException(status_code=403, detail='Only reviewers can edit captions during review')
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '8s'")
            # Lock before comparing the editor's base, preventing concurrent saves from losing an edit.
            row = await conn.fetchrow(
                'SELECT * FROM content_submissions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE', submission_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail='Submission not found')
            if row['status'] != 'IN_REVIEW':
                raise HTTPException(status_code=409, detail='Captions can only be edited while the post is in review')
            from services.compliance.submissions.review_handoff import may_edit_text
            if not await may_edit_text(row, user_id, conn):
                raise HTTPException(403, 'Text suggestions are closed or you do not have reviewer access')
            captions = subs._platform_captions(row)
            if data.platform is not None and data.platform not in (row.get('platforms') or []):
                raise HTTPException(status_code=422, detail='That platform is not selected for this post')
            before = (captions.get(data.platform) if data.platform else None) or row.get('caption_text') or ''
            if before != data.base_caption:
                raise HTTPException(status_code=409, detail='The caption changed since you opened it. Cancel and reopen the editor to load the latest text; copy your edits first.')
            if not data.platform and 'base_caption_html' in data.model_fields_set and data.base_caption_html != row.get('caption_html'):
                raise HTTPException(409, 'The caption formatting changed. Copy your edits, then reopen the editor.')
            html = saved_html(data.caption_text, data.caption_html, before, row.get('caption_html'))
            if before != data.caption_text or (not data.platform and html != row.get('caption_html')):
                await _persist_edit(conn, row, user_id, data, captions, before)
    return await get_caption_review(submission_id, user_id)


async def _persist_edit(conn, row, user_id, data, captions, before):
    subs = _submissions()
    if data.platform:
        captions[data.platform] = data.caption_text
        await conn.execute(
            """UPDATE content_submissions SET platform_captions = $2::jsonb, updated_by = $3,
                      updated_at = NOW(), content_edited_at = NOW() WHERE id = $1""",
            row['id'], json.dumps(captions), user_id,
        )
    else:
        # Store matching HTML as well: publishing and formatted previews must use the edited text.
        html = saved_html(data.caption_text, data.caption_html, before, row.get('caption_html'))
        await conn.execute(
            """UPDATE content_submissions SET caption_text = $2, caption_html = $3, updated_by = $4,
                      updated_at = NOW(), content_edited_at = NOW() WHERE id = $1""",
            row['id'], data.caption_text, html, user_id,
        )
    await conn.execute(
        """UPDATE content_submissions SET latest_check = CASE WHEN latest_check IS NULL THEN NULL
               ELSE latest_check || '{\"review_caption_edited\":true}'::jsonb END WHERE id = $1""", row['id'],
    )
    await subs._add_event(conn, row['id'], 'CAPTION_EDITED', 'Caption edited during review', user_id, {
        'before': before, 'after': data.caption_text, 'platform': data.platform,
        'before_html': row.get('caption_html') if not data.platform else None,
        'after_html': html if not data.platform else None,
        'version': row['current_version'],
    })


async def decision_status(conn, row):
    """Do not approve a caption changed after the review's check/decision preflight."""
    current = await conn.fetchrow(
        'SELECT status, content_edited_at FROM content_submissions WHERE id = $1 FOR UPDATE', row['id'],
    )
    if current and current.get('content_edited_at') != row.get('content_edited_at'):
        raise HTTPException(status_code=409, detail='The post changed while approval was being prepared. Refresh and review the latest caption.')
    return current['status'] if current else None



def feedback_is_current_sql(alias, timestamp='decided_at'):
    """Keep audit rows, but never reuse a decision made before a caption edit on that version."""
    if alias not in {'f', 'v', 'r'} or timestamp not in {'decided_at', 'requested_at'}:
        raise ValueError('Unsupported feedback alias')
    return f"""NOT EXISTS (
        SELECT 1 FROM content_submission_events ce
        WHERE ce.submission_id = {alias}.submission_id AND ce.event_type = 'CAPTION_EDITED'
          AND ce.data->>'version' = CAST({alias}.version_number AS TEXT)
          AND ce.created_at > {alias}.{timestamp}
    )"""
