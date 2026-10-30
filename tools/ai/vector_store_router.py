"""Vector store admin: check the MongoDB connection and create the search index (super admins only)."""

from typing import Annotated, Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from postgresql_db.database import fetch_one
from services.compliance import review_memory_service
from services.ai import vector_store_service
from services.shared.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/vector-store", tags=["Vector Store"])


async def _super_admin(user_id: Annotated[UUID, Depends(get_current_user_id_dependency)]) -> UUID:
    row = await fetch_one("SELECT is_super_admin FROM users WHERE id = $1", user_id)
    if not (row and row["is_super_admin"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only super admins can manage the vector store")
    return user_id


@router.get("/status")
async def get_status(_user_id: Annotated[UUID, Depends(_super_admin)]) -> Dict[str, Any]:
    """Whether MONGODB_URI is set and reachable, the collection and index in use, and how many documents it holds."""
    return await vector_store_service.status()


@router.post("/index")
async def create_index(_user_id: Annotated[UUID, Depends(_super_admin)]) -> Dict[str, Any]:
    """Create the Atlas vector search index on this project's collection if it's missing."""
    try:
        return await vector_store_service.ensure_index()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Couldn't create the index: {e}")


@router.post("/review-memory/sync")
async def sync_review_memory(_user_id: Annotated[UUID, Depends(_super_admin)]) -> Dict[str, Any]:
    """Store every decided review as precedent for sentiment analysis (only new or changed content is embedded).
    It also runs at startup and after each decision, so this is only needed after a bulk change."""
    if not vector_store_service.is_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The vector store isn't configured (MONGODB_URI)")
    try:
        return await review_memory_service.remember_all()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Couldn't update review memory: {e}")
