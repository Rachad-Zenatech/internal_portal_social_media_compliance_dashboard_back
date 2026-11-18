"""Exercise hierarchy saves against a legacy roles table in temporary PostgreSQL storage."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from models.shared.rbac_model import RoleUpdate
from postgresql_db.schemas.roles_schema import setup_roles
from services.shared.auth import rbac_service


@unittest.skipUnless(os.getenv("DATABASE_URL"), "Requires a PostgreSQL DATABASE_URL")
class RoleHierarchySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_roles_migrate_and_hierarchy_save_records_actor(self):
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            # Temp tables shadow the application's roles table without changing its data.
            await conn.execute("""
                CREATE TEMP TABLE roles (
                    id UUID PRIMARY KEY, code TEXT UNIQUE, name TEXT,
                    parent_role_id UUID REFERENCES roles(id),
                    deleted_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
                );
            """)
            await setup_roles(conn)
            await setup_roles(conn)
            manager_id, reviewer_id, actor_id = uuid4(), uuid4(), uuid4()
            await conn.executemany(
                "INSERT INTO roles (id, code, name) VALUES ($1, $2, $3)",
                [(manager_id, "MANAGER", "Content Manager"),
                 (reviewer_id, "REVIEWER", "Content Reviewer")],
            )
            pool = MagicMock()
            pool.acquire.return_value.__aenter__.return_value = conn
            with (
                patch.object(rbac_service, "get_pool", return_value=pool),
                patch.object(rbac_service, "log_audit", new_callable=AsyncMock),
                patch.object(rbac_service, "emit_cache_invalidation", new_callable=AsyncMock),
            ):
                role = await rbac_service.update_role(
                    reviewer_id, RoleUpdate(parent_role_id=manager_id), actor_id=actor_id,
                )
            self.assertEqual(role["parent_role_id"], manager_id)
            self.assertEqual(role["updated_by"], actor_id)
            self.assertIsNotNone(role["updated_at"])
            self.assertIn("created_by", role)
            self.assertIn("deleted_by", role)
        finally:
            await conn.close()
