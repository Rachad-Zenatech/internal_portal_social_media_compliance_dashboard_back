"""Setup Compliance Rulebooks and Rules Schema
Creates compliance_rulebooks and compliance_rules tables.
Populates base rules from:
- ZenaTech and ZenaDrone Defense Thought Leadership and Imagery Rulebook Oct. 2026
- ZenaTech and ZenaDrone Defense Content Quick Use Guide, Oct. 2026
"""

import asyncio
import json
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
            "procurement is broken",
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
            "demand from defense customers is accelerating",
            "strong defense demand",
            "defense backlog",
            "defense bookings",
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
            "export controlled",
            "flight path",
            "test schedule",
            "encryption key",
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
            "deployable solution",
            "fielded platform",
            "program of record",
            "customer adoption",
            "strategic partner",
            "broad deployment",
            "blue uas certified",
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
            "down to the final two",
            "evaluation team has been extremely positive",
            "evaluation team has been very positive",
            "expect the army decision",
            "competitor missed a requirement",
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
            "washington ignored",
        ],
        "required_disclaimers": [],
        "allowed_examples": "Constructive economic analysis of attritable autonomous systems in contested logistics.",
        "prohibited_examples": "Partisan political commentary regarding defense budgets or named elected officials.",
        "remediation_guidance": "Elevate the discussion to mission capability, defense industrial base resilience, and technical architecture.",
        "sort_order": 150,
    },
    {
        "rule_number": "Rule 16",
        "category": "Product & Prototype Maturity",
        "title": "Separate Vision from Current Capability",
        "summary": "Be bold about where defense is heading, but statements about what ZenaDrone can do today must match documented test evidence and current status. Anything the company might, plans to or aspires to do must be qualified as such. Avoid certainty words (will, proves, solves, guarantees, promises) and unsubstantiated superlatives unless evidence supports them.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "guarantees",
            "solves",
            "has solved",
            "eliminates",
            "military grade",
            "battlefield ready",
            "game changing",
            "trusted by the military",
            "fielded by",
            "advantage today",
        ],
        "required_disclaimers": [],
        "allowed_examples": "A $2,000 drone should not require a million-dollar response. We believe lower-cost autonomous interception will be part of the answer; that challenge is informing ZenaDrone's counter-UAS development work.",
        "prohibited_examples": "ZenaDrone has solved the counter-UAS cost problem for the U.S. military. ZenaDrone swarms give U.S. forces that advantage today.",
        "remediation_guidance": "Use \"designed to\", \"intended to\", \"we believe\", \"could\" or \"may\" for objectives and predictions. Connect to ZenaDrone as \"a problem driving our development work\", not \"a problem we have solved\". Cite and date any number in a headline.",
        "sort_order": 160,
    },
    {
        "rule_number": "Rule 17",
        "category": "Thought Leadership & Tone",
        "title": "Mission-Oriented and Humane: No Theatrical Lethality",
        "summary": "Discuss conflict professionally, never celebrating destruction. Ideally do not address lethality directly; emphasize force protection, mission effectiveness, survivability, decision advantage and human consequences.",
        "severity": "MEDIUM",
        "traffic_light": "YELLOW",
        "prohibited_terms": [
            "lethality",
            "kill count",
            "confirmed kills",
            "obliterate",
            "annihilate",
            "wipe out the enemy",
            "destroy the enemy",
        ],
        "required_disclaimers": [],
        "allowed_examples": "The most consequential autonomous aircraft may be the one carrying blood, batteries or critical supplies - not a weapon.",
        "prohibited_examples": "Watch our drones obliterate the enemy. Maximum lethality at minimum cost.",
        "remediation_guidance": "Reframe around force protection, survivability, reduced human exposure and mission outcomes. Keep explosions, kills and intercept outcomes out of copy and visuals.",
        "sort_order": 170,
    },
    {
        "rule_number": "Rule 18",
        "category": "Governance & Workflow",
        "title": "Substantiate Every Claim and Keep the Evidence File",
        "summary": "Every factual statement belongs to a claim class (opinion, industry fact, company fact, product, status, government relationship, procurement/financial) with matching support and reviewer. Keep dated support for specifications, test results, status, customer relationships, certifications, statistics and imagery rights.",
        "severity": "MEDIUM",
        "traffic_light": "YELLOW",
        "prohibited_terms": [],
        "required_disclaimers": [],
        "allowed_examples": "Industry statistic with its current primary source, date, scope and link; product specification matching the approved status register.",
        "prohibited_examples": "Statistics or specifications quoted from memory, an informal conversation or an outdated deck.",
        "remediation_guidance": "For each yellow/red item keep: final copy, creative and caption; platform and publish date; approver names and timestamps; the specification, test record, filing or customer authorization; image licenses and releases; and AI prompts/source files with the concept label.",
        "sort_order": 180,
    },
    {
        "rule_number": "Rule 19",
        "category": "Government & Customer Claims",
        "title": "Real Tests, Demos and Pilots: Release First, Caption Exactly",
        "summary": "Real test, demonstration and customer-pilot imagery is the highest-risk category because it can imply selection or adoption. Do not publish until the owner confirms the organization may be named, photography and promotional use were authorized, every person, location and background detail is releasable, and the exact objective and result may be disclosed.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [
            "puts zenadrone to the test",
            "warfighters validate",
            "validate zenadrone",
            "customer deploys zenadrone",
            "government validated",
            "government approved",
        ],
        "required_disclaimers": [],
        "allowed_examples": "\"ZenaDrone 1000 during a development test conducted on [date], focused on [cleared objective].\" \"Paid customer pilot evaluating [defined use], subject to pilot scope.\"",
        "prohibited_examples": "\"The U.S. military puts ZenaDrone to the test.\" \"Warfighters validate ZenaDrone.\" \"Customer deploys ZenaDrone.\"",
        "remediation_guidance": "Check the NDA, contract, test plan, public-affairs instructions, CUI markings and export controls. The caption must not turn observation into validation, evaluation into selection or a pilot into deployment.",
        "sort_order": 190,
    },
    {
        "rule_number": "Rule 20",
        "category": "Defense Imagery & Video",
        "title": "Image Editing: Never Change What Happened",
        "summary": "Cropping, tonal correction, background cleanup, privacy/security blurring and clearly conceptual, documented compositing are permitted. Edits that change who attended, what equipment was present, what occurred, the apparent test result, the product configuration or the government relationship are prohibited.",
        "severity": "CRITICAL",
        "traffic_light": "RED",
        "prohibited_terms": [],
        "required_disclaimers": [],
        "allowed_examples": "Blurring a badge or screen for security, then confirming the remaining image is not misleading.",
        "prohibited_examples": "Adding a payload that was not fitted, or removing an observer so a test looks like a solo customer operation.",
        "remediation_guidance": "If an edit removes a marking for compliance, confirm the result is not misleading and keep the original securely.",
        "sort_order": 200,
    },
    {
        "rule_number": "Rule 21",
        "category": "Defense Imagery & Video",
        "title": "Image Sources, Rights and Releases",
        "summary": "Prefer original company imagery under company releases, then labeled conceptual renders, then licensed stock with releases, and government imagery only when cleared. Never use scraped social media, unlabeled reposts, images of uncertain provenance or AI assets with unclear commercial rights. Publicly available does not mean commercially usable.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [],
        "required_disclaimers": [],
        "allowed_examples": "Licensed stock with model/property releases, credited and logged with the approval.",
        "prohibited_examples": "A drone photo pulled from another account's post, or an AI asset generated with a tool whose commercial terms are unknown.",
        "remediation_guidance": "Document commercial rights, model/property releases, photographer credit and source-specific permissions for every image, person, mark, clip and music track before publishing.",
        "sort_order": 210,
    },
    {
        "rule_number": "Rule 22",
        "category": "Governance & Workflow",
        "title": "Comments, Replies and Amplification Follow the Same Rules",
        "summary": "Replies, reposts, video descriptions, comments and DMs are governed by the same rules as the original post. Do not confirm rumors, customer names, procurement stages, test results or timelines, and do not improvise specifications. Employee or paid-partner amplification must not add new claims and must disclose employment or payment connections.",
        "severity": "HIGH",
        "traffic_light": "YELLOW",
        "prohibited_terms": [],
        "required_disclaimers": [],
        "allowed_examples": "\"Good question - the team will follow up through the proper channel,\" with a link to an approved public source.",
        "prohibited_examples": "Confirming in a reply that a named agency is evaluating ZenaDrone, or arguing with a journalist or service member in the comments.",
        "remediation_guidance": "Acknowledge a substantive counterpoint and return to the issue. For an important error or sensitive disclosure, preserve a screenshot, stop replying and contact Comms/IR and Legal immediately.",
        "sort_order": 220,
    },
]


DEFENSE = "ZenaDrone Defense"
DISCLOSURE = "Public Company Disclosure"
CEO = "Shaun CEO Thought Leadership"

# Which rule sets each baseline rule belongs to. All of them come from the defense rulebook; the
# investor-disclosure rules also apply to all public-company content, and the tone rules to Shaun's
# thought leadership.
BASE_RULE_SETS = {
    "Rule 1": [DEFENSE, CEO],
    "Rule 6": [DEFENSE, DISCLOSURE],
    "Rule 14": [DEFENSE, DISCLOSURE],
    "Rule 15": [DEFENSE, CEO],
    "Rule 16": [DEFENSE, CEO],
    "Rule 17": [DEFENSE, CEO],
    "Rule 18": [DEFENSE, DISCLOSURE],
    "Rule 22": [DEFENSE, CEO, DISCLOSURE],
}


def _base_rules_json() -> str:
    return json.dumps([{**rule, "rule_sets": BASE_RULE_SETS.get(rule["rule_number"], [DEFENSE])} for rule in BASE_RULES])


# Reads BASE_RULES (passed as JSON in $2) as rows, so all baseline rules go in one statement.
_BASE_RULES_SELECT = """
    SELECT $1::uuid, r.rule_number, r.category, r.title, r.summary,
           r.severity, r.traffic_light,
           ARRAY(SELECT jsonb_array_elements_text(r.prohibited_terms)),
           ARRAY(SELECT jsonb_array_elements_text(r.required_disclaimers)),
           r.allowed_examples, r.prohibited_examples, r.remediation_guidance,
           r.sort_order, ARRAY(SELECT jsonb_array_elements_text(r.rule_sets)), true, false
    FROM jsonb_to_recordset($2::jsonb) AS r(
        rule_number text, category text, title text, summary text, severity text, traffic_light text,
        prohibited_terms jsonb, required_disclaimers jsonb, allowed_examples text, prohibited_examples text,
        remediation_guidance text, sort_order int, rule_sets jsonb
    )
"""


async def setup(reset: bool = False):
    """Create the tables and add any missing baseline rules.

    Startup never overwrites existing rules, so edits made in the rulebook screen survive restarts and
    removed baseline rules stay removed. reset=True ("Re-sync Rulebook") puts every baseline rule back
    to the rulebook's wording, scope and active state; custom rules are left alone.
    """
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

            -- Soft delete, so removed baseline rules aren't re-seeded and history still resolves
            ALTER TABLE compliance_rules ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;
        """)

        # Scope columns. They're added without a default so the first run can tell which rows still
        # need their baseline scope; after the backfill below they're non-null with an empty default.
        new_scope = await conn.fetchval("""
            SELECT NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'compliance_rules' AND column_name = 'rule_sets'
            )
        """)
        await conn.execute("""
            ALTER TABLE compliance_rules
                ADD COLUMN IF NOT EXISTS rule_sets TEXT[],
                ADD COLUMN IF NOT EXISTS brands TEXT[],
                ADD COLUMN IF NOT EXISTS audiences TEXT[],
                ADD COLUMN IF NOT EXISTS regions TEXT[],
                ADD COLUMN IF NOT EXISTS platforms TEXT[];
        """)
        if new_scope:
            logger.info("Backfilling rule sets on existing baseline rules...")
            await conn.execute("""
                UPDATE compliance_rules c
                SET rule_sets = ARRAY(SELECT jsonb_array_elements_text(r.rule_sets))
                FROM jsonb_to_recordset($1::jsonb) AS r(rule_number text, rule_sets jsonb)
                WHERE c.rule_number = r.rule_number AND NOT c.is_custom
            """, _base_rules_json())
        await conn.execute("""
            UPDATE compliance_rules SET
                rule_sets = COALESCE(rule_sets, '{}'), brands = COALESCE(brands, '{}'),
                audiences = COALESCE(audiences, '{}'), regions = COALESCE(regions, '{}'),
                platforms = COALESCE(platforms, '{}')
            WHERE rule_sets IS NULL OR brands IS NULL OR audiences IS NULL OR regions IS NULL OR platforms IS NULL;

            ALTER TABLE compliance_rules
                ALTER COLUMN rule_sets SET DEFAULT '{}', ALTER COLUMN rule_sets SET NOT NULL,
                ALTER COLUMN brands SET DEFAULT '{}', ALTER COLUMN brands SET NOT NULL,
                ALTER COLUMN audiences SET DEFAULT '{}', ALTER COLUMN audiences SET NOT NULL,
                ALTER COLUMN regions SET DEFAULT '{}', ALTER COLUMN regions SET NOT NULL,
                ALTER COLUMN platforms SET DEFAULT '{}', ALTER COLUMN platforms SET NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_compliance_rules_rule_sets ON compliance_rules USING GIN (rule_sets);
        """)

        # Earlier versions added a duplicate rulebook row on every startup. Keep the oldest per title,
        # point rules at it, and make titles unique so it can't happen again.
        dupes = """
            SELECT id, first_value(id) OVER (PARTITION BY title ORDER BY created_at, id) AS keep_id
            FROM compliance_rulebooks
        """
        await conn.execute(f"""
            UPDATE compliance_rules c SET rulebook_id = d.keep_id
            FROM ({dupes}) d WHERE c.rulebook_id = d.id AND d.id <> d.keep_id;
        """)
        await conn.execute(f"""
            DELETE FROM compliance_rulebooks b USING ({dupes}) d
            WHERE b.id = d.id AND d.id <> d.keep_id;
        """)
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS compliance_rulebooks_title_unique ON compliance_rulebooks(title);"
        )

        # Seed Rulebooks
        logger.info("Seeding base compliance rulebooks...")
        rulebook_ids = {}
        for rb in BASE_RULEBOOKS:
            rb_id = await conn.fetchval("""
                INSERT INTO compliance_rulebooks (title, version, effective_date, description, source_document)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (title) DO NOTHING
                RETURNING id;
            """, rb["title"], rb["version"], rb["effective_date"], rb["description"], rb["source_document"])
            if not rb_id:
                rb_id = await conn.fetchval("SELECT id FROM compliance_rulebooks WHERE title = $1 LIMIT 1", rb["title"])
            rulebook_ids[rb["title"]] = rb_id

        # Seed Rules
        logger.info("Seeding baseline compliance rules from Rulebook and Quick Use Guide...")
        primary_rb_id = rulebook_ids.get("ZenaTech & ZenaDrone Defense Thought Leadership & Imagery Rulebook")

        insert = f"""
            INSERT INTO compliance_rules (
                rulebook_id, rule_number, category, title, summary,
                severity, traffic_light, prohibited_terms, required_disclaimers,
                allowed_examples, prohibited_examples, remediation_guidance,
                sort_order, rule_sets, is_active, is_custom
            )
            {_BASE_RULES_SELECT}
        """
        if reset:
            # A custom rule that took a baseline number is left as it is.
            await conn.execute(insert + """
                ON CONFLICT (rule_number) DO UPDATE SET
                    rulebook_id = EXCLUDED.rulebook_id,
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
                    rule_sets = EXCLUDED.rule_sets,
                    brands = '{}', audiences = '{}', regions = '{}', platforms = '{}',
                    is_active = true,
                    deleted_at = NULL,
                    updated_at = NOW()
                WHERE NOT compliance_rules.is_custom
            """, primary_rb_id, _base_rules_json())
        else:
            await conn.execute(insert + " ON CONFLICT (rule_number) DO NOTHING", primary_rb_id, _base_rules_json())

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
