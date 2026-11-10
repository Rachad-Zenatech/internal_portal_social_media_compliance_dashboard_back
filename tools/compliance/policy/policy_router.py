"""Social Media Content Policy Router
Read the platforms' policy pages, review the rules proposed from them, and approve them into the rulebook.
Reading is open to everyone signed in; reading pages, editing and deciding need rulebook-manage permission.
"""

import asyncio
import logging
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from models.compliance.policy_model import (
    PastedPolicyText,
    PolicyOverview,
    PolicyRuleProposal,
    ProposalDecision,
    ProposalUpdate,
)
from services.compliance.policy import policy_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/compliance/policies", tags=["Social Media Content Policy"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
# The same permission as adding or editing rulebook rules.
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


def _background(coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(lambda t: t.cancelled() or not t.exception() or logger.error(
        "Policy task %s failed: %s", name, t.exception()))


@router.get("", response_model=PolicyOverview)
async def policy_overview(_user_id: CurrentUser):
    """Every policy page, what it stands for (platform, region, policy name), when it was read and its proposals."""
    return await svc.overview()


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_all(_user_id: ManageUser, repropose: bool = False):
    """Read every page in the background. Pages whose text changed get new proposals (repropose: every page that
    reads, changed or not)."""
    if svc.is_refreshing():
        return {"started": False, "detail": "Already reading the pages"}
    _background(svc.refresh_all(force_propose=repropose), "policy-refresh-all")
    return {"started": True}


@router.post("/pages/{page_id}/fetch")
async def fetch_page(page_id: UUID, _user_id: ManageUser):
    """Read one page now; proposals follow when its text changed."""
    return await svc.fetch_page(page_id)


@router.post("/pages/{page_id}/text")
async def paste_text(page_id: UUID, body: PastedPolicyText, user_id: ManageUser):
    """The policy's text, pasted by a person, for a page that wouldn't load here."""
    return await svc.paste_text(page_id, body.text, user_id)


@router.post("/pages/{page_id}/propose", status_code=status.HTTP_202_ACCEPTED)
async def propose(page_id: UUID, _user_id: ManageUser):
    """Propose rules again from the page's latest text (undecided proposals from it are replaced)."""
    _background(svc.propose_for_page(page_id), f"policy-propose-{page_id}")
    return {"started": True}


@router.get("/proposals", response_model=List[PolicyRuleProposal])
async def list_proposals(
    _user_id: CurrentUser,
    status_filter: Optional[Literal["PENDING", "APPROVED", "REJECTED", "SUPERSEDED"]] = Query(None, alias="status"),
    page_id: Optional[UUID] = None,
):
    return await svc.list_proposals(status_filter, page_id)


@router.patch("/proposals/{proposal_id}", response_model=PolicyRuleProposal)
async def update_proposal(proposal_id: UUID, data: ProposalUpdate, _user_id: ManageUser):
    """Edit a proposal before deciding on it."""
    return await svc.update_proposal(proposal_id, data)


@router.post("/proposals/{proposal_id}/approve", response_model=PolicyRuleProposal)
async def approve(proposal_id: UUID, user_id: ManageUser, body: Optional[ProposalDecision] = None):
    """Add the proposal to the rulebook (Social Media Content Policy)."""
    return await svc.approve(proposal_id, user_id, body.note if body else None)


@router.post("/proposals/{proposal_id}/reject", response_model=PolicyRuleProposal)
async def reject(proposal_id: UUID, user_id: ManageUser, body: Optional[ProposalDecision] = None):
    return await svc.reject(proposal_id, user_id, body.note if body else None)
