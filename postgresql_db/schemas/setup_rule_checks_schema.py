"""One-time explicit checking-method configuration; never infer manual checks from empty phrases."""
import json
from postgresql_db.database import get_pool

TEXT = "TEXT_ANALYSIS"
VISUAL = "VISUAL_INSPECTION"
SOURCE = "SOURCE_VERIFICATION"
CLEARANCE = "CLEARANCE_DOCUMENTS"
HUMAN = "HUMAN_CONFIRMATION"
# Deliberate baseline policy. Multiple methods mean all of them require a result.
BASELINE_METHODS = {
    "Rule 1": [TEXT], "Rule 2": [TEXT, VISUAL, SOURCE], "Rule 3": [TEXT, SOURCE],
    "Rule 4": [TEXT, VISUAL, SOURCE], "Rule 5": [TEXT, VISUAL],
    "Rule 6": [TEXT, SOURCE, CLEARANCE], "Rule 7": [TEXT, VISUAL], "Rule 8": [CLEARANCE, HUMAN],
    "Rule 9": [TEXT, VISUAL], "Rule 10": [TEXT], "Rule 11": [TEXT, VISUAL], "Rule 12": [VISUAL],
    "Rule 13": [VISUAL, SOURCE, CLEARANCE], "Rule 14": [TEXT, SOURCE], "Rule 15": [TEXT, VISUAL],
    "Rule 16": [TEXT, SOURCE], "Rule 17": [TEXT, VISUAL], "Rule 18": [TEXT, SOURCE, HUMAN],
    "Rule 19": [TEXT, VISUAL, SOURCE, CLEARANCE], "Rule 20": [VISUAL, HUMAN],
    "Rule 21": [SOURCE, CLEARANCE, HUMAN], "Rule 22": [HUMAN], "Rule 23": [TEXT],
    "Rule 24": [TEXT, SOURCE], "Rule 25": [TEXT], "Rule 26": [TEXT, SOURCE],
    "Rule 27": [TEXT, VISUAL], "Rule 28": [TEXT, VISUAL], "Rule 29": [TEXT, VISUAL],
    "Rule 30": [TEXT, VISUAL], "Rule 31": [TEXT],
}


async def setup() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS checking_methods TEXT[],
                ADD COLUMN IF NOT EXISTS checking_methods_origin TEXT NOT NULL DEFAULT 'UNCONFIGURED';
        """)
        policy = [{"rule_number": number, "methods": methods} for number, methods in BASELINE_METHODS.items()]
        await conn.execute("""
            UPDATE compliance_rules r SET checking_methods = p.methods, checking_methods_origin = 'BASELINE', updated_at = NOW()
            FROM jsonb_to_recordset($1::jsonb) AS p(rule_number TEXT, methods TEXT[])
            WHERE r.rule_number = p.rule_number AND NOT r.is_custom AND r.checking_methods_origin = 'UNCONFIGURED';
        """, json.dumps(policy))
        await conn.execute("""
            UPDATE compliance_rules SET checking_methods = ARRAY['HUMAN_CONFIRMATION']::text[],
                checking_methods_origin = 'MIGRATED_CUSTOM', updated_at = NOW()
            WHERE checking_methods_origin = 'UNCONFIGURED';
            ALTER TABLE compliance_rules ALTER COLUMN checking_methods SET DEFAULT ARRAY['HUMAN_CONFIRMATION']::text[],
                ALTER COLUMN checking_methods SET NOT NULL;
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'compliance_rules_checking_methods_valid'
                               AND conrelid = 'compliance_rules'::regclass) THEN
                    ALTER TABLE compliance_rules ADD CONSTRAINT compliance_rules_checking_methods_valid
                    CHECK (cardinality(checking_methods) > 0 AND checking_methods <@
                        ARRAY['TEXT_ANALYSIS','VISUAL_INSPECTION','SOURCE_VERIFICATION','CLEARANCE_DOCUMENTS','HUMAN_CONFIRMATION']::text[]);
                END IF;
            END $$;
        """)
