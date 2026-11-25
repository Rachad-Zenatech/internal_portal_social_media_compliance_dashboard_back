"""CEO Dashboard machine-to-machine read endpoints for Social Media Compliance."""

from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from models.compliance.public_mood_model import NavPublicMoodResponse
from postgresql_db.database import fetch_all
from services.compliance.analytics import nav_public_mood_service
from services.shared.auth.service_security_policy import require_application_scope
from tools.compliance.analytics import listening_router

router = APIRouter(prefix="/service/ceo", tags=["CEO Integration"])

CeoServiceAuth = Annotated[
    Dict[str, Any],
    Depends(require_application_scope("metrics:read", allowed_services=["ceo-dashboard", "admin"])),
]


def _serialize_approved_row(row: Any, brand_names: Dict[str, str]) -> Dict[str, Any]:
    raw_codes = list(row["brand_codes"] or [])
    primary_code = row["brand_code"] or (raw_codes[0] if raw_codes else None)
    if primary_code and primary_code not in raw_codes:
        raw_codes = [primary_code, *raw_codes]
    resolved_names = [brand_names.get(code, code) for code in raw_codes]
    return {
        "id": str(row["id"]),
        "reference_code": row["reference_code"],
        "title": row["title"],
        "content_type": row["content_type"],
        "brand_code": primary_code,
        "brand_codes": raw_codes,
        "brand_name": brand_names.get(primary_code, primary_code) if primary_code else "Unassigned",
        "brand_names": resolved_names,
        "platforms": list(row["platforms"] or []),
        "status": row["status"],
        "caption_text": row["caption_text"] or "",
        "audience": row["audience"],
        "audiences": list(row["audiences"] or []),
        "region": row["region"],
        "publish_at": row["publish_at"].isoformat() if row["publish_at"] else None,
        "submitted_at": row["submitted_at"].isoformat() if row["submitted_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "created_by_name": row["created_by_name"] or "Unknown",
        "created_by_email": row["created_by_email"],
        "latest_score": row["latest_score"],
        "latest_traffic_light": row["latest_traffic_light"],
        "file_count": int(row["file_count"] or 0),
        "comment_count": int(row["comment_count"] or 0),
    }


@router.get("/approved-content")
async def list_approved_content_for_ceo(
    _: CeoServiceAuth,
    brand_code: Optional[str] = Query(None, max_length=50),
    limit: int = Query(500, ge=1, le=2000),
) -> Dict[str, Any]:
    """Return all approved content submissions and available brands for the CEO Dashboard."""
    brand_rows = await fetch_all(
        "SELECT code, name FROM brands WHERE is_active = true ORDER BY sort_order ASC, name ASC"
    )
    brand_names = {r["code"]: r["name"] for r in brand_rows}
    brands: List[Dict[str, str]] = [{"code": r["code"], "name": r["name"]} for r in brand_rows]

    normalized_brand = brand_code.strip().upper() if brand_code and brand_code.strip() else None
    rows = await fetch_all(
        """
        SELECT
            s.id,
            s.reference_code,
            s.title,
            s.content_type,
            s.brand_code,
            COALESCE(s.brand_codes, '{}'::text[]) AS brand_codes,
            COALESCE(s.platforms, '{}'::text[]) AS platforms,
            s.status,
            left(COALESCE(s.caption_text, ''), 1200) AS caption_text,
            s.audience,
            COALESCE(s.audiences, '{}'::text[]) AS audiences,
            s.region,
            s.publish_at,
            s.submitted_at,
            s.created_at,
            s.updated_at,
            u.full_name AS created_by_name,
            u.email AS created_by_email,
            (s.latest_check->>'score')::int AS latest_score,
            s.latest_check->>'traffic_light' AS latest_traffic_light,
            (SELECT count(*) FROM content_submission_files f
              WHERE f.submission_id = s.id AND f.deleted_at IS NULL) AS file_count,
            (SELECT count(*) FROM content_review_comments rc
              WHERE rc.submission_id = s.id AND rc.deleted_at IS NULL) AS comment_count
        FROM content_submissions s
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.deleted_at IS NULL
          AND UPPER(s.status) = 'APPROVED'
          AND (
              $1::text IS NULL
              OR UPPER(COALESCE(s.brand_code, '')) = $1
              OR EXISTS (
                  SELECT 1 FROM unnest(COALESCE(s.brand_codes, '{}'::text[])) bc
                  WHERE UPPER(bc) = $1
              )
          )
        ORDER BY COALESCE(s.publish_at, s.updated_at) DESC, s.updated_at DESC
        LIMIT $2
        """,
        normalized_brand,
        limit,
    )

    items = [_serialize_approved_row(r, brand_names) for r in rows]
    known_codes = {b["code"] for b in brands}
    for item in items:
        for code in item["brand_codes"]:
            if code and code not in known_codes:
                brands.append({"code": code, "name": brand_names.get(code, code)})
                known_codes.add(code)

    return {"items": items, "brands": brands, "total": len(items)}


@router.get("/public-mood", response_model=NavPublicMoodResponse)
async def get_public_mood_for_ceo(
    _: CeoServiceAuth,
    refresh: bool = Query(False),
):
    """Return the shared Nav Public Mood overview (Financial ZenaTech + Today's Posts)."""
    listening_router._clear_stale_scrape()
    return await nav_public_mood_service.get_nav_public_mood(
        refresh=refresh,
        scrape_state=str(listening_router._scrape.get("state") or "idle"),
    )


@router.post("/public-mood/scrape")
async def trigger_public_mood_scrape_for_ceo(
    background_tasks: BackgroundTasks,
    _: CeoServiceAuth,
    scope: Annotated[Literal["all", "financial", "today"], Query()] = "all",
) -> Dict[str, Any]:
    """Trigger a background public mood scrape from the CEO Dashboard."""
    chosen_scope = listening_router._normalize_scope(scope)
    listening_router._clear_stale_scrape()
    if listening_router._scrape.get("state") == "running":
        raise HTTPException(status_code=409, detail="A scrape is already running.")
    listening_router._scrape.update(
        state="running",
        scope=chosen_scope,
        started_at=listening_router._now(),
        finished_at=None,
        error=None,
        collection=None,
    )
    background_tasks.add_task(listening_router._run_scrape_and_refresh, chosen_scope)
    return {
        "status": "started",
        "scope": chosen_scope,
        "message": "Public mood scrape started in the background.",
    }
