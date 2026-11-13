"""Script directory for the Share Document portal (app-to-app).

The Share Document portal's invite screen, for a script, lists the people of this portal who can review scripts. Its backend
asks here, signed with SCRIPT_SERVICE_SECRET (SESSION_SECRET when that isn't set) as the service "share-document" with the
scope "script_users:read", and names the person it asks for by email: the answer is who that person could send a script to.
"""

import os
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from postgresql_db.database import fetch_all, fetch_one
from services.compliance.submissions import submission_service as subs
from services.shared.auth.service_security_policy import JWT_ALGORITHM, JWT_SECRET

CALLER = "share-document"
SCOPE = "script_users:read"
_bearer = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/service")


async def require_share_document(credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> Dict[str, Any]:
    """The caller is the Share Document portal's backend, with the scope this route needs."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authentication credentials")
    try:
        claims = jwt.decode(credentials.credentials, os.getenv("SCRIPT_SERVICE_SECRET") or JWT_SECRET,
                            algorithms=[JWT_ALGORITHM], options={"verify_exp": True})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Service token has expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid service token: {e}")
    if claims.get("service") != CALLER or not claims.get("is_service_token"):
        raise HTTPException(status_code=403, detail="Only the Share Document portal's backend may call this")
    if SCOPE not in (claims.get("scopes") or []):
        raise HTTPException(status_code=403, detail=f"The token doesn't carry the scope {SCOPE}")
    return claims


@router.get("/script-users")
async def script_users(email: str = Query(..., min_length=3), _: Dict[str, Any] = Depends(require_share_document)) -> List[Dict[str, Any]]:
    """The people `email` could send a script to (reviewers, suggesting reviewers, super admins), in the shape of the
    Share Document portal's user directory."""
    me = await fetch_one("SELECT id FROM users WHERE lower(email) = lower($1) AND is_active AND deleted_at IS NULL", email.strip())
    if not me:
        raise HTTPException(status_code=404, detail=f"{email} isn't a user of the Social Media portal")
    options = await subs.reviewer_options(me["id"], None)
    extra = {r["id"]: r for r in await _profiles([o.id for o in options])}
    return [{"id": str(o.id), "full_name": o.name, "email": o.email,
             "job_title": (extra.get(o.id) or {}).get("job_title") or ("Suggesting reviewer" if o.kind == "SUGGESTER" else "Reviewer"),
             "department": (extra.get(o.id) or {}).get("department"), "is_super_admin": o.is_super_admin, "kind": o.kind}
            for o in options]


async def _profiles(ids: list) -> List[Dict[str, Any]]:
    if not ids:
        return []
    return await fetch_all("SELECT id, job_title, department FROM users WHERE id = ANY($1::uuid[])", ids)
