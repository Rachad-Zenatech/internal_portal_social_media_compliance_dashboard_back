from fastapi import APIRouter, Query, Depends
from typing import List, Optional, Any, Dict
from pydantic import BaseModel
from postgresql_db.database import fetch_all
from uuid import UUID
from services.shared.auth.auth_service import get_current_user_id_dependency, get_my_permissions

router = APIRouter()


class SearchResult(BaseModel):
    type: str
    id: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    url: Optional[str] = None


# Static system pages for quick navigation search with explicit permission codes
SYSTEM_PAGES = [
    {"perm_code": "DASHBOARD", "title": "Dashboard", "subtitle": "System Overview & Metric Cards", "url": "/dashboard", "keywords": ["dashboard", "home", "kpi", "overview", "analytics"]},
    {"perm_code": "CONFIG_USERS", "title": "Users Management", "subtitle": "Manage User Accounts & Roles", "url": "/configuration/users", "keywords": ["users", "accounts", "employees", "members", "staff"]},
    {"perm_code": "CONFIG_ROLES", "title": "Roles & Permissions", "subtitle": "Role Hierarchy & Navigation Access", "url": "/configuration/roles", "keywords": ["roles", "permissions", "rbac", "access", "groups"]},
    {"perm_code": "CONFIG_ROLE_API_PERMISSIONS", "title": "Role API Permissions", "subtitle": "Configure Backend Endpoint Access", "url": "/configuration/role-api-permissions", "keywords": ["api", "permissions", "endpoints", "security"]},
    {"perm_code": "CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "title": "Role MCP Tools", "subtitle": "Configure MCP Tool Access", "url": "/configuration/role-mcp-tool-permissions", "keywords": ["mcp", "tools", "ai", "permissions"]},
    {"perm_code": "SYSTEM_LOGS", "title": "AI Status & Usage", "subtitle": "AI Cores, Feature Mapping, Outages & Token Usage", "url": "/configurations/ai-status", "keywords": ["ai", "usage", "tokens", "cores", "bedrock", "embeddings", "outage", "overflow"]},
    {"perm_code": "CONFIG_USER_ROLE_ASSIGNMENT", "title": "User Role Assignments", "subtitle": "Assign Roles and Permissions to Users", "url": "/configuration/user-role-assignment", "keywords": ["assignment", "roles", "users", "assign"]},
    {"perm_code": "AUDIT_LOG", "title": "Audit Logs", "subtitle": "System Audit Trail & Login History", "url": "/log/audit", "keywords": ["audit", "logs", "history", "security", "activity", "trail"]},
]


def _has_search_perm(perms: Dict[str, Any], code: str) -> bool:
    is_super_admin = perms["user"]["is_super_admin"] or any(r.get("code") == "SUPER_ADMIN" for r in perms.get("roles", []))
    if is_super_admin:
        return True
    nav_perms: Dict[str, Any] = perms.get("navigation_permissions", {})
    if not nav_perms:
        return False
    if code in nav_perms:
        return True
    aliases = {"AUDIT_LOG": ["AUDIT_LOGS", "SYSTEM_LOGS"]}
    return any(alias in nav_perms for alias in aliases.get(code, []))


async def _search_users(search_term: str) -> List[SearchResult]:
    try:
        users = await fetch_all(
            """
            SELECT id, full_name, email, job_title, department
            FROM users
            WHERE deleted_at IS NULL AND is_active = true
              AND (full_name ILIKE $1 OR email ILIKE $1 OR job_title ILIKE $1 OR department ILIKE $1)
            ORDER BY full_name LIMIT 5
            """,
            search_term,
        )
        return [
            SearchResult(
                type="user",
                id=str(u["id"]),
                title=f"User: {u['full_name'] or u['email']}",
                subtitle=f"{u['email']} • {u['job_title'] or u['department'] or 'User'}",
                url="/configuration/users",
            )
            for u in users
        ]
    except Exception:
        return []


async def _search_roles(search_term: str) -> List[SearchResult]:
    try:
        roles = await fetch_all(
            """
            SELECT id, name, code, description, department
            FROM roles
            WHERE deleted_at IS NULL AND is_active = true
              AND (name ILIKE $1 OR code ILIKE $1 OR description ILIKE $1 OR department ILIKE $1)
            ORDER BY name LIMIT 5
            """,
            search_term,
        )
        return [
            SearchResult(
                type="role",
                id=str(r["id"]),
                title=f"Role: {r['name']}",
                subtitle=r["description"] or r["code"] or "Role Configuration",
                url="/configuration/roles",
            )
            for r in roles
        ]
    except Exception:
        return []


async def _search_audits(search_term: str) -> List[SearchResult]:
    try:
        audits = await fetch_all(
            """
            SELECT a.id, a.action, a.entity_type, a.entity_id, a.created_at, u.full_name as actor_name
            FROM audit_logs a
            LEFT JOIN users u ON a.actor_user_id = u.id
            WHERE (a.action ILIKE $1 OR a.entity_type ILIKE $1 OR u.full_name ILIKE $1 OR CAST(a.entity_id AS TEXT) ILIKE $1)
            ORDER BY a.created_at DESC LIMIT 5
            """,
            search_term,
        )
        return [
            SearchResult(
                type="audit",
                id=str(a["id"]),
                title=f"Audit: {a['action']} ({a['entity_type']})",
                subtitle=f"By {a['actor_name'] or 'System'} on {a['created_at'].strftime('%b %d, %Y') if a.get('created_at') else 'Recent'}",
                url="/log/audit",
            )
            for a in audits
        ]
    except Exception:
        return []


@router.get("")
async def global_search(
    q: str = Query(..., min_length=1, max_length=100),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> List[SearchResult]:
    query_str = q.strip()
    if not query_str:
        return []
    perms = await get_my_permissions(user_id)
    if not perms:
        return []

    search_term = f"%{query_str}%"
    lower_query = query_str.lower()
    results: List[SearchResult] = []
    for page in SYSTEM_PAGES:
        if not _has_search_perm(perms, page["perm_code"]):
            continue
        if (
            lower_query in page["title"].lower()
            or lower_query in page["subtitle"].lower()
            or any(lower_query in kw for kw in page["keywords"])
        ):
            results.append(
                SearchResult(type="page", id=page["perm_code"], title=f"Page: {page['title']}", subtitle=page["subtitle"], url=page["url"])
            )

    if _has_search_perm(perms, "CONFIG_USERS"):
        results.extend(await _search_users(search_term))
    if _has_search_perm(perms, "CONFIG_ROLES"):
        results.extend(await _search_roles(search_term))
    if _has_search_perm(perms, "AUDIT_LOG"):
        results.extend(await _search_audits(search_term))
    return results
