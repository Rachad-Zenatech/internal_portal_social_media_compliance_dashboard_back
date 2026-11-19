"""Role hierarchy and audit columns for new and existing databases."""


async def setup_roles(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(100) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            is_system_role BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            parent_role_id UUID REFERENCES roles(id) ON DELETE SET NULL,
            display_order INT DEFAULT 0,
            department VARCHAR(255),
            deleted_at TIMESTAMP WITH TIME ZONE,
            deleted_by UUID,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            created_by UUID,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_by UUID
        );
        ALTER TABLE roles ADD COLUMN IF NOT EXISTS created_by UUID;
        ALTER TABLE roles ADD COLUMN IF NOT EXISTS updated_by UUID;
        ALTER TABLE roles ADD COLUMN IF NOT EXISTS deleted_by UUID;
        CREATE INDEX IF NOT EXISTS idx_roles_code ON roles(code);
    """)
