# mcp_tools/__init__.py
# Central registry for all MCP tools and prompts. Each tool/prompt is a plain
# module-level function; `register_all` attaches tools via `mcp.add_tool` and
# prompts via `mcp.add_prompt`, deriving the name and description from the
# function name and docstring.
#
# The same tools serve two callers, with the same RBAC check, tracing and output
# redaction: MCP clients through /mcp (mcp_tools.middleware) and the assistant
# chat through Gemini function calling (services.ai.assistant_chat_service, via run_tool).
import inspect
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID

from fastmcp.prompts import Prompt

from mcp_tools.permissions import list_my_accessible_tools_tool
from mcp_tools.prompts import ALL_PROMPTS
from mcp_tools.reference import list_compliance_rules_tool, search_reference_material_tool
from services.ai.ai_guardrails import redact_pii
from services.ai.ai_observability import span

logger = logging.getLogger(__name__)

# Registered MCP tools for this template
ALL_TOOLS = [
    list_my_accessible_tools_tool,
    search_reference_material_tool,
    list_compliance_rules_tool,
]

# Permission code per tool: roles are granted tools by these codes (role_mcp_tool_permissions).
MCP_TOOL_CODE_MAP = {
    "list_my_accessible_tools_tool": "PERMISSIONS_VIEW",
    "search_reference_material_tool": "REFERENCE_SEARCH",
    "list_compliance_rules_tool": "COMPLIANCE_RULES_VIEW",
}

TOOLS_BY_NAME: Dict[str, Callable[..., Any]] = {t.__name__: t for t in ALL_TOOLS}


def tool_code(name: str) -> str:
    return MCP_TOOL_CODE_MAP.get(name, name)


def register_all(mcp):
    """Register every tool and prompt with the given FastMCP instance."""
    from mcp_tools.middleware import McpAccessMiddleware

    mcp.add_middleware(McpAccessMiddleware())
    for tool_fn in ALL_TOOLS:
        mcp.add_tool(tool_fn)
    for prompt_fn in ALL_PROMPTS:
        if isinstance(prompt_fn, Prompt):
            mcp.add_prompt(prompt_fn)
        else:
            try:
                mcp.add_prompt(prompt_fn)
            except TypeError:
                mcp.add_prompt(Prompt.from_function(prompt_fn))


async def allowed_tool_names(user_id: Optional[UUID]) -> List[str]:
    """The registered tools this person's roles allow (all of them for super admins)."""
    from services.shared.auth_service import get_my_permissions

    if not user_id:
        return []
    perms = await get_my_permissions(user_id)
    if not perms:
        return []
    is_super_admin = perms.get("user", {}).get("is_super_admin") or any(
        r.get("code") == "SUPER_ADMIN" for r in perms.get("roles", [])
    )
    codes = set(perms.get("mcp_tool_permissions") or [])
    return [name for name in TOOLS_BY_NAME if is_super_admin or tool_code(name) in codes]


async def run_tool(name: str, args: Dict[str, Any], user_id: Optional[UUID], source: str = "chat") -> str:
    """Run a tool for a person: RBAC check, traced, output PII-redacted. Failures come back as text for the model."""
    from services.shared.auth_service import current_user_id_ctx, user_can_use_mcp_tool

    async with span("tool", name, source=source, code=tool_code(name), args=sorted(args)) as s:
        fn = TOOLS_BY_NAME.get(name)
        if fn is None:
            s.status = "ERROR"
            return f"There is no tool called {name}."
        if not user_id or not await user_can_use_mcp_tool(user_id, tool_code(name)):
            s.status, s.error_type = "DENIED", "PermissionDenied"
            return "You don't have permission to use this tool."
        current_user_id_ctx.set(user_id)
        params = inspect.signature(fn).parameters
        takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        try:
            # Arguments the tool doesn't take (the model sometimes invents some) are dropped.
            result = await fn(**{k: v for k, v in args.items() if takes_any or k in params})
        except Exception as e:
            logger.exception("Tool %s failed", name, extra={"event": "mcp_tool_failed"})
            s.status, s.error_type = "ERROR", type(e).__name__
            return "The tool failed. Try again or answer without it."
        text, redactions = redact_pii(result if isinstance(result, str) else json.dumps(result, default=str))
        s.attributes.update(result_chars=len(text), pii_redactions=redactions or None)
        return text


async def sync_mcp_tools_to_vector_db():
    """Sync registered tools to PostgreSQL mcp_tools table (one batch statement)."""
    from postgresql_db.database import get_pool
    try:
        pool = get_pool()
        if pool is not None:
            names = [t.__name__ for t in ALL_TOOLS]
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE mcp_tools SET is_active = false WHERE NOT (code = ANY($1::text[]))',
                                       [tool_code(n) for n in names])
                    await conn.execute('''
                        INSERT INTO mcp_tools (name, code, server_name, description, is_read_only, is_sensitive, is_active)
                        SELECT name, code, $4::text, description, true, false, true
                        FROM unnest($1::text[], $2::text[], $3::text[]) AS t(name, code, description)
                        ON CONFLICT (code) DO UPDATE SET
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            is_active = true
                    ''',
                        [n.replace('_tool', '').replace('_', ' ').title() for n in names],
                        [tool_code(n) for n in names],
                        [inspect.getdoc(t) or t.__name__ for t in ALL_TOOLS],
                        "template-mcp",
                    )
    except Exception as e:
        logger.warning(f"MCP tool sync skipped or errored: {e}")
