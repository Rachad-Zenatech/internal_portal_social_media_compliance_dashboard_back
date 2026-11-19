"""Default navigation, permission groups, roles and initial administrators."""

import os


async def seed_navigation(conn):
    print("Seeding permission actions...")
    actions = [
        ("VIEW", "View / Read access"),
        ("CREATE", "Create / Add access"),
        ("UPDATE", "Edit / Update access"),
        ("DELETE", "Delete access"),
        ("EXECUTE", "Execution access"),
        ("APPROVE", "Approval access"),
    ]
    for code, name in actions:
        await conn.execute("""
            INSERT INTO permission_actions (code, name)
            VALUES ($1, $2)
            ON CONFLICT (code) DO NOTHING
        """, code, name)

    print("Seeding navigation items...")
    nav_items = [
        ("DASHBOARD", "Dashboard", "/dashboard", None, 10, "LayoutDashboard", True),
        ("CONFIGURATIONS", "Configurations", "/configuration", None, 20, "Settings", True),
        ("CONFIG_USERS", "Users", "/configuration/users", "CONFIGURATIONS", 21, "Users", True),
        ("CONFIG_ROLES", "Roles & Permissions", "/configuration/roles", "CONFIGURATIONS", 22, "Shield", True),
        ("CONFIG_USER_ROLE_ASSIGNMENT", "User Role Assignment", "/configuration/user-role-assignment", "CONFIGURATIONS", 23, "UserCheck", True),
        ("CONFIG_ROLE_API_PERMISSIONS", "Role API Permissions", "/configuration/role-api-permissions", "CONFIGURATIONS", 24, "Lock", True),
        ("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "Role MCP Permissions", "/configuration/role-mcp-tool-permissions", "CONFIGURATIONS", 25, "Bot", True),
        ("LOGS", "Logs", "/log", None, 30, "FileText", True),
        ("AUDIT_LOG", "Audit Logs", "/log/audit", "LOGS", 31, "History", True),
        ("SYSTEM_LOGS", "System Logs", "/log/system-logs", "LOGS", 32, "Terminal", True),
        # Not a menu entry: carries the "everyone's content" permissions for the Content Portal.
        ("CONTENT_SUBMISSIONS", "Content Portal", "/submissions", None, 15, "LayoutGrid", False),
        # Not a menu entry: reviewing submitted content and publishing or scheduling it.
        ("CONTENT_REVIEW", "Content Review", "/compliance/scanner", None, 16, "ClipboardCheck", False),
    ]
    for code, name, path, parent, order, icon, is_menu in nav_items:
        await conn.execute("""
            INSERT INTO navigation_items (code, name, route_path, parent_code, display_order, icon, is_menu_item, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, true)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                route_path = EXCLUDED.route_path,
                parent_code = EXCLUDED.parent_code,
                display_order = EXCLUDED.display_order,
                icon = EXCLUDED.icon,
                is_active = true
        """, code, name, path, parent, order, icon, is_menu)


async def seed_permission_groups(conn):
    # Modules and groups are created or updated in place, never deleted: deleting them (this used to TRUNCATE)
    # cascaded to role_permission_groups and wiped every grant made in Role Permissions on each restart.
    print("Seeding permission modules and groups...")

    modules = [
        ("MAIN", "Dashboard & Overview", 10),
        ("CONTENT", "Content & Submissions", 15),
        ("CONFIGURATIONS", "Configurations & Security", 20),
        ("LOGS", "Audit & System Logs", 30),
    ]
    for m in modules:
        await conn.execute("""
            INSERT INTO permission_modules (code, name, sort_order) VALUES ($1, $2, $3)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sort_order = EXCLUDED.sort_order, updated_at = NOW()
        """, *m)

    groups_data = {
        "MAIN": [
            ("MAIN_VIEW_DASHBOARD", "View Dashboard", "Access system KPI dashboards and metrics.", 10, [("DASHBOARD", "VIEW")]),
        ],
        "CONTENT": [
            ("CONTENT_MANAGE_DRAFTS", "Manage Team Drafts",
             "See everyone's content in the Content Portal and delete other people's drafts.", 10,
             [("CONTENT_SUBMISSIONS", "VIEW"), ("CONTENT_SUBMISSIONS", "DELETE")]),
            ("CONTENT_REVIEW_PUBLISH", "Review & Publish Content",
             "Review everyone's submitted content, comment on it, request changes, and approve it for the "
             "calendar or publish it.", 20,
             [("CONTENT_REVIEW", "VIEW"), ("CONTENT_REVIEW", "APPROVE")]),
            ("CONTENT_REVIEW_SUGGEST", "Suggest on Content",
             "See the content that was sent to them by name, comment on it and suggest edits to its script. They "
             "can't approve it, request changes or schedule anything.", 30,
             [("CONTENT_REVIEW", "VIEW"), ("CONTENT_REVIEW", "SUGGEST")]),
        ],
        "CONFIGURATIONS": [
            ("CONFIG_VIEW_USERS", "View Users & Roles", "View all users, roles, and permission assignments.", 10, [("CONFIG_USERS", "VIEW"), ("CONFIG_ROLES", "VIEW")]),
            ("CONFIG_MANAGE_USERS", "Manage Users", "Create, edit, activate/deactivate system users.", 20, [("CONFIG_USERS", "CREATE"), ("CONFIG_USERS", "UPDATE"), ("CONFIG_USERS", "DELETE")]),
            ("CONFIG_MANAGE_ROLES", "Manage Roles & Permissions", "Create, edit, or configure roles and permission groups.", 30, [("CONFIG_ROLES", "CREATE"), ("CONFIG_ROLES", "UPDATE"), ("CONFIG_ROLES", "DELETE")]),
            ("CONFIG_ASSIGN_ROLES", "User Role Assignment", "Assign roles and configure user permissions.", 40, [("CONFIG_USER_ROLE_ASSIGNMENT", "CREATE"), ("CONFIG_USER_ROLE_ASSIGNMENT", "UPDATE")]),
            ("CONFIG_MCP_PERMISSIONS", "MCP Tool Permissions", "Configure MCP tool access rules by role.", 50, [("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "VIEW"), ("CONFIG_ROLE_MCP_TOOL_PERMISSIONS", "UPDATE")]),
        ],
        "LOGS": [
            ("LOGS_VIEW_AUDIT", "View Audit Logs", "View system audit logs and action history.", 10, [("AUDIT_LOG", "VIEW")]),
            ("LOGS_VIEW_SYSTEM", "View System Logs", "Access system error and diagnostic logs.", 20, [("SYSTEM_LOGS", "VIEW")]),
        ],
    }

    # Groups created by this run; only these get the default role grants below.
    new_group_codes = set()
    async with conn.transaction():
        for module_code, groups in groups_data.items():
            module_id = await conn.fetchval("SELECT id FROM permission_modules WHERE code = $1", module_code)
            for g_code, g_name, g_desc, g_sort, actions_list in groups:
                g = await conn.fetchrow("""
                    INSERT INTO permission_groups (module_id, code, name, description, sort_order)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (code) DO UPDATE SET
                        module_id = EXCLUDED.module_id, name = EXCLUDED.name,
                        description = EXCLUDED.description, sort_order = EXCLUDED.sort_order, updated_at = NOW()
                    RETURNING id, (xmax = 0) AS inserted
                """, module_id, g_code, g_name, g_desc, g_sort)
                if g["inserted"]:
                    new_group_codes.add(g_code)

                # What a group grants is defined here in code, so refresh its actions in place.
                await conn.execute("DELETE FROM permission_group_actions WHERE permission_group_id = $1", g["id"])
                await conn.execute("""
                    INSERT INTO permission_group_actions (permission_group_id, api_module_code, action)
                    SELECT $1, nav, act FROM unnest($2::text[], $3::text[]) AS a(nav, act)
                """, g["id"], [n for n, _ in actions_list], [a for _, a in actions_list])
    return groups_data, new_group_codes


async def seed_roles(conn, groups_data, new_group_codes):
    print("Seeding standard roles...")
    roles_data = [
        ("SUPER_ADMIN", "Super Administrator", "Full unrestricted access to all modules and configurations", True),
        ("ADMIN", "Administrator", "Administrative access to user and role configurations", True),
        ("USER", "Standard User", "Standard system user with basic dashboard access", False),
        ("MANAGER", "Content Manager", "Oversees the team's content: sees everyone's submissions and can delete drafts", False),
        ("REVIEWER", "Content Reviewer", "Reviews submitted content, requests changes and approves it for publishing", False),
        ("SUGGESTING_REVIEWER", "Suggesting Reviewer",
         "Comments on and suggests edits to the content sent to them. Can't approve, request changes or schedule", False),
    ]
    new_roles = set()
    for r_code, r_name, r_desc, is_sys in roles_data:
        inserted = await conn.fetchval("""
            INSERT INTO roles (code, name, description, is_system_role, is_active)
            VALUES ($1, $2, $3, $4, true)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                is_active = true
            RETURNING (xmax = 0)
        """, r_code, r_name, r_desc, is_sys)
        if inserted:
            new_roles.add(r_code)

    # Super Administrator always has everything.
    await conn.execute("""
        INSERT INTO role_permission_groups (role_id, permission_group_id)
        SELECT r.id, g.id FROM roles r CROSS JOIN permission_groups g WHERE r.code = 'SUPER_ADMIN'
        ON CONFLICT DO NOTHING
    """)

    # Default grants for the other built-in roles ("*" = every group). They're applied only to a role that was
    # just created, or for a group that was just created, so permissions removed in Role Permissions stay removed.
    default_grants = {
        "ADMIN": "*",
        "USER": {"MAIN_VIEW_DASHBOARD"},
        "MANAGER": {"MAIN_VIEW_DASHBOARD", "CONTENT_MANAGE_DRAFTS", "CONTENT_REVIEW_PUBLISH"},
        "REVIEWER": {"MAIN_VIEW_DASHBOARD", "CONTENT_REVIEW_PUBLISH"},
        "SUGGESTING_REVIEWER": {"MAIN_VIEW_DASHBOARD", "CONTENT_REVIEW_SUGGEST"},
    }
    all_codes = [c for groups in groups_data.values() for c, *_ in groups]
    for r_code, wanted in default_grants.items():
        codes = all_codes if wanted == "*" else [c for c in all_codes if c in wanted]
        if r_code not in new_roles:
            codes = [c for c in codes if c in new_group_codes]
        if codes:
            await conn.execute("""
                INSERT INTO role_permission_groups (role_id, permission_group_id)
                SELECT r.id, g.id FROM roles r JOIN permission_groups g ON g.code = ANY($2::text[])
                WHERE r.code = $1
                ON CONFLICT DO NOTHING
            """, r_code, codes)


async def seed_super_admins(conn):
    super_admin_role_id = await conn.fetchval("SELECT id FROM roles WHERE code = 'SUPER_ADMIN'")

    # Seed Super Admin Users
    admin_emails_env = os.getenv("INITIAL_SUPER_ADMIN_EMAILS", "admin@example.com")
    admin_emails = [e.strip().lower() for e in admin_emails_env.split(",") if e.strip()]

    for email in admin_emails:
        full_name = email.split('@')[0].replace('.', ' ').title()
        user_id = await conn.fetchval("""
            INSERT INTO users (email, full_name, is_active, is_super_admin)
            VALUES ($1, $2, true, true)
            ON CONFLICT (email) DO UPDATE SET is_super_admin = true, is_active = true
            RETURNING id
        """, email, full_name)

        if super_admin_role_id and user_id:
            await conn.execute("""
                INSERT INTO user_roles (user_id, role_id, is_active)
                VALUES ($1, $2, true)
                ON CONFLICT (user_id, role_id) DO UPDATE SET is_active = true
            """, user_id, super_admin_role_id)
