"""Social Media Channels & Integrations Router

Manages Organization -> Company -> Social Media Channel Connections.
Synchronizes channels and status directly from the Social Media application via MQTT event bus.
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.shared.oauth_connection_model import (
    Company,
    CompanyCreate,
    CompanyUpdate,
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthProvider,
    Organization,
    OrganizationCreate,
)
from services.shared import oauth_connection_service as oauth_svc
from services.shared.audit_service import log_audit_event
from services.shared.auth_service import get_current_user_id_dependency, require_permission
from services.shared.cache_service import emit_cache_invalidation

router = APIRouter(prefix="/oauth", tags=["Social Media Channels (MQTT)"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
AdminViewUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_VIEW"))]
AdminUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]



# ==============================================================================
# Organizations
# ==============================================================================

@router.get("/organizations", response_model=list[Organization])
async def list_organizations(_user_id: AdminViewUser):
    """List all top-level organizations (e.g. ZenaTech Group)."""
    return await oauth_svc.list_organizations()


@router.get("/organizations/{organization_id}", response_model=Organization)
async def get_organization(organization_id: UUID, _user_id: AdminViewUser):
    org = await oauth_svc.get_organization(organization_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


@router.post("/organizations", response_model=Organization, status_code=status.HTTP_201_CREATED)
async def create_organization(data: OrganizationCreate, user_id: AdminUser):
    try:
        org = await oauth_svc.create_organization(data)
        await log_audit_event("Organization", str(org.id), "CREATE", user_id, {"name": org.name, "slug": org.slug})
        return org
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ==============================================================================
# Companies / Entities / Subsidiaries
# ==============================================================================

@router.get("/companies", response_model=list[Company])
async def list_companies(
    _user_id: AdminViewUser,
    organization_id: UUID | None = Query(None, description="Filter by parent organization"),
):
    """List companies/entities (e.g. ZenaTech USA, LE, ZD, Othership, DeskFlex, WorkAware)."""
    return await oauth_svc.list_companies(organization_id)


@router.get("/companies/{company_id}", response_model=Company)
async def get_company(company_id: UUID, _user_id: AdminViewUser):
    comp = await oauth_svc.get_company(company_id)
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return comp


@router.post("/companies", response_model=Company, status_code=status.HTTP_201_CREATED)
async def create_company(data: CompanyCreate, user_id: AdminUser):
    try:
        comp = await oauth_svc.create_company(data)
        await log_audit_event("Company", str(comp.id), "CREATE", user_id, {"name": comp.name, "code": comp.code, "organization_id": str(comp.organization_id)})
        await emit_cache_invalidation(scope="master_data")
        return comp
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.put("/companies/{company_id}", response_model=Company)
async def update_company(company_id: UUID, data: CompanyUpdate, user_id: AdminUser):
    try:
        comp = await oauth_svc.update_company(company_id, data)
        if not comp:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
        await log_audit_event("Company", str(comp.id), "UPDATE", user_id, {"name": comp.name, "code": comp.code})
        await emit_cache_invalidation(scope="master_data")
        return comp
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(company_id: UUID, user_id: AdminUser):
    try:
        success = await oauth_svc.delete_company(company_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
        await log_audit_event("Company", str(company_id), "DELETE", user_id, {})
        await emit_cache_invalidation(scope="master_data")
        return None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ==============================================================================
# OAuth Connections
# ==============================================================================

@router.get("/connections", response_model=list[OAuthConnection])
async def list_connections(
    _user_id: AdminViewUser,
    organization_id: UUID | None = Query(None),
    company_id: UUID | None = Query(None),
    provider: OAuthProvider | None = Query(None),
):
    """List registered OAuth connections with stripped/safe token credentials (requires administrative permission)."""
    return await oauth_svc.list_connections(organization_id, company_id, provider)


@router.get("/connections/{connection_id}", response_model=OAuthConnection)
async def get_connection(connection_id: UUID, _user_id: AdminViewUser):
    conn = await oauth_svc.get_connection(connection_id)
    if not conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OAuth connection not found")
    return conn


@router.post("/connections", response_model=OAuthConnection, status_code=status.HTTP_201_CREATED)
async def store_connection(data: OAuthConnectionCreate, user_id: AdminUser):
    """Store a newly authorized OAuth connection (encrypts tokens before saving)."""
    try:
        conn = await oauth_svc.store_connection(data, user_id=user_id)
        await log_audit_event(
            "OAuthConnection",
            str(conn.id),
            "CREATE",
            user_id,
            {
                "provider": conn.provider,
                "account_name": conn.account_name,
                "provider_account_id": conn.provider_account_id,
                "scope_level": "COMPANY" if conn.company_id else "ORGANIZATION",
                "organization_id": str(conn.organization_id),
                "company_id": str(conn.company_id) if conn.company_id else None,
            },
        )
        return conn
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/connections/{connection_id}/refresh", response_model=OAuthConnection)
async def refresh_connection(connection_id: UUID, user_id: AdminUser):
    """Refreshes connection status from the Social Media application via MQTT."""
    conn = await oauth_svc.get_connection(connection_id)
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connection not found"
        )
    await oauth_svc.sync_with_social_media_app_mqtt()
    await log_audit_event("OAuthConnection", str(connection_id), "SYNC_REFRESH", user_id, {"connection_id": str(connection_id)})
    return await oauth_svc.get_connection(connection_id)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_connection(connection_id: UUID, user_id: AdminUser):
    """Disconnects and revokes an OAuth connection."""
    success = await oauth_svc.revoke_connection(connection_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    await log_audit_event("OAuthConnection", str(connection_id), "REVOKE", user_id, {"connection_id": str(connection_id)})
    return None


# ==============================================================================
# MQTT Social Media Application Synchronization Endpoints
# ==============================================================================

@router.post("/sync-mqtt")
async def sync_accounts_via_mqtt(_user_id: CurrentUser):
    """Synchronizes social media accounts from the Social Media / Marketing application via MQTT event bus."""
    res = await oauth_svc.sync_with_social_media_app_mqtt()
    return res


@router.get("/mqtt-status")
async def get_mqtt_sync_status(_user_id: CurrentUser):
    """Returns the current MQTT bus connection and synchronization status."""
    from services.notifications.mqtt_presence import get_social_media_presence
    presence = get_social_media_presence()
    return {
        "mqtt_connected": bool(presence and presence.is_connected),
        "service": presence.service_name if presence else "social-media",
        "instance_id": presence.instance_id if presence else "social-media-01",
        "broker": f"{presence.host}:{presence.port}" if presence else "127.0.0.1:1883",
        "subscribed_topics": [
            "services/marketing/connections",
            "services/social-media/connections",
            "social-media/accounts/sync",
            "social-media/accounts/response",
        ],
    }

