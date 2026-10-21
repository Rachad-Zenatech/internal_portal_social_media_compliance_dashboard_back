"""Pydantic models for Organization, Company, and OAuth connections."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

OAuthProvider = Literal[
    # Paid Ads
    "GOOGLE_ADS",
    "MICROSOFT_ADS",
    "META_ADS",
    "LINKEDIN_ADS",
    # Organic Social Media
    "LINKEDIN_PAGE",
    "X_TWITTER",
    "FACEBOOK_PAGE",
    "INSTAGRAM",
    "YOUTUBE",
    "TIKTOK",
    # Enterprise Services
    "QUICKBOOKS",
    "ONEDRIVE",
]

OAuthStatus = Literal["CONNECTED", "EXPIRED", "REVOKED", "ERROR", "NEEDS_REAUTH"]


class OrganizationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100)
    description: str | None = None


class OrganizationCreate(OrganizationBase):
    pass


class Organization(OrganizationBase):
    id: UUID
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class CompanyBase(BaseModel):
    organization_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=1, max_length=50)
    parent_company_id: UUID | None = None
    brand_id: int | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    is_active: bool = True


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    organization_id: UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    code: str | None = Field(None, min_length=1, max_length=50)
    parent_company_id: UUID | None = None
    brand_id: int | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)
    is_active: bool | None = None


class Company(CompanyBase):
    id: UUID
    organization_name: str | None = None
    brand_name: str | None = None
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class OAuthConnectionCreate(BaseModel):
    organization_id: UUID
    company_id: UUID | None = None  # None = organization-wide fallback
    provider: OAuthProvider
    provider_account_id: str | None = None
    account_name: str | None = None
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)


class OAuthConnectionUpdate(BaseModel):
    account_name: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] | None = None
    status: OAuthStatus | None = None
    error_detail: str | None = None
    connection_metadata: dict[str, Any] | None = None


class OAuthConnection(BaseModel):
    """Safe connection model for public/API responses (secret tokens stripped)."""
    id: UUID
    organization_id: UUID
    organization_name: str | None = None
    company_id: UUID | None = None
    company_name: str | None = None
    company_code: str | None = None
    provider: OAuthProvider
    provider_account_id: str | None = None
    account_name: str | None = None
    token_type: str
    expires_at: datetime | None = None
    is_expired: bool = False
    scopes: list[str] = Field(default_factory=list)
    status: OAuthStatus
    error_detail: str | None = None
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    has_refresh_token: bool = False
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
