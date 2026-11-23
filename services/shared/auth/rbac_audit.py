"""Audit and login activity logging for Social Media Compliance RBAC."""

import json
from uuid import UUID

from postgresql_db.database import fetch_all, get_pool
from services.notifications.audit_hub_publisher import publish_audit_log_from_conn


async def log_audit(
    conn,
    actor_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    old_value: dict = None,
    new_value: dict = None,
    ip: str = None,
    ua: str = None,
):
    sql = """
        INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, old_value, new_value, ip_address, user_agent, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
    """
    old_json = json.dumps(old_value, default=str) if old_value else None
    new_json = json.dumps(new_value, default=str) if new_value else None
    await conn.execute(
        sql,
        actor_id,
        action,
        entity_type,
        str(entity_id) if entity_id is not None else None,
        old_json,
        new_json,
        ip,
        ua,
    )
    await publish_audit_log_from_conn(
        conn,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip,
        user_agent=ua,
    )


async def get_audit_logs():
    sql = """
        SELECT a.*, u.full_name as actor_name, u.email as actor_email
        FROM audit_logs a
        LEFT JOIN users u ON a.actor_user_id = u.id
        ORDER BY a.created_at DESC
        LIMIT 1000
    """
    rows = await fetch_all(sql)
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("old_value"), str):
            d["old_value"] = json.loads(d["old_value"])
        if isinstance(d.get("new_value"), str):
            d["new_value"] = json.loads(d["new_value"])
        for key in ("old_value", "new_value"):
            if isinstance(d.get(key), dict):
                d[key].pop("password", None)
                d[key].pop("password_hash", None)
        result.append(d)
    return result


async def log_login_activity(
    email: str,
    user_id: UUID | None,
    success: bool,
    failure_reason: str | None,
    ip_address: str | None,
    user_agent: str | None,
):
    pool = get_pool()
    async with pool.acquire() as conn:
        sql = """
            INSERT INTO login_activity_logs (email, user_id, success, failure_reason, ip_address, user_agent, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
        """
        await conn.execute(sql, email, user_id, success, failure_reason, ip_address, user_agent)


async def get_login_activity_logs():
    sql = """
        SELECT l.*, u.full_name as user_full_name
        FROM login_activity_logs l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.created_at DESC
        LIMIT 1000
    """
    rows = await fetch_all(sql)
    return [dict(r) for r in rows]


async def log_audit_action(action: str, user_id: UUID, details: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        await log_audit(conn, user_id, action, "auth_access", user_id, None, {"details": details})
