"""
Setup Social Media & Multi-Entity OAuth Connection Schema
Initializes brands, organizations, companies, and oauth_connections.
"""
import asyncio
import logging
from postgresql_db.database import get_pool, close_pool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def setup():
    pool = get_pool()
    async with pool.acquire() as conn:
        logger.info("Setting up brands, organizations, companies, and oauth_connections schema...")

        await conn.execute("""
            -- Brands Table
            CREATE TABLE IF NOT EXISTS brands (
                id SERIAL PRIMARY KEY,
                code VARCHAR(50) UNIQUE NOT NULL,
                name VARCHAR(100) NOT NULL,
                is_active BOOLEAN DEFAULT true,
                sort_order INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Organizations Table
            CREATE TABLE IF NOT EXISTS organizations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(255) NOT NULL,
                slug VARCHAR(100) UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );

            -- Companies Table
            CREATE TABLE IF NOT EXISTS companies (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                code VARCHAR(50) NOT NULL,
                parent_company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL,
                currency VARCHAR(3) DEFAULT 'USD' NOT NULL,
                is_active BOOLEAN DEFAULT true NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                CONSTRAINT companies_org_code_key UNIQUE (organization_id, code)
            );

            CREATE INDEX IF NOT EXISTS idx_companies_organization ON companies(organization_id);
            CREATE INDEX IF NOT EXISTS idx_companies_brand ON companies(brand_id);

            -- OAuth Connections Table
            CREATE TABLE IF NOT EXISTS oauth_connections (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
                provider VARCHAR(50) NOT NULL,
                provider_account_id VARCHAR(255),
                account_name VARCHAR(255),
                access_token_encrypted TEXT NOT NULL,
                refresh_token_encrypted TEXT,
                token_type VARCHAR(50) DEFAULT 'Bearer' NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE,
                scopes TEXT[] DEFAULT '{}'::text[],
                status VARCHAR(50) DEFAULT 'CONNECTED' NOT NULL,
                error_detail TEXT,
                connection_metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_oauth_connections_org_company ON oauth_connections(organization_id, company_id);
            CREATE INDEX IF NOT EXISTS idx_oauth_connections_provider ON oauth_connections(provider);
            CREATE INDEX IF NOT EXISTS idx_oauth_connections_status ON oauth_connections(status);
        """)

        # Seed Default Brands (the full list of five, and retiring the old product brands, is in setup_submissions_schema)
        logger.info("Seeding default brands...")
        await conn.execute("""
            INSERT INTO brands (code, name, sort_order)
            VALUES
                ('ZENATECH', 'ZenaTech', 10),
                ('ZENADRONE', 'ZenaDrone', 20)
            ON CONFLICT (code) DO NOTHING;
        """)

        # Seed Default Organization
        logger.info("Seeding default organization...")
        org_id = await conn.fetchval("""
            INSERT INTO organizations (name, slug, description)
            VALUES ('ZenaTech', 'zenatech', 'ZenaTech Global Social Media & Compliance Ecosystem')
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id;
        """)

        # Seed Default Companies
        if org_id:
            logger.info("Seeding default companies...")
            await conn.execute("""
                INSERT INTO companies (organization_id, name, code, currency)
                VALUES 
                    ($1, 'ZenaTech Group', 'ZTG', 'USD'),
                    ($1, 'DeskFlex Inc', 'DFX', 'USD'),
                    ($1, 'Othership Work', 'OSH', 'USD'),
                    ($1, 'ZenaDrone Operations', 'ZDR', 'USD')
                ON CONFLICT (organization_id, code) DO NOTHING;
            """, org_id)

        logger.info("Social media & OAuth schema setup successfully completed!")


async def _main():
    from postgresql_db.database import create_pool
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
