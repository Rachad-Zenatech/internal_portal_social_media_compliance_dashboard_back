"""Every endpoint about a submission's sources, under /api/submissions: checking links, pasted articles and images
(source_link_router), and people's calls on those sources (source_trust_router)."""

from fastapi import APIRouter

from tools.compliance.submissions.source_link_router import router as source_link_router
from tools.compliance.submissions.source_trust_router import router as source_trust_router

router = APIRouter()
router.include_router(source_link_router)
router.include_router(source_trust_router)
