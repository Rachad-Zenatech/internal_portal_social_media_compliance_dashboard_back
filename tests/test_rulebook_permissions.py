"""Rulebook editing is an assignable permission independent of role administration."""

import ast
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock

from postgresql_db.schemas.pbac_seed import seed_navigation, seed_permission_groups, seed_roles


ROOT = Path(__file__).resolve().parents[1]


class RulebookPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_rulebook_is_listed_with_its_own_action(self):
        conn = AsyncMock()
        conn.transaction = MagicMock()
        conn.fetchrow.return_value = {"id": 17, "inserted": True}

        groups, created = await seed_permission_groups(conn)

        rulebook = next(group for group in groups["CONTENT"] if group[0] == "CONTENT_EDIT_RULEBOOK")
        self.assertEqual(rulebook[1], "Edit Rulebook")
        self.assertEqual(rulebook[4], [("CONTENT_RULEBOOK", "UPDATE")])
        self.assertIn("CONTENT_EDIT_RULEBOOK", created)
        self.assertTrue(any(
            call.args[2:] == (["CONTENT_RULEBOOK"], ["UPDATE"])
            for call in conn.execute.await_args_list
            if "INSERT INTO permission_group_actions" in call.args[0]
        ))

    async def test_rulebook_navigation_is_registered_without_a_menu_item(self):
        conn = AsyncMock()
        await seed_navigation(conn)
        rulebook = next(
            call.args for call in conn.execute.await_args_list
            if len(call.args) > 1 and call.args[1] == "CONTENT_RULEBOOK"
        )
        self.assertFalse(rulebook[-1])

    async def test_only_admin_gets_new_rulebook_grant_by_default(self):
        conn = AsyncMock()
        conn.fetchval.return_value = False
        groups = {"CONTENT": [("CONTENT_EDIT_RULEBOOK",)]}
        await seed_roles(conn, groups, {"CONTENT_EDIT_RULEBOOK"})
        grants = [call.args[1:] for call in conn.execute.await_args_list if len(call.args) > 1]
        self.assertEqual(grants, [("ADMIN", ["CONTENT_EDIT_RULEBOOK"])])
        self.assertIn("r.code = 'SUPER_ADMIN'", conn.execute.await_args_list[0].args[0])

    async def test_restart_preserves_removed_rulebook_grant(self):
        conn = AsyncMock()
        conn.fetchval.return_value = False
        await seed_roles(conn, {"CONTENT": [("CONTENT_EDIT_RULEBOOK",)]}, set())
        self.assertEqual(conn.execute.await_count, 1)  # Only the unconditional super admin grant.

    def test_all_rulebook_mutations_require_dedicated_permission(self):
        tree = ast.parse((ROOT / "tools/compliance/submissions/compliance_router.py").read_text())
        manage_user = next(
            node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "ManageUser" for target in node.targets)
        )
        self.assertIn('require_permission("CONTENT_RULEBOOK_UPDATE")', ast.unparse(manage_user).replace("'", '"'))
        mutation_names = {"create_rule", "update_rule", "delete_rule", "reset_defaults"}
        mutations = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in mutation_names]
        self.assertEqual({node.name for node in mutations}, mutation_names)
        for node in mutations:
            user = next(arg for arg in node.args.args if arg.arg == "user_id")
            self.assertEqual(ast.unparse(user.annotation), "ManageUser", node.name)


if __name__ == "__main__":
    unittest.main()
