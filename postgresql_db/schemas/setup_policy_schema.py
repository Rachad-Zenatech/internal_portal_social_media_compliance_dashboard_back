"""Social media content policies: the platforms' own policy pages, what was read from them, and the rules the AI
proposes from them. A proposal joins the rulebook (in the Social Media Content Policy rulebook) only once a person
approves it.

policy_pages            one row per URL; several platforms and regions can share a page (Meta's standards cover
                        Instagram and Facebook)
policy_sources          which platform, region and policy name each page stands for
policy_page_versions    the page's text each time it changed, so a change can be traced to the rules made from it
policy_rule_proposals   rules the AI proposed from a version, with the existing rules they resemble, awaiting approval
"""

import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

POLICY_RULEBOOK_TITLE = "Social Media Content Policy"

# (region, platform code, policy name, url). "Global" applies to every region.
SEED_SOURCES = [
    ("Global", "TIKTOK", "Community Guidelines", "https://www.tiktok.com/community-guidelines/en/"),
    ("Global", "TIKTOK", "Commercial content disclosure",
     "https://ads.tiktok.com/resources/help/article/about-the-content-disclosure-setting-for-creators"),
    ("Global", "TIKTOK", "Commercial music rules",
     "https://support.tiktok.com/en/business-and-creator/creator-and-business-accounts/commercial-use-of-music-on-tiktok"),
    ("Global", "YOUTUBE", "Community policy hub", "https://www.youtube.com/howyoutubeworks/our-policies/"),
    ("Global", "YOUTUBE", "Branded Content Policies", "https://support.google.com/youtube/answer/17596007?hl=en"),
    ("Global", "YOUTUBE", "Sponsorship disclosure and restrictions", "https://support.google.com/youtube/answer/154235?hl=en"),
    ("Global", "YOUTUBE_SHORTS", "Branded Content Policies, including Shorts",
     "https://support.google.com/youtube/answer/17596007?hl=en"),
    ("Global", "YOUTUBE_SHORTS", "Shorts music rights", "https://support.google.com/youtube/answer/13053317?hl=en"),
    ("Global", "LINKEDIN", "Professional Community Policies", "https://www.linkedin.com/legal/professional-community-policies"),
    ("Global", "LINKEDIN", "Advertising Policies", "https://www.linkedin.com/legal/ads-policy"),
    ("Global", "INSTAGRAM", "Meta Community Standards", "https://transparency.meta.com/policies/community-standards/"),
    ("Global", "INSTAGRAM", "Advertising Standards", "https://transparency.meta.com/policies/ad-standards/"),
    ("Global", "INSTAGRAM", "Branded Content Policies", "https://www.facebook.com/business/help/221149188908254"),
    ("Global", "FACEBOOK", "Meta Community Standards", "https://transparency.meta.com/policies/community-standards/"),
    ("Global", "FACEBOOK", "Advertising Standards", "https://transparency.meta.com/policies/ad-standards/"),
    ("Global", "FACEBOOK", "Branded Content Policies", "https://www.facebook.com/business/help/221149188908254"),
    ("Global", "X", "X Rules", "https://help.x.com/en/rules-and-policies/x-rules"),
    ("Global", "X", "Paid Partnerships Policy", "https://help.x.com/en/rules-and-policies/paid-partnerships-policy"),
    ("Global", "X", "Advertising Policies", "https://help.x.com/en/business-and-advertising/ads-policies"),
    ("Global", "REDDIT", "Reddit Rules", "https://redditinc.com/policies/reddit-rules"),
    ("Global", "REDDIT", "Advertising Policy directory",
     "https://business.reddithelp.com/articles/Knowledge/Reddit-Advertising-Policy-Overview"),
    ("Global", "WEBSITE", "Google Ads destination requirements", "https://support.google.com/adspolicy/answer/6368661?hl=en"),
    ("Global", "WEBSITE", "LinkedIn landing-page requirements", "https://www.linkedin.com/help/linkedin/answer/a502710"),
]

_TIKTOK_MARKETS = "https://ads.tiktok.com/resources/help/article/branded-content-policy-country-specific-requirements?redirected=2"
_GOOGLE_POLITICAL = "https://support.google.com/adspolicy/answer/6014595?hl=en"
_GOOGLE_DESTINATION = "https://support.google.com/adspolicy/answer/6368661?hl=en"
_LINKEDIN_ADS = "https://www.linkedin.com/legal/ads-policy"
_LINKEDIN_COMMUNITY = "https://www.linkedin.com/legal/professional-community-policies"
_META_STANDARDS = "https://transparency.meta.com/policies/community-standards/"
_X_RULES = "https://help.x.com/en/rules-and-policies/x-rules"
_X_POLITICAL = "https://help.x.com/en/business-and-advertising/political-content"
_X_PARTNERSHIPS = "https://help.x.com/en/rules-and-policies/paid-partnerships-policy"
_REDDIT_ADS = "https://business.reddithelp.com/articles/Knowledge/Reddit-Advertising-Policy-Overview"
_META_CANADA = "https://about.fb.com/news/2023/06/changes-to-news-availability-on-our-platforms-in-canada/"
_META_EU = "https://about.fb.com/news/2025/07/ending-political-electoral-and-social-issue-advertising-in-the-eu/"

SEED_SOURCES += [
    # United States
    ("United States", "TIKTOK", "Market-specific branded content requirements", _TIKTOK_MARKETS),
    ("United States", "YOUTUBE", "US election-ad requirements", _GOOGLE_POLITICAL),
    ("United States", "YOUTUBE_SHORTS", "US election-ad requirements", _GOOGLE_POLITICAL),
    ("United States", "LINKEDIN", "Advertising Policies, including US provisions", _LINKEDIN_ADS),
    ("United States", "INSTAGRAM", "Global Community Standards", _META_STANDARDS),
    ("United States", "FACEBOOK", "Global Community Standards", _META_STANDARDS),
    ("United States", "X", "Political advertising requirements", _X_POLITICAL),
    ("United States", "REDDIT", "Global advertising policy directory", _REDDIT_ADS),
    ("United States", "WEBSITE", "Ad destination requirements", _GOOGLE_DESTINATION),
    # Canada
    ("Canada", "TIKTOK", "Market-specific branded content requirements", _TIKTOK_MARKETS),
    ("Canada", "YOUTUBE", "Canadian election and issue-ad requirements", _GOOGLE_POLITICAL),
    ("Canada", "YOUTUBE_SHORTS", "Canadian election and issue-ad requirements", _GOOGLE_POLITICAL),
    ("Canada", "LINKEDIN", "Advertising Policies, including Canada provisions", _LINKEDIN_ADS),
    ("Canada", "INSTAGRAM", "Canadian news restrictions", _META_CANADA),
    ("Canada", "FACEBOOK", "Canadian news restrictions", _META_CANADA),
    ("Canada", "X", "Global rules", _X_RULES),
    ("Canada", "X", "Political-ad eligibility by country", _X_POLITICAL),
    ("Canada", "REDDIT", "Global advertising policy directory", _REDDIT_ADS),
    ("Canada", "WEBSITE", "Ad destination requirements", _GOOGLE_DESTINATION),
    # Europe
    ("Europe", "TIKTOK", "Market-specific branded content requirements", _TIKTOK_MARKETS),
    ("Europe", "YOUTUBE", "EU and UK political-ad requirements", _GOOGLE_POLITICAL),
    ("Europe", "YOUTUBE_SHORTS", "EU and UK political-ad requirements", _GOOGLE_POLITICAL),
    ("Europe", "LINKEDIN", "Advertising Policies, including EU/UK provisions", _LINKEDIN_ADS),
    ("Europe", "INSTAGRAM", "EU political, electoral and social-issue ad restrictions", _META_EU),
    ("Europe", "FACEBOOK", "EU political, electoral and social-issue ad restrictions", _META_EU),
    ("Europe", "X", "Paid Partnerships Policy, including EU/UK restrictions", _X_PARTNERSHIPS),
    ("Europe", "REDDIT", "Advertising directory, including the UK HFSS policy", _REDDIT_ADS),
    ("Europe", "WEBSITE", "Google consent policy for EEA, UK and Switzerland",
     "https://www.google.com/about/company/user-consent-policy/"),
    # Middle East
    ("Middle East", "TIKTOK", "Market-specific branded content requirements", _TIKTOK_MARKETS),
    ("Middle East", "TIKTOK", "Alcohol advertising requirements by market",
     "https://ads.tiktok.com/resources/help/article/alcohol-market-specific-requirements"),
    ("Middle East", "YOUTUBE", "Political-ad requirements, including Israel", _GOOGLE_POLITICAL),
    ("Middle East", "YOUTUBE_SHORTS", "Political-ad requirements, including Israel", _GOOGLE_POLITICAL),
    ("Middle East", "LINKEDIN", "Global Professional Community Policies", _LINKEDIN_COMMUNITY),
    ("Middle East", "INSTAGRAM", "Global Community Standards", _META_STANDARDS),
    ("Middle East", "FACEBOOK", "Global Community Standards", _META_STANDARDS),
    ("Middle East", "X", "Political-ad eligibility and country restrictions", _X_POLITICAL),
    ("Middle East", "REDDIT", "Global advertising policy directory", _REDDIT_ADS),
    ("Middle East", "WEBSITE", "Ad destination requirements", _GOOGLE_DESTINATION),
    # Asia-Pacific
    ("Asia-Pacific", "TIKTOK", "Market-specific branded content requirements", _TIKTOK_MARKETS),
    ("Asia-Pacific", "YOUTUBE", "Country-specific election-ad requirements", _GOOGLE_POLITICAL),
    ("Asia-Pacific", "YOUTUBE_SHORTS", "Country-specific election-ad requirements", _GOOGLE_POLITICAL),
    ("Asia-Pacific", "LINKEDIN", "Global Professional Community Policies", _LINKEDIN_COMMUNITY),
    ("Asia-Pacific", "INSTAGRAM", "Global Community Standards", _META_STANDARDS),
    ("Asia-Pacific", "FACEBOOK", "Global Community Standards", _META_STANDARDS),
    ("Asia-Pacific", "X", "Paid Partnerships Policy, including Australia", _X_PARTNERSHIPS),
    ("Asia-Pacific", "X", "Political-ad eligibility by country", _X_POLITICAL),
    ("Asia-Pacific", "REDDIT", "Global advertising policy directory", _REDDIT_ADS),
    ("Asia-Pacific", "WEBSITE", "Ad destination requirements", _GOOGLE_DESTINATION),
]


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS policy_pages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                url TEXT NOT NULL UNIQUE,
                -- NEW: never read. READ: text saved. NEEDS_TEXT: the page wouldn't give its text (it needs a browser or
                -- blocks automated readers), so someone pastes it. FAILED: the last read failed.
                status VARCHAR(20) NOT NULL DEFAULT 'NEW',
                error TEXT,
                fetch_method VARCHAR(20),
                content_hash CHAR(64),
                last_fetched_at TIMESTAMP WITH TIME ZONE,
                last_changed_at TIMESTAMP WITH TIME ZONE,
                -- PENDING / RUNNING / DONE / FAILED: turning the latest version into proposed rules.
                proposal_status VARCHAR(20),
                proposal_error TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );

            CREATE TABLE IF NOT EXISTS policy_sources (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                page_id UUID NOT NULL REFERENCES policy_pages(id) ON DELETE CASCADE,
                platform VARCHAR(30) NOT NULL,
                region VARCHAR(50) NOT NULL,
                policy_name VARCHAR(300) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                UNIQUE (page_id, platform, region, policy_name)
            );

            CREATE TABLE IF NOT EXISTS policy_page_versions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                page_id UUID NOT NULL REFERENCES policy_pages(id) ON DELETE CASCADE,
                content_hash CHAR(64) NOT NULL,
                text TEXT NOT NULL,
                -- FETCHED or PASTED
                source VARCHAR(20) NOT NULL DEFAULT 'FETCHED',
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_policy_page_versions_page ON policy_page_versions(page_id, fetched_at DESC);

            CREATE TABLE IF NOT EXISTS policy_rule_proposals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                page_id UUID NOT NULL REFERENCES policy_pages(id) ON DELETE CASCADE,
                version_id UUID REFERENCES policy_page_versions(id) ON DELETE SET NULL,
                -- PENDING: waiting for a person. APPROVED: in the rulebook (rule_id). REJECTED. SUPERSEDED: the page
                -- changed and a newer proposal replaced it before anyone decided.
                status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                title VARCHAR(255) NOT NULL,
                summary TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'HIGH',
                traffic_light VARCHAR(20) NOT NULL DEFAULT 'YELLOW',
                evaluation_mode VARCHAR(10) NOT NULL DEFAULT 'CONTEXT',
                evaluation_guidance TEXT,
                prohibited_terms TEXT[] NOT NULL DEFAULT '{}',
                exception_terms TEXT[] NOT NULL DEFAULT '{}',
                required_disclaimers TEXT[] NOT NULL DEFAULT '{}',
                disclaimer_triggers TEXT[] NOT NULL DEFAULT '{}',
                remediation_guidance TEXT,
                platforms TEXT[] NOT NULL DEFAULT '{}',
                regions TEXT[] NOT NULL DEFAULT '{}',
                -- The requirement is only for paid ads (sponsored posts, promoted content).
                paid_ads_only BOOLEAN NOT NULL DEFAULT false,
                source_quote TEXT,
                -- Existing rules it resembles: [{rule_id, rule_number, title, overlap, reason, score}]
                similar_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
                rule_id UUID REFERENCES compliance_rules(id) ON DELETE SET NULL,
                review_note TEXT,
                reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
                reviewed_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_policy_rule_proposals_status ON policy_rule_proposals(status, created_at DESC);
        """)

        # The rulebook approved policy rules belong to (its own section on the rulebook page).
        await conn.execute("""
            INSERT INTO compliance_rulebooks (title, version, effective_date, description, source_document)
            VALUES ($1, '1.0', to_char(NOW(), 'YYYY-MM-DD'),
                    'Rules taken from the social media platforms'' own content and advertising policies, each approved '
                    'by a person before it applies. Each rule is limited to the platforms and regions its policy covers.',
                    'Platform policy pages (see Policy sources)')
            ON CONFLICT (title) DO NOTHING
        """, POLICY_RULEBOOK_TITLE)

        # Seed the sources; a URL is one page however many platforms and regions use it.
        for region, platform, name, url in SEED_SOURCES:
            page_id = await conn.fetchval("""
                INSERT INTO policy_pages (url) VALUES ($1)
                ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
                RETURNING id
            """, url)
            await conn.execute("""
                INSERT INTO policy_sources (page_id, platform, region, policy_name) VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
            """, page_id, platform, region, name)
    logger.info("Policy source schema ready")
