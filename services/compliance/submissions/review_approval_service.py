"""Approval gates for a submitted post: saved checks, findings and final approvers."""
from fastapi import HTTPException


async def require_approval(row, user_id, is_super):
    """Validate approval and return the refreshed row and its effective score."""
    from services.compliance.submissions import submission_service as subs
    submission_id = row["id"]
    # Nothing can be approved on a check that wasn't read in full: with the AI down every finding is only a
    # "needs review", so there'd be no score to hold to the minimum.
    saved = subs._json(row["latest_check"]) or {}
    current_rulebook = await subs.compliance_rulebook_version()
    if saved and (saved.get("rulebook_version") != current_rulebook or saved.get("review_caption_edited")):
        # Keep approval safe when the rules changed after the check: refresh the stored result before applying
        # the detailed approval gates below.
        await subs._recheck_one(dict(row), lambda before, result, check: None)
        row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
        if row["status"] != "IN_REVIEW":
            raise HTTPException(status_code=409, detail="The post changed while it was re-checked; open it again")
        saved = subs._json(row["latest_check"]) or {}
    if saved.get("review_caption_edited"):
        raise HTTPException(status_code=409, detail="The caption changed during its compliance check. Review the latest wording and try approval again.")
    subs.manual_check_service.require_current_check(row, current_rulebook, allow_incomplete=True, validate=False)
    if subs.analysis_pending(saved):
        raise HTTPException(
            status_code=422,
            detail="The post is still being read, so it has no compliance score yet. Approve it once the check has finished.",
        )
    failed = subs.reads_failed(saved)
    if failed:
        names = ", ".join(sorted({f"{f['what']} ({f['name']})" for f in failed})[:4])
        raise HTTPException(
            status_code=422,
            detail=f"Part of the post couldn't be read, so its wording was never checked: {names}. It's tried again "
                   "automatically when the cause was a temporary outage. Otherwise ask the creator to upload it "
                   "again, or request changes.",
        )
    if subs.ai_unavailable(saved):
        raise HTTPException(
            status_code=422,
            detail="The AI review couldn't run, so this has no compliance score and can't be approved yet. It's "
                   "checked again using the Compliance page once the AI is back. You can still request changes.",
        )
    standing = (await subs.flag_review_service.approval_standing([row])).get(row["id"])
    open_critical = standing.open_critical if standing else 0
    if open_critical:
        raise HTTPException(
            status_code=422,
            detail=f"{open_critical} critical compliance flag{'s are' if open_critical > 1 else ' is'} still open. "
                   "Request changes to fix it, or dismiss the flag with a reason if it's a false alarm.",
        )
    open_yellow = standing.open_yellow if standing else 0
    if open_yellow:
        raise HTTPException(
            status_code=422,
            detail=f"{open_yellow} flag{'s need' if open_yellow > 1 else ' needs'} a person's answer. Confirm each "
                   "one is right, or dismiss it with a reason if it's a false alarm, then approve.",
        )
    # Flags dismissed as false alarms give their points back; what's left must reach the minimum.
    if standing and standing.score_too_low:
        raise HTTPException(
            status_code=422,
            detail=f"The rulebook score is {standing.score}/100; approval needs at least {standing.min_score}. "
                   "Request changes to fix the flagged wording, or dismiss flags with a reason if they're false alarms.",
        )
    approval_score = standing.score if standing else None
    approver_ids, approver_names = subs._approvers(await subs.approval_path_service.load_rules(), row, subs._accounts(row))
    # A named final approver who delegated the post passes their approval to the delegate.
    approver_ids = await subs.delegation_service.with_delegates(submission_id, approver_ids)
    if not row.get("manager_review_access", False) and not subs._may_approve(user_id, is_super, approver_ids):
        raise HTTPException(
            status_code=403,
            detail=f"Final approval for this content is with {', '.join(approver_names)}. "
                   "You can still comment or request changes.",
        )
    return row, approval_score
