"""Source trust: the checker learns which sites are worth reading from reviewers' calls on them.

People confirm, claim by claim, whether what a post says matches what the source says, and rate the source itself as
a good or a poor one. Everyone's calls are kept and shown with the check; only the calls of people deciding on the post
(reviewers, the people it was sent or delegated to, named final approvers and their delegates; never its author)
count towards a site's standing, which:

- orders the sources found for a citation, so sites reviewers rated good are read first and poor ones last
  (image_source_checks), and
- is shown with every source, so the author and the reviewer know whether it has a good track record.

A mismatch says nothing against the source (catching a post's wrong figure is what a good source is for), so it
doesn't lower a site's standing; only a "poor source" rating does.
"""

import hashlib
import os
import re
from typing import Dict, Iterable, List
from urllib.parse import urlsplit
from uuid import UUID

from models.compliance.source_trust_model import (
    SourceConfirmation, SourceConfirmationRequest, SourceConfirmationsResponse, SourceTrust,
)
from postgresql_db.database import execute, fetch_all

# A site nobody has rated, with at least this many claims reviewers checked against it, counts as trusted.
TRUST_MIN_CONFIRMATIONS = int(os.getenv("SOURCE_TRUST_MIN_CONFIRMATIONS", "3"))
_RANK = {"TRUSTED": 0, "UNRATED": 1, "FLAGGED": 2}


def host_of(url: str) -> str:
    host = (urlsplit(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def claim_key(claim: str) -> str:
    """The claim as worded, ignoring case, spacing and punctuation; '' for a call on the page itself."""
    words = " ".join(re.findall(r"[a-z0-9.$%]+", (claim or "").lower()))
    return hashlib.sha256(words.encode("utf-8")).hexdigest()[:64] if words else ""


def _standing(host: str, row: Dict) -> SourceTrust:
    good, poor = row.get("good", 0), row.get("poor", 0)
    confirmations, matches = row.get("confirmations", 0), row.get("matches", 0)
    if poor > good:
        level = "FLAGGED"
        message = f"Reviewers rated this site a poor source ({poor} to {good}). Look for a better one."
    elif good > poor or (good == poor == 0 and confirmations >= TRUST_MIN_CONFIRMATIONS):
        level = "TRUSTED"
        rated = f"rated it a good source {good} time{'s' if good != 1 else ''}" if good else None
        checked = f"checked {confirmations} claim{'s' if confirmations != 1 else ''} against it" if confirmations else None
        message = "Reviewers " + " and ".join(p for p in (rated, checked) if p) + "."
    else:
        level = "UNRATED"
        message = "No reviewer has rated this site yet." if not confirmations else (
            f"Reviewers checked {confirmations} claim{'s' if confirmations != 1 else ''} against it, but haven't rated it."
        )
    return SourceTrust(host=host, level=level, confirmations=confirmations, matches=matches,
                       good_votes=good, poor_votes=poor, message=message)


async def trust_for(urls: Iterable[str]) -> Dict[str, SourceTrust]:
    """Each site's standing from reviewers' calls, by site."""
    hosts = sorted({host_of(u) for u in urls if u} - {""})
    if not hosts:
        return {}
    rows = await fetch_all(
        """
        SELECT host,
               count(*) FILTER (WHERE claim_key <> '' AND matches IS NOT NULL) AS confirmations,
               count(*) FILTER (WHERE claim_key <> '' AND matches) AS matches,
               count(*) FILTER (WHERE claim_key = '' AND good_source) AS good,
               count(*) FILTER (WHERE claim_key = '' AND good_source = false) AS poor
        FROM source_confirmations
        WHERE host = ANY($1::text[]) AND by_reviewer
        GROUP BY host
        """,
        hosts,
    )
    found = {r["host"]: dict(r) for r in rows}
    return {h: _standing(h, found.get(h, {})) for h in hosts}


async def rank_by_trust(urls: List[str]) -> List[str]:
    """The URLs with sites reviewers rated good first and poor ones last, keeping the order within each."""
    try:
        trust = await trust_for(urls)
    except Exception:  # no database (tests, scripts): keep the order as given
        return list(urls)
    return sorted(urls, key=lambda u: _RANK[trust[host_of(u)].level] if host_of(u) in trust else 1)


async def counts_toward_trust(row: Dict, user_id: UUID) -> bool:
    """Whether this person's calls on this post's sources count towards a site's standing: someone deciding on it, not
    its author. That is a reviewer, anyone it was sent or delegated to, or a named final approver (or whoever they
    delegated it to), who may not hold the reviewer role. Super admins always count."""
    from services.compliance.submissions import approval_path_service, delegation_service
    from services.compliance.submissions import submission_service as subs

    if await subs._is_super_admin(user_id):
        return True
    if row["created_by"] == user_id:
        return False  # nobody vouches for the sources of their own post
    if user_id in (row.get("reviewer_ids") or []) or await subs._can_review(user_id):
        return True
    approvers, _ = subs._approvers(await approval_path_service.load_rules(), row, subs._accounts(row))
    return user_id in await delegation_service.with_delegates(row["id"], approvers)


async def record(submission_id: UUID, user_id: UUID, data: SourceConfirmationRequest) -> SourceConfirmationsResponse:
    """Keep (or take back) one call, and return the post's calls with the site's standing as it now is."""
    from services.compliance.submissions import submission_service
    from services.compliance.submissions.source_link_service import normalize_source_url

    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    url = normalize_source_url(data.url)
    key = claim_key(data.claim or "")
    value_missing = (data.matches is None) if key else (data.good_source is None)
    if value_missing:
        await execute(
            "DELETE FROM source_confirmations WHERE submission_id = $1 AND user_id = $2 AND url = $3 AND claim_key = $4",
            submission_id, user_id, url, key,
        )
    else:
        await execute(
            """
            INSERT INTO source_confirmations
                (submission_id, user_id, by_reviewer, url, host, claim_key, claim, source_quote, matches, good_source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (submission_id, user_id, url, claim_key) DO UPDATE SET
                by_reviewer = EXCLUDED.by_reviewer, claim = EXCLUDED.claim, source_quote = EXCLUDED.source_quote,
                matches = EXCLUDED.matches, good_source = EXCLUDED.good_source, updated_at = NOW()
            """,
            submission_id, user_id, await counts_toward_trust(row, user_id), url, host_of(url), key,
            (data.claim or None), data.source_quote, data.matches if key else None, None if key else data.good_source,
        )
    return await for_submission(submission_id, user_id, [url])


async def for_submission(submission_id: UUID, user_id: UUID, urls: List[str]) -> SourceConfirmationsResponse:
    """Everyone's calls on this post's sources, and the standing of the given sources' sites."""
    from services.compliance.submissions import submission_service

    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    rows = await fetch_all(
        """
        SELECT user_id, by_reviewer, url, claim, matches, good_source
        FROM source_confirmations WHERE submission_id = $1 ORDER BY updated_at
        """,
        submission_id,
    )
    calls = [SourceConfirmation(url=r["url"], claim=r["claim"], matches=r["matches"], good_source=r["good_source"],
                                by_reviewer=r["by_reviewer"], mine=r["user_id"] == user_id) for r in rows]
    return SourceConfirmationsResponse(
        confirmations=calls,
        trust=await trust_for(list(urls) + [c.url for c in calls]),
        counts_toward_trust=await counts_toward_trust(row, user_id),
    )
