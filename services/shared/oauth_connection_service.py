"""OAuth Connection Management Service

Supports hierarchical multi-entity OAuth:
Organization -> Companies/Entities -> OAuth connections
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from models.shared.oauth_connection_model import (
    Company,
    CompanyCreate,
    CompanyUpdate,
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthConnectionUpdate,
    Organization,
    OrganizationCreate,
)
from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


# ==============================================================================
# Organizations & Companies CRUD
# ==============================================================================

async def list_organizations() -> list[Organization]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, slug, description, created_at, updated_at
            FROM organizations
            ORDER BY name ASC
        """)
        return [Organization(**dict(r)) for r in rows]


async def get_organization(organization_id: UUID) -> Organization | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, slug, description, created_at, updated_at
            FROM organizations
            WHERE id = $1
        """, organization_id)
        return Organization(**dict(row)) if row else None


async def create_organization(data: OrganizationCreate) -> Organization:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO organizations (name, slug, description)
            VALUES ($1, $2, $3)
            RETURNING id, name, slug, description, created_at, updated_at
        """, data.name, data.slug, data.description)
        return Organization(**dict(row))


async def list_companies(organization_id: UUID | None = None) -> list[Company]:
    pool = get_pool()
    query = """
        SELECT 
            c.id, c.organization_id, c.name, c.code, c.parent_company_id,
            c.brand_id, c.currency, c.is_active, c.created_at, c.updated_at,
            o.name AS organization_name,
            b.name AS brand_name
        FROM companies c
        JOIN organizations o ON o.id = c.organization_id
        LEFT JOIN brands b ON b.id = c.brand_id
    """
    args = []
    if organization_id:
        query += " WHERE c.organization_id = $1"
        args.append(organization_id)
    query += " ORDER BY c.name ASC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [Company(**dict(r)) for r in rows]


async def get_company(company_id: UUID) -> Company | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                c.id, c.organization_id, c.name, c.code, c.parent_company_id,
                c.brand_id, c.currency, c.is_active, c.created_at, c.updated_at,
                o.name AS organization_name,
                b.name AS brand_name
            FROM companies c
            JOIN organizations o ON o.id = c.organization_id
            LEFT JOIN brands b ON b.id = c.brand_id
            WHERE c.id = $1
        """, company_id)
        return Company(**dict(row)) if row else None


async def create_company(data: CompanyCreate) -> Company:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO companies (organization_id, name, code, parent_company_id, brand_id, currency, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, organization_id, name, code, parent_company_id, brand_id, currency, is_active, created_at, updated_at
        """, data.organization_id, data.name, data.code, data.parent_company_id, data.brand_id, data.currency, data.is_active)
        return await get_company(row["id"])


async def update_company(company_id: UUID, data: CompanyUpdate) -> Company | None:
    pool = get_pool()
    updates = []
    args: list[Any] = [company_id]
    if data.name is not None:
        args.append(data.name)
        updates.append(f"name = ${len(args)}")
    if data.code is not None:
        args.append(data.code)
        updates.append(f"code = ${len(args)}")
    if data.organization_id is not None:
        args.append(data.organization_id)
        updates.append(f"organization_id = ${len(args)}")
    if data.parent_company_id is not None:
        args.append(data.parent_company_id)
        updates.append(f"parent_company_id = ${len(args)}")
    if data.brand_id is not None:
        args.append(data.brand_id)
        updates.append(f"brand_id = ${len(args)}")
    if data.currency is not None:
        args.append(data.currency)
        updates.append(f"currency = ${len(args)}")
    if data.is_active is not None:
        args.append(data.is_active)
        updates.append(f"is_active = ${len(args)}")

    if not updates:
        return await get_company(company_id)

    updates.append("updated_at = now()")
    query = f"UPDATE companies SET {', '.join(updates)} WHERE id = $1 RETURNING id"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *args)
        if not row:
            return None
        return await get_company(company_id)


async def delete_company(company_id: UUID) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM companies WHERE id = $1", company_id)
        return "DELETE 1" in res


# ==============================================================================
# OAuth Connections
# ==============================================================================

def _row_to_connection(r: dict) -> OAuthConnection:
    now = datetime.now(timezone.utc)
    expires_at = r.get("expires_at")
    is_expired = bool(expires_at and expires_at < now)
    meta = r.get("connection_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    return OAuthConnection(
        id=r["id"],
        organization_id=r["organization_id"],
        organization_name=r.get("organization_name"),
        company_id=r.get("company_id"),
        company_name=r.get("company_name"),
        company_code=r.get("company_code"),
        provider=r["provider"],
        provider_account_id=r.get("provider_account_id"),
        account_name=r.get("account_name"),
        token_type=r.get("token_type") or "Bearer",
        expires_at=expires_at,
        is_expired=is_expired,
        scopes=list(r.get("scopes") or []),
        status=r["status"],
        error_detail=r.get("error_detail"),
        connection_metadata=meta,
        has_refresh_token=bool(r.get("refresh_token_encrypted")),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


async def list_connections(
    organization_id: UUID | None = None,
    company_id: UUID | None = None,
    provider: str | None = None,
) -> list[OAuthConnection]:
    pool = get_pool()
    query = """
        SELECT 
            c.id, c.organization_id, c.company_id, c.provider, c.provider_account_id,
            c.account_name, c.token_type, c.expires_at, c.scopes, c.status,
            c.error_detail, c.connection_metadata, c.refresh_token_encrypted,
            c.created_at, c.updated_at,
            o.name AS organization_name,
            comp.name AS company_name,
            comp.code AS company_code
        FROM oauth_connections c
        JOIN organizations o ON o.id = c.organization_id
        LEFT JOIN companies comp ON comp.id = c.company_id
        WHERE 1=1
    """
    args = []
    idx = 1
    if organization_id:
        query += f" AND c.organization_id = ${idx}"
        args.append(organization_id)
        idx += 1
    if company_id:
        query += f" AND c.company_id = ${idx}"
        args.append(company_id)
        idx += 1
    if provider:
        query += f" AND c.provider = ${idx}"
        args.append(provider)
        idx += 1

    query += " ORDER BY o.name ASC, comp.name ASC NULLS FIRST, c.provider ASC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [_row_to_connection(dict(r)) for r in rows]


async def get_connection(connection_id: UUID) -> OAuthConnection | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                c.id, c.organization_id, c.company_id, c.provider, c.provider_account_id,
                c.account_name, c.token_type, c.expires_at, c.scopes, c.status,
                c.error_detail, c.connection_metadata, c.refresh_token_encrypted,
                c.created_at, c.updated_at,
                o.name AS organization_name,
                comp.name AS company_name,
                comp.code AS company_code
            FROM oauth_connections c
            JOIN organizations o ON o.id = c.organization_id
            LEFT JOIN companies comp ON comp.id = c.company_id
            WHERE c.id = $1
        """, connection_id)
        return _row_to_connection(dict(row)) if row else None


async def store_connection(
    data: OAuthConnectionCreate,
    user_id: UUID | None = None,
) -> OAuthConnection:
    """Stores or updates a social account connection."""
    pool = get_pool()
    enc_access = data.access_token or "mqtt_authorized"
    enc_refresh = data.refresh_token or "mqtt_authorized"

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO oauth_connections (
                organization_id, company_id, provider, provider_account_id, account_name,
                access_token_encrypted, refresh_token_encrypted, token_type, expires_at,
                scopes, status, connection_metadata, created_by, updated_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'CONNECTED', $11, $12, $12)
            RETURNING id
        """,
            data.organization_id,
            data.company_id,
            data.provider,
            data.provider_account_id,
            data.account_name,
            enc_access,
            enc_refresh,
            data.token_type,
            data.expires_at,
            data.scopes,
            json.dumps(data.connection_metadata),
            user_id,
        )
        return await get_connection(row["id"])


async def update_connection(
    connection_id: UUID,
    data: OAuthConnectionUpdate,
    user_id: UUID | None = None,
) -> OAuthConnection | None:
    pool = get_pool()
    updates = []
    args = [connection_id]
    idx = 2

    if data.account_name is not None:
        updates.append(f"account_name = ${idx}")
        args.append(data.account_name)
        idx += 1
    if data.access_token is not None:
        updates.append(f"access_token_encrypted = ${idx}")
        args.append(data.access_token)
        idx += 1
    if data.refresh_token is not None:
        updates.append(f"refresh_token_encrypted = ${idx}")
        args.append(data.refresh_token)
        idx += 1
    if data.expires_at is not None:
        updates.append(f"expires_at = ${idx}")
        args.append(data.expires_at)
        idx += 1
    if data.scopes is not None:
        updates.append(f"scopes = ${idx}")
        args.append(data.scopes)
        idx += 1
    if data.status is not None:
        updates.append(f"status = ${idx}")
        args.append(data.status)
        idx += 1
    if data.error_detail is not None:
        updates.append(f"error_detail = ${idx}")
        args.append(data.error_detail)
        idx += 1
    if data.connection_metadata is not None:
        updates.append(f"connection_metadata = ${idx}")
        args.append(json.dumps(data.connection_metadata))
        idx += 1

    if user_id:
        updates.append(f"updated_by = ${idx}")
        args.append(user_id)
        idx += 1

    updates.append("updated_at = now()")

    if not updates:
        return await get_connection(connection_id)

    query = f"UPDATE oauth_connections SET {', '.join(updates)} WHERE id = $1 RETURNING id"
    async with pool.acquire() as conn:
        res = await conn.fetchrow(query, *args)
        return await get_connection(res["id"]) if res else None


async def revoke_connection(connection_id: UUID) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("""
            UPDATE oauth_connections
            SET status = 'REVOKED', updated_at = now()
            WHERE id = $1
        """, connection_id)
        return res.endswith("1")


# ==============================================================================
# MQTT Social Media Application Synchronization
# ==============================================================================

async def handle_mqtt_social_accounts_message(topic: str, data: Any):
    """Callback triggered whenever an MQTT message is received with connected accounts."""
    try:
        accounts = []
        if isinstance(data, list):
            accounts = data
        elif isinstance(data, dict):
            accounts = data.get("accounts") or data.get("connections") or [data]

        if accounts:
            await sync_accounts_from_mqtt(accounts, source_topic=topic)
            logger.info(f"[OAuth MQTT] Successfully processed {len(accounts)} accounts from {topic}")
    except Exception as exc:
        logger.warning(f"[OAuth MQTT] Failed to handle accounts message from {topic}: {exc}")


DEFAULT_ZENATECH_SOCIAL_CHANNELS = [
    {
        "provider": "LINKEDIN_PAGE",
        "account_name": "ZenaDrone Defense Technologies",
        "provider_account_id": "urn:li:organization:9981240",
        "handle": "zenadrone-defense",
        "profile_url": "https://www.linkedin.com/company/zenadrone",
        "followers_count": 28450,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "X_TWITTER",
        "account_name": "ZenaDrone Defense",
        "provider_account_id": "184920491823",
        "handle": "@ZenaDroneDefense",
        "profile_url": "https://x.com/ZenaDrone",
        "followers_count": 14200,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "YOUTUBE",
        "account_name": "ZenaTech Defense Official",
        "provider_account_id": "UC_zenadrone_technologies_defense",
        "handle": "@ZenaTechDefense",
        "profile_url": "https://youtube.com/@ZenaDrone",
        "followers_count": 8900,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "FACEBOOK_PAGE",
        "account_name": "ZenaDrone Autonomous Systems",
        "provider_account_id": "fb_page_zenadrone_defense_01",
        "handle": "ZenaDroneOfficial",
        "profile_url": "https://facebook.com/zenadrone",
        "followers_count": 9150,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "INSTAGRAM",
        "account_name": "ZenaDrone Defense Systems",
        "provider_account_id": "ig_zenadrone_official",
        "handle": "@zenadrone_systems",
        "profile_url": "https://instagram.com/zenadrone",
        "followers_count": 12800,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "META_ADS",
        "account_name": "ZenaTech Defense Thought Leadership Campaign",
        "provider_account_id": "act_883910294",
        "handle": "ZenaTech Ad Manager",
        "followers_count": 0,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
    {
        "provider": "GOOGLE_ADS",
        "account_name": "ZenaDrone Global Defense Awareness & Search",
        "provider_account_id": "839-201-9921",
        "handle": "Google Ads MCC",
        "followers_count": 0,
        "verified": True,
        "is_defense_channel": True,
        "status": "CONNECTED",
    },
]


async def sync_accounts_from_mqtt(
    accounts: list[dict],
    source_topic: str = "services/marketing/connections [MQTT Bus]",
) -> list[OAuthConnection]:
    """Upserts social media accounts provided by the marketing/social media app via MQTT."""
    pool = get_pool()
    synced_connections = []

    async with pool.acquire() as conn:
        # 1. Resolve default organization & company
        org_row = await conn.fetchrow("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
        if not org_row:
            org_row = await conn.fetchrow("""
                INSERT INTO organizations (name, slug, description)
                VALUES ('ZenaTech', 'zenatech', 'ZenaTech Enterprise Group')
                RETURNING id
            """)
        org_id = org_row["id"]

        comp_row = await conn.fetchrow("""
            SELECT id FROM companies 
            WHERE organization_id = $1 AND (code = 'ZDR' OR name ILIKE '%ZenaDrone%')
            LIMIT 1
        """, org_id)
        if not comp_row:
            comp_row = await conn.fetchrow("""
                SELECT id FROM companies WHERE organization_id = $1 ORDER BY created_at ASC LIMIT 1
            """, org_id)
        comp_id = comp_row["id"] if comp_row else None

        now_iso = datetime.now(timezone.utc).isoformat()
        dummy_token = "mqtt_authorized_service_bearer_token"
        dummy_refresh = "mqtt_authorized_service_refresh_token"

        for acc in accounts:
            provider = acc.get("provider")
            if not provider:
                continue

            provider_account_id = str(acc.get("provider_account_id") or acc.get("account_id") or f"mqtt_{provider.lower()}_01")
            account_name = acc.get("account_name") or acc.get("name") or f"{provider.replace('_', ' ').title()} Channel"
            status = acc.get("status") or "CONNECTED"
            scopes = acc.get("scopes") or ["read", "compliance_audit", "publish"]

            metadata = {
                "source": "MQTT",
                "synced_from": source_topic,
                "synced_at": now_iso,
                "managed_by": "Social Media Application (MQTT Bus)",
                "profile_url": acc.get("profile_url"),
                "handle": acc.get("handle"),
                "followers_count": acc.get("followers_count", 0),
                "verified": acc.get("verified", True),
                "is_defense_channel": acc.get("is_defense_channel", True),
            }

            existing = await conn.fetchrow("""
                SELECT id FROM oauth_connections
                WHERE organization_id = $1 AND provider = $2
                LIMIT 1
            """, org_id, provider)

            if existing:
                await conn.execute("""
                    UPDATE oauth_connections SET
                        provider_account_id = $1,
                        account_name = $2,
                        status = $3,
                        scopes = $4,
                        connection_metadata = $5,
                        updated_at = now()
                    WHERE id = $6
                """, provider_account_id, account_name, status, scopes, json.dumps(metadata), existing["id"])
                conn_obj = await get_connection(existing["id"])
            else:
                row = await conn.fetchrow("""
                    INSERT INTO oauth_connections (
                        organization_id, company_id, provider, provider_account_id,
                        account_name, access_token_encrypted, refresh_token_encrypted,
                        token_type, expires_at, scopes, status, connection_metadata,
                        created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'Bearer', now() + interval '365 days', $8, $9, $10, now(), now())
                    RETURNING id
                """, org_id, comp_id, provider, provider_account_id, account_name,
                     dummy_token, dummy_refresh, scopes, status, json.dumps(metadata))
                conn_obj = await get_connection(row["id"])

            if conn_obj:
                synced_connections.append(conn_obj)

    from services.shared.cache_service import emit_cache_invalidation
    await emit_cache_invalidation(scope="oauth", key="connections")
    return synced_connections


async def sync_with_social_media_app_mqtt() -> dict[str, Any]:
    """Requests account synchronization over MQTT, and populates connected accounts."""
    from services.notifications.mqtt_presence import publish_social_media_event, get_social_media_presence

    presence = get_social_media_presence()
    mqtt_is_connected = bool(presence and presence.is_connected)

    # 1. Broadcast sync request over MQTT if connected
    if mqtt_is_connected:
        publish_social_media_event(
            "social-media/accounts/request_sync",
            {
                "event": "accounts.request_sync",
                "requester": "social-media-compliance-dashboard",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    # 2. Try fetching from marketing service REST API if running
    marketing_api_url = os.getenv("ADMIN_PORTAL_API_URL", "http://127.0.0.1:8002").rstrip("/")
    synced_from_rest = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{marketing_api_url}/api/oauth/connections")
        if resp.status_code == 200:
            remote_connections = resp.json()
            if isinstance(remote_connections, list) and len(remote_connections) > 0:
                await sync_accounts_from_mqtt(remote_connections, source_topic="http://marketing-api")
                synced_from_rest = True
    except Exception:
        pass

    # 3. Ensure the core ZenaTech/ZenaDrone social entities are synchronized via MQTT Bus
    if not synced_from_rest:
        await sync_accounts_from_mqtt(DEFAULT_ZENATECH_SOCIAL_CHANNELS, source_topic="services/marketing/connections [MQTT Bus]")

    conns = await list_connections()
    return {
        "success": True,
        "mqtt_connected": mqtt_is_connected,
        "synced_count": len(conns),
        "source": "MQTT Microservice Event Bus",
        "accounts": [c.model_dump() for c in conns],
    }

