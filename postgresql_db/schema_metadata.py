"""
SQLAlchemy Core schema mirror used ONLY for Alembic autogenerate.
Contains core RBAC, Auth, Audit, and Notification tables for the template.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

app_settings = sa.Table(
    "app_settings", metadata,
    sa.Column("key", sa.VARCHAR(length=100), primary_key=True),
    sa.Column("settings_json", sa.TEXT()),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("full_name", sa.VARCHAR(length=255)),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("job_title", sa.VARCHAR(length=255)),
    sa.Column("auth_provider", sa.VARCHAR(length=50), server_default=sa.text("'microsoft'::character varying")),
    sa.Column("entra_oid", sa.VARCHAR(length=255)),
    sa.Column("microsoft_tenant_id", sa.VARCHAR(length=255)),
    sa.Column("microsoft_object_id", sa.VARCHAR(length=255)),
    sa.Column("microsoft_subject_id", sa.VARCHAR(length=255)),
    sa.Column("sso_enabled", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("graph_account_enabled", sa.BOOLEAN()),
    sa.Column("last_graph_sync", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_sso_login_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("force_password_change", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_super_admin", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_activity_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_by", sa.UUID()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_by", sa.UUID()),
    sa.UniqueConstraint("email", name="users_email_key"),
    sa.UniqueConstraint("entra_oid", name="users_entra_oid_key"),
)

roles = sa.Table(
    "roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_system_role", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("parent_role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="SET NULL")),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="roles_code_key"),
)

user_roles = sa.Table(
    "user_roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("assigned_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("assigned_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.UniqueConstraint("user_id", "role_id", name="user_roles_user_id_role_id_key"),
)

navigation_items = sa.Table(
    "navigation_items", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("route_path", sa.VARCHAR(length=255)),
    sa.Column("parent_code", sa.VARCHAR(length=100)),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("icon", sa.VARCHAR(length=100)),
    sa.Column("is_menu_item", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="navigation_items_code_key"),
)

permission_actions = sa.Table(
    "permission_actions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_actions_code_key"),
)

role_navigation_permissions = sa.Table(
    "role_navigation_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("navigation_item_id", sa.UUID(), sa.ForeignKey("navigation_items.id", ondelete="CASCADE"), nullable=False),
    sa.Column("action_id", sa.UUID(), sa.ForeignKey("permission_actions.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "navigation_item_id", "action_id", name="role_navigation_permissions_role_id_navigation_item_id_act_key"),
)

permission_modules = sa.Table(
    "permission_modules", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_modules_id_seq'::regclass)")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_modules_code_key"),
)

permission_groups = sa.Table(
    "permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_groups_id_seq'::regclass)")),
    sa.Column("module_id", sa.INTEGER(), sa.ForeignKey("permission_modules.id", ondelete="CASCADE")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_groups_code_key"),
)

permission_group_actions = sa.Table(
    "permission_group_actions", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_group_actions_id_seq'::regclass)")),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE")),
    sa.Column("api_module_code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("action", sa.VARCHAR(length=255), nullable=False),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

role_permission_groups = sa.Table(
    "role_permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('role_permission_groups_id_seq'::regclass)")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "permission_group_id", name="role_permission_groups_role_id_permission_group_id_key"),
)

mcp_tools = sa.Table(
    "mcp_tools", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("server_name", sa.VARCHAR(length=100), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_read_only", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_sensitive", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="mcp_tools_code_key"),
)

role_mcp_tool_permissions = sa.Table(
    "role_mcp_tool_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("mcp_tool_id", sa.UUID(), sa.ForeignKey("mcp_tools.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("access_level", sa.VARCHAR(length=50), server_default=sa.text("'ALLOW'::character varying")),
    sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "mcp_tool_id", name="role_mcp_tool_permissions_role_id_mcp_tool_id_key"),
)

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("actor_user_id", sa.UUID(), sa.ForeignKey("users.id")),
    sa.Column("action", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("ip_address", postgresql.INET()),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
)

login_activity_logs = sa.Table(
    "login_activity_logs", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('login_activity_logs_id_seq'::regclass)")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
    sa.Column("success", sa.BOOLEAN(), nullable=False),
    sa.Column("failure_reason", sa.TEXT()),
    sa.Column("ip_address", sa.VARCHAR(length=100)),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

notifications = sa.Table(
    "notifications", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('notifications_id_seq'::regclass)")),
    sa.Column("user_id", sa.VARCHAR(length=255), nullable=False),
    sa.Column("type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("title", sa.VARCHAR(length=255), nullable=False),
    sa.Column("message", sa.TEXT(), nullable=False),
    sa.Column("link_url", sa.VARCHAR(length=500)),
    sa.Column("entity_type", sa.VARCHAR(length=100)),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("sender_name", sa.VARCHAR(length=255)),
    sa.Column("sender_avatar", sa.VARCHAR(length=500)),
    sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("is_read", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("read_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

brands = sa.Table(
    "brands", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('brands_id_seq'::regclass)")),
    sa.Column("code", sa.VARCHAR(length=50), nullable=False),
    sa.Column("name", sa.VARCHAR(length=100), nullable=False),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="brands_code_key"),
)

organizations = sa.Table(
    "organizations", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("slug", sa.VARCHAR(length=100), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.UniqueConstraint("slug", name="organizations_slug_key"),
)

companies = sa.Table(
    "companies", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("code", sa.VARCHAR(length=50), nullable=False),
    sa.Column("parent_company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="SET NULL")),
    sa.Column("brand_id", sa.INTEGER(), sa.ForeignKey("brands.id", ondelete="SET NULL")),
    sa.Column("currency", sa.VARCHAR(length=3), nullable=False, server_default=sa.text("'USD'::character varying")),
    sa.Column("is_active", sa.BOOLEAN(), nullable=False, server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.UniqueConstraint("organization_id", "code", name="companies_org_code_key"),
)

oauth_connections = sa.Table(
    "oauth_connections", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
    sa.Column("company_id", sa.UUID(), sa.ForeignKey("companies.id", ondelete="CASCADE")),
    sa.Column("provider", sa.VARCHAR(length=50), nullable=False),
    sa.Column("provider_account_id", sa.VARCHAR(length=255)),
    sa.Column("account_name", sa.VARCHAR(length=255)),
    sa.Column("access_token_encrypted", sa.TEXT(), nullable=False),
    sa.Column("refresh_token_encrypted", sa.TEXT()),
    sa.Column("token_type", sa.VARCHAR(length=50), nullable=False, server_default=sa.text("'Bearer'::character varying")),
    sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("scopes", postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]")),
    sa.Column("status", sa.VARCHAR(length=50), nullable=False, server_default=sa.text("'CONNECTED'::character varying")),
    sa.Column("error_detail", sa.TEXT()),
    sa.Column("connection_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
)

compliance_rulebooks = sa.Table(
    "compliance_rulebooks", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("title", sa.VARCHAR(length=255), nullable=False),
    sa.Column("version", sa.VARCHAR(length=50), nullable=False),
    sa.Column("effective_date", sa.VARCHAR(length=50), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("source_document", sa.VARCHAR(length=255)),
    sa.Column("is_active", sa.BOOLEAN(), nullable=False, server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
)

compliance_rules = sa.Table(
    "compliance_rules", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("rulebook_id", sa.UUID(), sa.ForeignKey("compliance_rulebooks.id", ondelete="SET NULL")),
    sa.Column("rule_number", sa.VARCHAR(length=50), nullable=False),
    sa.Column("category", sa.VARCHAR(length=100), nullable=False),
    sa.Column("title", sa.VARCHAR(length=255), nullable=False),
    sa.Column("summary", sa.TEXT(), nullable=False),
    sa.Column("severity", sa.VARCHAR(length=50), nullable=False, server_default=sa.text("'HIGH'::character varying")),
    sa.Column("traffic_light", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("'YELLOW'::character varying")),
    sa.Column("prohibited_terms", postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]")),
    sa.Column("required_disclaimers", postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]")),
    sa.Column("allowed_examples", sa.TEXT()),
    sa.Column("prohibited_examples", sa.TEXT()),
    sa.Column("remediation_guidance", sa.TEXT()),
    sa.Column("applies_to", postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{\"video\", \"imagery\", \"caption\"}'::text[]")),
    sa.Column("is_active", sa.BOOLEAN(), nullable=False, server_default=sa.text("true")),
    sa.Column("is_custom", sa.BOOLEAN(), nullable=False, server_default=sa.text("false")),
    sa.Column("sort_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.UniqueConstraint("rule_number", name="compliance_rules_rule_number_unique"),
)
