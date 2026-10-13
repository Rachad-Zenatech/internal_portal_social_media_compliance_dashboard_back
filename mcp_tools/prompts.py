from fastmcp.prompts import prompt


@prompt
def rbac_help_prompt(query: str = "") -> str:
    """Ask about system roles, permissions, and security configurations."""
    query_clause = f" specifically regarding: '{query}'" if query else ""
    return (
        f"Call `list_my_accessible_tools_tool` to inspect available permissions{query_clause}, "
        f"and explain what access levels and capabilities are granted to the user."
    )


ALL_PROMPTS = [
    rbac_help_prompt,
]

