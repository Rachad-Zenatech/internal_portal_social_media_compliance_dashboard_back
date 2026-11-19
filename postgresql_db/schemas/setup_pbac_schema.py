import asyncio
import os
from dotenv import load_dotenv
from postgresql_db.schemas.roles_schema import setup_roles
from postgresql_db.schemas.pbac_seed import (
    seed_navigation, seed_permission_groups, seed_roles, seed_super_admins,
)
from postgresql_db.database import create_pool, close_pool, get_pool

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env'))

async def _setup_users(conn):
    await conn.execute("""
            -- Extensions
            CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
            CREATE EXTENSION IF NOT EXISTS "pgcrypto";

            -- Users
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email VARCHAR(255) UNIQUE NOT NULL,
                full_name VARCHAR(255),
                department VARCHAR(255),
                job_title VARCHAR(255),
                auth_provider VARCHAR(50) DEFAULT 'microsoft',
                entra_oid VARCHAR(255) UNIQUE,
                microsoft_tenant_id VARCHAR(255),
                microsoft_object_id VARCHAR(255),
                microsoft_subject_id VARCHAR(255),
                sso_enabled BOOLEAN DEFAULT false,
                graph_account_enabled BOOLEAN,
                last_graph_sync TIMESTAMP WITH TIME ZONE,
                last_sso_login_at TIMESTAMP WITH TIME ZONE,
                force_password_change BOOLEAN DEFAULT false,
                is_active BOOLEAN DEFAULT TRUE,
                is_super_admin BOOLEAN DEFAULT FALSE,
                last_login_at TIMESTAMP WITH TIME ZONE,
                last_activity_at TIMESTAMP WITH TIME ZONE,
                last_logout_at TIMESTAMP WITH TIME ZONE,
                deleted_at TIMESTAMP WITH TIME ZONE,
                deleted_by UUID,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_by UUID
            );
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);

            -- Ensure all Microsoft SSO columns exist on pre-existing users tables
            ALTER TABLE users ADD COLUMN IF NOT EXISTS microsoft_tenant_id VARCHAR(255);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS microsoft_object_id VARCHAR(255);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS microsoft_subject_id VARCHAR(255);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS sso_enabled BOOLEAN DEFAULT false;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS graph_account_enabled BOOLEAN;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_graph_sync TIMESTAMP WITH TIME ZONE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sso_login_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS force_password_change BOOLEAN DEFAULT false;
            CREATE INDEX IF NOT EXISTS idx_users_microsoft_tenant_object ON users(microsoft_tenant_id, microsoft_object_id);

    """)


async def _setup_navigation(conn):
    await conn.execute("""
            -- User Roles
            CREATE TABLE IF NOT EXISTS user_roles (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                is_active BOOLEAN DEFAULT TRUE,
                assigned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                assigned_by UUID REFERENCES users(id) ON DELETE SET NULL,
                UNIQUE(user_id, role_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_roles_user_id ON user_roles(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON user_roles(role_id);

            -- Navigation Items
            CREATE TABLE IF NOT EXISTS navigation_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                route_path VARCHAR(255),
                parent_code VARCHAR(100),
                display_order INT DEFAULT 0,
                icon VARCHAR(100),
                is_menu_item BOOLEAN DEFAULT TRUE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Permission Actions
            CREATE TABLE IF NOT EXISTS permission_actions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role Navigation Permissions
            CREATE TABLE IF NOT EXISTS role_navigation_permissions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                navigation_item_id UUID NOT NULL REFERENCES navigation_items(id) ON DELETE CASCADE,
                action_id UUID NOT NULL REFERENCES permission_actions(id) ON DELETE CASCADE,
                is_allowed BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, navigation_item_id, action_id)
            );

    """)


async def _setup_permission_groups(conn):
    await conn.execute("""
            -- PBAC Permission Modules
            CREATE TABLE IF NOT EXISTS permission_modules (
                id SERIAL PRIMARY KEY,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- PBAC Permission Groups
            CREATE TABLE IF NOT EXISTS permission_groups (
                id SERIAL PRIMARY KEY,
                module_id INT REFERENCES permission_modules(id) ON DELETE CASCADE,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- PBAC Permission Group Actions
            CREATE TABLE IF NOT EXISTS permission_group_actions (
                id SERIAL PRIMARY KEY,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                api_module_code VARCHAR(255) NOT NULL,
                action VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role PBAC Group Mappings
            CREATE TABLE IF NOT EXISTS role_permission_groups (
                id SERIAL PRIMARY KEY,
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, permission_group_id)
            );

    """)


async def _setup_mcp_permissions(conn):
    await conn.execute("""
            -- MCP Tools
            CREATE TABLE IF NOT EXISTS mcp_tools (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                server_name VARCHAR(100) NOT NULL,
                description TEXT,
                is_read_only BOOLEAN DEFAULT FALSE,
                is_sensitive BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            -- Role MCP Tool Permissions
            CREATE TABLE IF NOT EXISTS role_mcp_tool_permissions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                mcp_tool_id UUID NOT NULL REFERENCES mcp_tools(id) ON DELETE CASCADE,
                is_allowed BOOLEAN DEFAULT TRUE,
                access_level VARCHAR(50) DEFAULT 'ALLOW',
                conditions JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, mcp_tool_id)
            );

    """)


async def _setup_activity(conn):
    await conn.execute("""
            -- Audit Logs
            CREATE TABLE IF NOT EXISTS audit_logs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                action VARCHAR(100) NOT NULL,
                entity_type VARCHAR(100) NOT NULL,
                entity_id VARCHAR(100),
                old_value JSONB,
                new_value JSONB,
                ip_address INET,
                user_agent TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);

            -- Login Activity Logs
            CREATE TABLE IF NOT EXISTS login_activity_logs (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                success BOOLEAN NOT NULL,
                failure_reason TEXT,
                ip_address VARCHAR(100),
                user_agent TEXT,
                logout_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_login_activity_logs_user_id ON login_activity_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_login_activity_logs_created_at ON login_activity_logs(created_at DESC);

            -- Notifications (Real-time SSE and in-app alerts)
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                type VARCHAR(100) NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                link_url VARCHAR(500),
                entity_type VARCHAR(100),
                entity_id VARCHAR(100),
                sender_name VARCHAR(255),
                sender_avatar VARCHAR(500),
                attachments JSONB,
                is_read BOOLEAN DEFAULT FALSE,
                read_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read);

            -- App Settings
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(100) PRIMARY KEY,
                settings_json TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
    """)


async def setup():
    pool = get_pool()
    async with pool.acquire() as conn:
        print("Creating core RBAC & Template schema tables...")
        await _setup_users(conn)
        await setup_roles(conn)
        await _setup_navigation(conn)
        await _setup_permission_groups(conn)
        await _setup_mcp_permissions(conn)
        await _setup_activity(conn)
        await seed_navigation(conn)
        groups_data, new_group_codes = await seed_permission_groups(conn)
        await seed_roles(conn, groups_data, new_group_codes)
        await seed_super_admins(conn)
        print("RBAC and template schema initialization complete!")


async def _main():
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
