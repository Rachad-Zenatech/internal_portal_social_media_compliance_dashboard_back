"""Exercise manager visibility against PostgreSQL without modifying application records."""

import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg

from services.compliance.submissions import submission_service as subs
from services.compliance.submissions.review_hierarchy_service import manager_access_sql, reviewer_sees


@unittest.skipUnless(os.getenv("DATABASE_URL"), "Requires PostgreSQL DATABASE_URL")
class ReviewHierarchyQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_descendant_assignments_are_visible_only_to_active_managers(self):
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("""
                CREATE TEMP TABLE roles (id UUID PRIMARY KEY, code TEXT, parent_role_id UUID,
                    is_active BOOLEAN DEFAULT true, deleted_at TIMESTAMPTZ);
                CREATE TEMP TABLE users (id UUID PRIMARY KEY, is_active BOOLEAN DEFAULT true, deleted_at TIMESTAMPTZ);
                CREATE TEMP TABLE user_roles (user_id UUID, role_id UUID, is_active BOOLEAN DEFAULT true);
                CREATE TEMP TABLE content_submissions (id UUID, reviewer_ids UUID[], brand_codes TEXT[]);
            """)
            manager_role, reviewer_role, nested_role, unrelated_role = [uuid4() for _ in range(4)]
            manager, reviewer, nested, outsider = [uuid4() for _ in range(4)]
            await conn.executemany("INSERT INTO roles (id, code, parent_role_id) VALUES ($1,$2,$3)", [
                (manager_role, "MANAGER", None), (reviewer_role, "REVIEWER", manager_role),
                (nested_role, "NESTED", reviewer_role), (unrelated_role, "OTHER", None),
            ])
            await conn.executemany("INSERT INTO users (id) VALUES ($1)", [(u,) for u in (manager, reviewer, nested, outsider)])
            await conn.executemany("INSERT INTO user_roles (user_id, role_id) VALUES ($1,$2)", [
                (manager, manager_role), (reviewer, reviewer_role), (nested, nested_role), (outsider, unrelated_role),
            ])
            post, other_post = uuid4(), uuid4()
            await conn.executemany("INSERT INTO content_submissions VALUES ($1,$2,$3)", [
                (post, [reviewer, nested], ["BRAND_A"]), (other_post, [outsider], ["BRAND_A"]),
            ])
            query = f"SELECT s.id FROM content_submissions s WHERE {reviewer_sees('$1', '$2')}"
            visible = await conn.fetch(query, manager, ["DIFFERENT_BRAND"])
            self.assertEqual([r["id"] for r in visible], [post])
            self.assertEqual([r["id"] for r in await conn.fetch(query, outsider, ["DIFFERENT_BRAND"])], [other_post])
            self.assertEqual([r["id"] for r in await conn.fetch(query, reviewer, None)], [post])
            await conn.execute("UPDATE user_roles SET is_active=false WHERE user_id=$1", manager)
            self.assertFalse(await conn.fetch(query, manager, None))
            await conn.execute("UPDATE user_roles SET is_active=true WHERE user_id=$1", manager)
            await conn.execute("UPDATE roles SET parent_role_id=NULL WHERE id=$1", reviewer_role)
            self.assertFalse(await conn.fetch(query, manager, None))
            # Queries also expose the same flag used by approval controls.
            self.assertFalse(await conn.fetchval(f"SELECT {manager_access_sql('$1')} FROM content_submissions s WHERE id=$2", manager, post))
        finally:
            await conn.close()


class ManagerApprovalFieldsTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_can_open_assigned_content_across_brand_scopes(self):
        manager, reviewer = uuid4(), uuid4()
        row = {"id": uuid4(), "created_by": uuid4(), "brand_codes": ["A"],
               "reviewer_ids": [reviewer], "manager_review_access": True}
        with (
            patch.object(subs, "fetch_one", AsyncMock(return_value=row)),
            patch.object(subs, "_is_super_admin", AsyncMock(return_value=False)),
            patch.object(subs, "_can_see_team_content", AsyncMock(return_value=True)),
            patch.object(subs, "_brand_scope", AsyncMock(return_value=["OTHER"])),
        ):
            self.assertIs(await subs._get_owned_row(row["id"], manager, manager_can_read=True), row)

    async def test_manager_approval_and_delegation_controls_are_enabled(self):
        manager = uuid4()
        row = {"status": "IN_REVIEW", "created_by": uuid4(), "manager_review_access": True}
        with patch.object(subs, "_approvers", return_value=([uuid4()], ["Reviewer"])):
            fields = subs._approval_fields(None, row, manager, True, False)
            self.assertTrue(fields["can_approve"])
            self.assertTrue(fields["can_delegate"])
            own = subs._approval_fields(None, {**row, "created_by": manager}, manager, True, False)
            self.assertFalse(own["can_approve"])
            self.assertFalse(own["can_delegate"])
