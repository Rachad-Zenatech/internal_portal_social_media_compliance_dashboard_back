"""Upgrade untouched baseline guidance without overwriting operator edits or custom rules."""
from postgresql_db.schemas.baseline_context_guidance import CONTEXTUAL_BASELINE

OLD_RULE29_GUIDANCE = (
    'Decide whether the passage shows or promotes death, killing, violence, terrorism, weapons or '
    "self-harm. VIOLATION: violent, threatening or graphic wording or imagery (a slogan like 'KILL' or "
    "'DEATH' on a vehicle, clothing or sign; a weapon shown or named as part of the post; joking about "
    'killing, death or suicide). NEEDS_REVIEW: a defense or security post that names threats, weapons or '
    "terrorism factually (for example 'detects threats before they reach the base'), since the wording "
    'has to be checked against the defense rulebook. NO_VIOLATION: an ordinary figure of speech or a '
    "technical term ('killer app', 'kill switch', 'battery is dead', 'deadline'), or a news or safety "
    "context that doesn't glorify harm."
)


async def upgrade_rule29(conn):
    return await conn.execute(
        "UPDATE compliance_rules SET evaluation_guidance=$1, updated_at=NOW() "
        "WHERE rule_number='Rule 29' AND NOT is_custom AND evaluation_mode='CONTEXT' AND evaluation_guidance=$2",
        CONTEXTUAL_BASELINE["Rule 29"], OLD_RULE29_GUIDANCE,
    )
