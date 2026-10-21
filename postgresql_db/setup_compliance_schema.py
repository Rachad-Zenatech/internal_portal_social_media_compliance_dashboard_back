"""Setup Compliance Rulebooks and Rules Schema
Creates compliance_rulebooks and compliance_rules tables.
Populates base rules from:
- ZenaTech and ZenaDrone Defense Thought Leadership and Imagery Rulebook Oct. 2026
- ZenaTech and ZenaDrone Defense Content Quick Use Guide, Oct. 2026
"""

import asyncio
import logging
from postgresql_db.database import get_pool, close_pool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


BASE_RULEBOOKS = [
    {
        "title": "ZenaTech & ZenaDrone Defense Thought Leadership & Imagery Rulebook",
        "version": "1.0",
        "effective_date": "2026-10-01",
        "description": "Internal governance manual for defense thought leadership, social media, communications and visual imagery.",
        "source_document": "ZenaTech and ZenaDrone Defense Thought Leadership and Imagery Rulebook Oct. 2026.docx",
    },
    {
        "title": "Defense Content & Social Media Creator Quick-Use Guide",
        "version": "1.0",
        "effective_date": "2026-10-10",
        "description": "Everyday working rules for video editors, graphic designers, copywriters, and content creators.",
        "source_document": "ZenaTech and ZenaDrone Defense Content Quick Use Guide, October 10th, 2026.docx",
    },
]

BASE_RULES = [
    {
        "rule_number": "Rule 1",
        "category": "Thought Leadership & Tone",
        "title": "Challenge the Problem, Never Insult the Customer",
        "summary": "Be provocative about the future of defense, never provocative toward the people defending it. Bold on mission tension, physics, and cost curves—never attack named officials, generals, political parties, or procurement teams.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "procurement is failing",
            "incompetent officials",
            "pentagon failure",
            "corrupt defense",
            "wasteful generals",
            "bureaucrats failing warfighters",
            "failing warfighters",
            "military incompetence",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Defense does not have a sensor problem; it has a decision-speed problem. A million-dollar interceptor against a $2,000 drone is an economic mismatch.",
        "prohibited_examples": "Pentagon procurement is failing our warfighters. Named officials are asleep at the wheel.",
        "remediation_guidance": "Frame the criticism around structural incentives, technological shifts, and economics of attrition rather than attacking institutions or personnel.",
        "sort_order": 10,
    },
    {
        "rule_number": "Rule 2",
        "category": "Product & Prototype Maturity",
        "title": "Do Not Make a Prototype Look Deployed or Operational",
        "summary": "ZenaDrone systems are development, testing, evaluation, demonstration, or pilot stage. Never represent prototypes as fielded, combat-proven, or commercially adopted by defense branches.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "fielded system",
            "operationally deployed",
            "combat proven",
            "production ready",
            "in active military service",
            "delivers operational capability",
            "combat ready",
            "adopted by the military",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Prototype undergoing flight evaluation; development-stage autonomy platform; intended for contested logistics.",
        "prohibited_examples": "Fielded defense drone; commercially adopted by the Army; combat-proven ISR.",
        "remediation_guidance": "Consult the current dated Product Status Register and use exact maturity terminology: 'prototype', 'development-stage system', or 'flight testing'.",
        "sort_order": 20,
    },
    {
        "rule_number": "Rule 3",
        "category": "Government & Customer Claims",
        "title": "Never Upgrade a Meeting, Test, or Pilot into a Partnership",
        "summary": "Never translate an exploratory meeting into a partnership, an observation into an endorsement, or a demonstration/pilot into customer selection or a Program of Record.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "partnered with the military",
            "partnership with the army",
            "partnership with us navy",
            "selected by the dod",
            "chosen by the military",
            "in advanced negotiations with the army",
            "expect a major defense contract",
            "exclusive defense partner",
            "dod preferred partner",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Demonstrated specific cleared capability to government observers; completed paid evaluation pilot according to agreed scope.",
        "prohibited_examples": "ZenaDrone has partnered with the U.S. Army. Demand from defense customers is accelerating.",
        "remediation_guidance": "State only the cleared factual event. Obtain written legal sign-off before using customer agency names.",
        "sort_order": 30,
    },
    {
        "rule_number": "Rule 4",
        "category": "Endorsement & Disclaimers",
        "title": "Do Not Imply Endorsement by U.S. Defense Agencies",
        "summary": "Federal regulations strictly prohibit implying official endorsement, preference, or sanction by the DOD, Military Services, units, or uniform personnel.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "endorsed by the dod",
            "approved by the army",
            "navy certified",
            "warfighters choose zenadrone",
            "preferred by the military",
            "military validated",
        ],
        "required_disclaimers": [
            "The appearance of U.S. Department of Defense visual information does not imply or constitute DOD endorsement."
        ],
        "allowed_examples": "Factual description of participation in an authorized industry demonstration with the standard non-endorsement disclaimer.",
        "prohibited_examples": "U.S. Marines endorse ZenaDrone for autonomous logistics.",
        "remediation_guidance": "Include the mandatory non-endorsement disclaimer in readable proximity whenever cleared military visual information is utilized.",
        "sort_order": 40,
    },
    {
        "rule_number": "Rule 5",
        "category": "Defense Imagery & Video",
        "title": "Image-Text Parity: Visuals Cannot Claim More than Words",
        "summary": "Visual creative and video sequences cannot imply capabilities, weapons, payload capacities, or deployments that the written copy is prohibited from claiming.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "untested weapons",
            "missile firing",
            "fake combat kill",
            "untested swarm scale",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Clean product photography or 3D render matching verified engineering specs; operator in generic attire.",
        "prohibited_examples": "Fictional explosions, weaponized payloads, or photorealistic warzone combat footage not backed by test evidence.",
        "remediation_guidance": "Align visuals strictly with cleared engineering configurations. If conceptual, apply the mandatory 'Development concept' label.",
        "sort_order": 50,
    },
    {
        "rule_number": "Rule 6",
        "category": "Investor Disclosure & Procurement",
        "title": "Do Not Post Non-Public Contract, Pipeline, or Revenue Data",
        "summary": "Compliance with SEC disclosure principles and public company standards: never disclose material non-public business information, pipeline estimates, or unannounced contract values on social channels.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "expect a major defense contract",
            "advanced negotiations for contract",
            "pipeline value",
            "unreleased revenue",
            "internal test telemetry",
            "award imminent",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Quoting released SEC filings or press releases with exact figures, dates, and links.",
        "prohibited_examples": "We are in final negotiations for a $20M defense contract.",
        "remediation_guidance": "Stop immediately and verify whether the information has been released via official 6-K or press release. Secure IR/CFO confirmation.",
        "sort_order": 60,
    },
    {
        "rule_number": "Rule 7",
        "category": "Security, Privacy & CUI",
        "title": "OPSEC: Never Reveal Sensitive Locations, Screens, or CUI",
        "summary": "Inspect the entire video frame and background. Never reveal Controlled Unclassified Information (CUI), export-controlled data (ITAR/EAR), base layouts, flight paths, tactical displays, or identifiable faces.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "cui",
            "controlled unclassified",
            "classified",
            "itar restricted",
            "gps coordinates of base",
            "tactical frequency",
            "unredacted screen",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Abstracted graphic telemetry, blurred installation background, non-identifiable test field.",
        "prohibited_examples": "Video panning across computer monitors showing classified telemetry, base security checkpoints, or badges.",
        "remediation_guidance": "Review video frames at 100% zoom. Blur name tapes, serial numbers, computer screens, badges, and distinctive military markings.",
        "sort_order": 70,
    },
    {
        "rule_number": "Rule 8",
        "category": "Governance & Workflow",
        "title": "When Uncertain, Stop and Require Comms/Legal Clearance",
        "summary": "Operating baseline: if any claim, video clip, or graphic touches a defense agency, active bid, or product specification, obtain second-pair review before publishing.",
        "severity": "MEDIUM",
        "traffic_light": "GREEN",
        "prohibited_terms": [],
        "required_disclaimers": [],
        "allowed_examples": "Draft reviewed and approved by Comms/IR and Legal with documented audit trail.",
        "prohibited_examples": "Publishing unreviewed video cut directly to public YouTube or social channel.",
        "remediation_guidance": "Follow the three-reader test (Defense Buyer, Investor, Diligence Reviewer) and submit for pre-publication sign-off.",
        "sort_order": 80,
    },
    {
        "rule_number": "Rule 9",
        "category": "AI, Renders & Synthetic Media",
        "title": "AI Content: Never Fabricate Deceptive Photographic Proof",
        "summary": "Absolute rule: never generate an AI image or synthetic video that a reasonable viewer could mistake for photographic proof of an actual military operation, deployment, or customer adoption.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "synthetic combat proof",
            "ai generated military test",
            "fabricated telemetry",
            "fake warzone",
        ],
        "required_disclaimers": [
            "Conceptual visualization - depicts a potential future use, not an actual military operation, deployment or endorsement.",
            "Development concept - configuration and capabilities shown are illustrative and subject to change.",
            "Simulation or rendering - not test footage."
        ],
        "allowed_examples": "Clearly stylized conceptual render with required disclaimer watermark prominently displayed.",
        "prohibited_examples": "Photorealistic AI image of U.S. troops operating ZenaDrone in combat.",
        "remediation_guidance": "Apply unmistakable conceptual styling and affix the mandatory 'Conceptual visualization' or 'Simulation' label.",
        "sort_order": 90,
    },
    {
        "rule_number": "Rule 10",
        "category": "Product & Prototype Maturity",
        "title": "Development-Status Vocabulary: Approved Phrasing",
        "summary": "Enforces strict word choice between developmental milestones and commercial/operational claims.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "available now",
            "ready for deployment",
            "delivers operational capability",
            "proven solution",
            "production system",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Concept, intended use, prototype, undergoing flight testing, demonstrated specific cleared function.",
        "prohibited_examples": "Available now, delivers operational capability, validated and certified, production system.",
        "remediation_guidance": "Replace absolute or definitive operational terms with precise engineering progress markers.",
        "sort_order": 100,
    },
    {
        "rule_number": "Rule 11",
        "category": "Military Seals & Insignia",
        "title": "Prohibition on Military Seals, Word Marks & Insignia",
        "summary": "Do not use Department of Defense or Military Service seals, badges, or word marks decoratively or in co-branded lockups.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "dod seal",
            "army emblem",
            "air force logo",
            "navy seal",
            "army x zenadrone",
            "military lockup",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Standalone ZenaTech / ZenaDrone branding with neutral descriptive text.",
        "prohibited_examples": "Official DOD seal displayed next to ZenaDrone logo in thumbnail or video title card.",
        "remediation_guidance": "Remove military seals and insignia immediately. Defense trademark licensing strictly prohibits unauthorized non-official use.",
        "sort_order": 110,
    },
    {
        "rule_number": "Rule 12",
        "category": "Personnel, Uniforms & Badges",
        "title": "Personnel and Uniforms: Generic Silhouettes Required",
        "summary": "Default to generic, non-identifiable personnel, silhouettes, or operators viewed from behind. Never display recognizable active-duty service members endorsing commercial systems.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "identifiable servicemember",
            "visible name tape",
            "uniform endorsement",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Silhouettes, operators filmed from behind, generic contractor flight gear.",
        "prohibited_examples": "Close-up of military pilot in uniform smiling with ZenaDrone controller.",
        "remediation_guidance": "Crop, blur, or re-shoot to obscure faces, name tapes, and distinctive unit badges.",
        "sort_order": 120,
    },
    {
        "rule_number": "Rule 13",
        "category": "Defense Imagery & Video",
        "title": "Government / DVIDS Media Verification Standards",
        "summary": "Verify commercial reuse rights for DVIDS imagery. Distinguish between news/informational context and commercial/promotional product marketing.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [],
        "required_disclaimers": [
            "The appearance of U.S. Department of Defense visual information does not imply or constitute DOD endorsement."
        ],
        "allowed_examples": "Accredited DVIDS footage utilized for educational thought leadership with creator name, date, and disclaimer.",
        "prohibited_examples": "Using DVIDS military clips in a direct paid product promotion ad without release clearance.",
        "remediation_guidance": "Confirm asset metadata on DVIDS, ensure no third-party copyright, and attach mandatory non-endorsement attribution.",
        "sort_order": 130,
    },
    {
        "rule_number": "Rule 14",
        "category": "Investor Disclosure & Procurement",
        "title": "Procurement Integrity & Competitor Conduct",
        "summary": "Strict compliance with FAR 3.104 Procurement Integrity. Never comment on active bids, competitor standings, or source-selection insights.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "active rfp bid",
            "competitor proposal",
            "source selection leak",
            "disparage competitor drone",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Highlighting architectural advantages of modular dual-use autonomy without disparaging competing vendors.",
        "prohibited_examples": "Competitor X's aircraft crashed in testing; ZenaDrone is the only viable candidate for the tender.",
        "remediation_guidance": "Do not reference competitor technical evaluations or confidential procurement data.",
        "sort_order": 140,
    },
    {
        "rule_number": "Rule 15",
        "category": "Thought Leadership & Tone",
        "title": "Political and Institutional Neutrality",
        "summary": "The controversy dial must remain focused on ideas, not partisanship. Never attack political administrations, congressional leaders, or allied governments.",
        "severity": "MEDIUM",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "partisan attack",
            "attack administration",
            "unpatriotic defense",
            "political corruption in military",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Constructive economic analysis of attritable autonomous systems in contested logistics.",
        "prohibited_examples": "Partisan political commentary regarding defense budgets or named elected officials.",
        "remediation_guidance": "Elevate the discussion to mission capability, defense industrial base resilience, and technical architecture.",
        "sort_order": 150,
    },
]


async def setup():
    pool = get_pool()
    async with pool.acquire() as conn:
        logger.info("Setting up compliance_rulebooks and compliance_rules schema...")

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
        """)

        # Seed Rulebooks
        logger.info("Seeding base compliance rulebooks...")
        rulebook_ids = {}
        for rb in BASE_RULEBOOKS:
            rb_id = await conn.fetchval("""
                INSERT INTO compliance_rulebooks (title, version, effective_date, description, source_document)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT DO NOTHING
                RETURNING id;
            """, rb["title"], rb["version"], rb["effective_date"], rb["description"], rb["source_document"])
            if not rb_id:
                rb_id = await conn.fetchval("SELECT id FROM compliance_rulebooks WHERE title = $1 LIMIT 1", rb["title"])
            rulebook_ids[rb["title"]] = rb_id

        # Seed Rules
        logger.info("Seeding baseline compliance rules from Rulebook and Quick Use Guide...")
        primary_rb_id = rulebook_ids.get("ZenaTech & ZenaDrone Defense Thought Leadership & Imagery Rulebook")

        for rule in BASE_RULES:
            await conn.execute("""
                INSERT INTO compliance_rules (
                    rulebook_id, rule_number, category, title, summary,
                    severity, traffic_light, prohibited_terms, required_disclaimers,
                    allowed_examples, prohibited_examples, remediation_guidance,
                    sort_order, is_active, is_custom
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, true, false
                )
                ON CONFLICT (rule_number) DO UPDATE SET
                    category = EXCLUDED.category,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    severity = EXCLUDED.severity,
                    traffic_light = EXCLUDED.traffic_light,
                    prohibited_terms = EXCLUDED.prohibited_terms,
                    required_disclaimers = EXCLUDED.required_disclaimers,
                    allowed_examples = EXCLUDED.allowed_examples,
                    prohibited_examples = EXCLUDED.prohibited_examples,
                    remediation_guidance = EXCLUDED.remediation_guidance,
                    sort_order = EXCLUDED.sort_order,
                    updated_at = NOW();
            """,
                primary_rb_id,
                rule["rule_number"],
                rule["category"],
                rule["title"],
                rule["summary"],
                rule["severity"],
                rule["traffic_light"],
                rule["prohibited_terms"],
                rule["required_disclaimers"],
                rule["allowed_examples"],
                rule["prohibited_examples"],
                rule["remediation_guidance"],
                rule["sort_order"],
            )

        logger.info("Compliance schema and rules successfully initialized!")


async def _main():
    from postgresql_db.database import create_pool
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
