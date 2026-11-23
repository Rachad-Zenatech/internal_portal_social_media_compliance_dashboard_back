"""Create compliance rule tables and their backward-compatible columns."""


async def create_rule_tables(conn):
    await conn.execute("""
        -- Compliance Rulebooks Table
        CREATE TABLE IF NOT EXISTS compliance_rulebooks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title VARCHAR(255) NOT NULL,
            version VARCHAR(50) NOT NULL,
            effective_date VARCHAR(50) NOT NULL,
            description TEXT,
            source_document VARCHAR(255),
            is_active BOOLEAN DEFAULT true NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        );

        -- Compliance Rules Table
        CREATE TABLE IF NOT EXISTS compliance_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rulebook_id UUID REFERENCES compliance_rulebooks(id) ON DELETE SET NULL,
            rule_number VARCHAR(50) NOT NULL,
            category VARCHAR(100) NOT NULL,
            title VARCHAR(255) NOT NULL,
            summary TEXT NOT NULL,
            severity VARCHAR(50) DEFAULT 'HIGH' NOT NULL,
            traffic_light VARCHAR(20) DEFAULT 'YELLOW' NOT NULL,
            prohibited_terms TEXT[] DEFAULT '{}'::text[],
            required_disclaimers TEXT[] DEFAULT '{}'::text[],
            allowed_examples TEXT,
            prohibited_examples TEXT,
            remediation_guidance TEXT,
            applies_to TEXT[] DEFAULT '{"video", "imagery", "caption", "ceo_post"}'::text[],
            is_active BOOLEAN DEFAULT true NOT NULL,
            is_custom BOOLEAN DEFAULT false NOT NULL,
            sort_order INT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
            CONSTRAINT compliance_rules_rule_number_unique UNIQUE (rule_number)
        );

        CREATE INDEX IF NOT EXISTS idx_compliance_rules_category ON compliance_rules(category);
        CREATE INDEX IF NOT EXISTS idx_compliance_rules_severity ON compliance_rules(severity);
        CREATE INDEX IF NOT EXISTS idx_compliance_rules_traffic_light ON compliance_rules(traffic_light);
        CREATE INDEX IF NOT EXISTS idx_compliance_rules_is_active ON compliance_rules(is_active);

        -- Soft delete, so removed baseline rules aren't re-seeded and history still resolves
        ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;
        -- Phrases that make a rule's required disclaimer necessary (empty: always needed)
        ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS disclaimer_triggers TEXT[] DEFAULT '{}'::text[];
        -- Phrases that cancel a match inside them (e.g. "not combat proven")
        ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS exception_terms TEXT[] DEFAULT '{}'::text[];
        -- RULE: a match is a finding. CONTEXT: a match is a candidate the AI judges from the surrounding wording
        ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS evaluation_mode VARCHAR(10) NOT NULL DEFAULT 'RULE';
        ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS evaluation_guidance TEXT;
    """)
