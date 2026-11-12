"""Messaging Context: reference material the analysis is given (brand voice, facts, platform norms, audience
profiles). Starter platform norms and audience profiles are seeded once by key; edits and removals are kept."""

import json
import logging

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

# Thought leadership accounts (Shaun's channels): every post on them is the CEO_THOUGHT_LEADERSHIP brand.
THOUGHT_LEADERSHIP = ["CEO_THOUGHT_LEADERSHIP"]
BRAND_MANUAL = "ZenaTech Brand Manual"

# Starter entries. Each is seeded only if its key has never existed, so edits (and removals) stick.
SEED_ENTRIES = [
    # ---- Brand voice: thought leadership accounts (from the ZenaTech Brand Manual) ----
    # The manual is a visual identity guide (colours, typefaces, logo); its words are kept as written, and the
    # voice entry comes from the Defense Thought Leadership Rulebook's voice standard.
    {
        "seed_key": "brand:CEO_THOUGHT_LEADERSHIP:colours",
        "kind": "BRAND_VOICE",
        "title": "Brand colours",
        "brands": THOUGHT_LEADERSHIP,
        "source": BRAND_MANUAL,
        "body": "Three main colours. Grand Bleu #005981 is the lead colour: a dark, cool blue that conveys trust, "
                "responsibility, calm, security and reliability, and suits a technology company's professionalism. "
                "Ottoman Red #ED2224 is the accent: bold and energetic, it draws attention and adds urgency, and it is "
                "the red of the dots in the ZENA wordmark. Steely Grey #939598 is the neutral: calm, understated and "
                "modern, used for supporting elements and for TECH in the logo. Tints and shades of these three, and "
                "black and white, complete the palette. Applies to designed graphics: title cards, text overlays, "
                "lower thirds, quote cards, slides and end cards. Unedited photos and footage of real people and "
                "operations don't need to be in brand colours.",
        "do_examples": "Grand Bleu backgrounds or headlines with white text; red for one point of emphasis (a key "
                       "word, a number, a divider); grey for secondary text and rules.",
        "dont_examples": "Colours outside the palette as the main colour of a graphic (purple, green, orange, neon "
                         "or rainbow gradients); red as the dominant background; low-contrast text, such as grey on "
                         "Grand Bleu at small sizes.",
    },
    {
        "seed_key": "brand:CEO_THOUGHT_LEADERSHIP:typography",
        "kind": "BRAND_VOICE",
        "title": "Brand typefaces",
        "brands": THOUGHT_LEADERSHIP,
        "source": BRAND_MANUAL,
        "body": "Poppins is the brand typeface, in its Light, Regular, Medium, SemiBold, Bold and Black weights. "
                "Montserrat is the second typeface. On-screen text, headlines and captions burned into images or "
                "video should be set in one of these two geometric sans serifs.",
        "do_examples": "Poppins SemiBold or Bold for headlines and key numbers; Poppins Regular or Montserrat for "
                       "body text and lower thirds.",
        "dont_examples": "Script, handwritten, novelty or serif display fonts; more than two typefaces in one graphic; "
                         "stretched or outlined text.",
    },
    {
        "seed_key": "brand:CEO_THOUGHT_LEADERSHIP:logo",
        "kind": "BRAND_VOICE",
        "title": "ZenaTech logo",
        "brands": THOUGHT_LEADERSHIP,
        "source": BRAND_MANUAL,
        "body": "The logo is the ZENA wordmark with two Ottoman Red dots, above TECH in Steely Grey. The manual shows "
                "four approved versions: white ZENA on Grand Bleu, white ZENA on black, Grand Bleu ZENA on white, and "
                "charcoal ZENA on light grey. TECH stays Steely Grey in every version.",
        "do_examples": "One of the four approved versions, on a plain background with clear space around it.",
        "dont_examples": "Recoloured, stretched, rotated, outlined or shadowed logos; the logo on a busy photo or on "
                         "a background with poor contrast; the dots in any colour but red; ZENA without TECH.",
    },
    {
        "seed_key": "brand:CEO_THOUGHT_LEADERSHIP:voice",
        "kind": "BRAND_VOICE",
        "title": "Thought leadership voice",
        "brands": THOUGHT_LEADERSHIP,
        "source": "Defense Thought Leadership Rulebook (voice standard)",
        "body": "Authoritative, plain-spoken and specific, never breathless. Confident about the issue and calibrated "
                "about the company's present maturity. Nonpartisan, and serious about conflict, never celebratory "
                "about destruction. Bold about the problem and the vision, precise about capability and development "
                "status, and ultra-precise about any military relationship. The look matches the voice: calm Grand "
                "Bleu, clean Poppins type and restrained use of red read as credible; loud colours, busy layouts "
                "and hype graphics undercut it.",
        "do_examples": "\"Defense does not have a sensor problem. It has a decision-speed problem.\" One clear point "
                       "per post, backed by a specific example or number.",
        "dont_examples": "Hype words (revolutionary, game-changing, unstoppable), exclamation-heavy copy, attacks on "
                         "named officials or agencies, and capability claims beyond the product's current stage.",
    },

    # ---- Platform norms ----
    {
        "seed_key": "platform:LINKEDIN",
        "kind": "PLATFORM_NORM",
        "title": "LinkedIn",
        "platforms": ["LINKEDIN"],
        "body": "Professional network; readers are executives, buyers, investors, partners and recruits reading at work. "
                "Expect a measured, credible, first-person-professional voice. Specific numbers, outcomes and lessons land "
                "well; hype, slang and heavy emoji read as unserious. Posts are often read as the company's or the "
                "executive's official position, so claims are taken at face value and screenshotted.",
        "do_examples": "Lead with the problem or result; short paragraphs; one clear point; precise capability language.",
        "dont_examples": "Clickbait hooks, emoji strings, hashtag walls (more than 3-5), sales pressure, unverified superlatives.",
    },
    {
        "seed_key": "platform:TIKTOK",
        "kind": "PLATFORM_NORM",
        "title": "TikTok",
        "platforms": ["TIKTOK"],
        "body": "Short vertical video for a broad, younger and largely non-specialist audience. Casual, energetic and "
                "visual-first; the first 2 seconds decide whether it's watched. Humour and trends are normal, but clips "
                "travel far beyond the intended audience and are often seen without context or sound.",
        "do_examples": "Strong visual hook, on-screen captions, plain language, one idea per clip.",
        "dont_examples": "Jargon, long setups, anything that only makes sense with the caption, jokes about weapons or harm.",
    },
    {
        "seed_key": "platform:X",
        "kind": "PLATFORM_NORM",
        "title": "X (Twitter)",
        "platforms": ["X"],
        "body": "Short, fast and public; posts are quoted, replied to and stripped of context quickly. Journalists, "
                "investors and critics watch it. Brevity rewards sharp statements, which also makes overstatement and "
                "ambiguity more costly.",
        "do_examples": "One precise claim, a link to the source, a calm tone.",
        "dont_examples": "Absolute or market-moving claims, sarcasm, replying in anger, threads that bury the caveat.",
    },
    {
        "seed_key": "platform:YOUTUBE",
        "kind": "PLATFORM_NORM",
        "title": "YouTube",
        "platforms": ["YOUTUBE"],
        "body": "Long-form, searchable and permanent; viewers choose to watch and expect depth, demonstration and "
                "explanation. Titles and thumbnails are judged for clickbait. Descriptions carry links and disclaimers.",
        "do_examples": "Show real footage and say what it is; put disclaimers in the description and on screen where needed.",
        "dont_examples": "Misleading thumbnails, renders presented as real footage, titles that promise more than the video shows.",
    },
    {
        "seed_key": "platform:YOUTUBE_SHORTS",
        "kind": "PLATFORM_NORM",
        "title": "YouTube Shorts",
        "platforms": ["YOUTUBE_SHORTS"],
        "body": "Short vertical video like TikTok, but surfaced to YouTube's broader and search-driven audience. Fast, "
                "visual, and often watched on loop without the description.",
        "do_examples": "Self-explanatory visuals, on-screen text for key facts, a clear label on concept footage.",
        "dont_examples": "Relying on the description for context or disclaimers.",
    },
    {
        "seed_key": "platform:INSTAGRAM",
        "kind": "PLATFORM_NORM",
        "title": "Instagram",
        "platforms": ["INSTAGRAM"],
        "body": "Visual-first, polished and aspirational; Reels behave like TikTok, feed posts like a portfolio. Captions "
                "are often skimmed, so the image or video carries the message.",
        "do_examples": "High-quality visuals, short captions, accurate alt text.",
        "dont_examples": "Text-heavy images, stock imagery presented as real operations, hashtag stuffing.",
    },
    {
        "seed_key": "platform:FACEBOOK",
        "kind": "PLATFORM_NORM",
        "title": "Facebook",
        "platforms": ["FACEBOOK"],
        "body": "Broad, older and community-oriented audience; posts are shared into groups and comment threads can turn "
                "political quickly. Friendly and informative works; contentious framing attracts argument.",
        "do_examples": "Plain explanations, local or community angles, clear calls to learn more.",
        "dont_examples": "Political or divisive framing, sensational headlines.",
    },
    {
        "seed_key": "platform:REDDIT",
        "kind": "PLATFORM_NORM",
        "title": "Reddit",
        "platforms": ["REDDIT"],
        "body": "Community-run and sceptical of marketing; users expect transparency about who is posting and why. "
                "Promotional tone is called out and can damage credibility.",
        "do_examples": "Disclose the company affiliation, answer questions directly, share substance.",
        "dont_examples": "Disguised promotion, marketing language, deleting critical replies.",
    },
    {
        "seed_key": "platform:WEBSITE",
        "kind": "PLATFORM_NORM",
        "title": "Website / blog",
        "platforms": ["WEBSITE"],
        "body": "The company's own record; read by customers, investors, journalists and regulators, and treated as the "
                "official, citable source. Accuracy and dates matter more than reach.",
        "do_examples": "Dated statements, sources for figures, clear distinction between current products and plans.",
        "dont_examples": "Undated claims, forward-looking statements without the safe-harbor line.",
    },
    # ---- Audience profiles ----
    # ---- Platform content policies (what each platform allows; a post that breaks them can be removed, limited or
    # get the account penalised). Kept as platform norms so the analysis checks every post against them. ----
    {
        "seed_key": "policy:META",
        "kind": "PLATFORM_NORM",
        "title": "Facebook & Instagram content policy (Meta)",
        "platforms": ["FACEBOOK", "INSTAGRAM"],
        "body": "Meta's Community Standards apply to every post, and its Advertising Standards to anything boosted or run "
                "as an ad. Paid or sponsored partnerships must use the Paid partnership label (Branded Content Policies). "
                "Misleading claims, fake engagement, and content that infringes someone else's copyright or trademark "
                "(music, footage, logos) can be removed or limit the account's reach. Ads about financial products, "
                "investment opportunities or social issues need extra authorisation and disclaimers. Photorealistic "
                "AI-generated or altered imagery may be labelled as AI by Meta; don't present it as real footage.",
        "do_examples": "Tag paid partners with the Paid partnership label; use licensed or Meta-cleared music; keep "
                       "claims verifiable; blur faces and number plates of people who didn't agree to appear.",
        "dont_examples": "Engagement bait (\"like if you agree\", \"tag 3 friends\"); guaranteed-return or get-rich "
                         "investment language; unlicensed music or clips; AI imagery passed off as real.",
        "source": "https://transparency.meta.com/policies/community-standards/ · "
                  "https://transparency.meta.com/policies/ad-standards/",
    },
    {
        "seed_key": "policy:LINKEDIN",
        "kind": "PLATFORM_NORM",
        "title": "LinkedIn content policy",
        "platforms": ["LINKEDIN"],
        "body": "LinkedIn's Professional Community Policies expect posts to be truthful, professional and not misleading; "
                "its Advertising Policies apply to sponsored content. False or misleading claims, spam and repetitive "
                "promotional posting, and harassment are removed. Ads for financial services and investments must be "
                "accurate, include required risk disclaimers and must not promise returns. Don't share confidential or "
                "personal information about colleagues, customers or other people.",
        "do_examples": "State facts you can back up; disclose sponsorship and partnerships; credit sources and "
                       "co-authors; keep promotional posts occasional.",
        "dont_examples": "Unverified superlatives or guaranteed results; posting the same promotion repeatedly; "
                         "sharing private details or screenshots of people without consent.",
        "source": "https://www.linkedin.com/legal/professional-community-policies · "
                  "https://www.linkedin.com/legal/ads-policy",
    },
    {
        "seed_key": "policy:TIKTOK",
        "kind": "PLATFORM_NORM",
        "title": "TikTok content policy",
        "platforms": ["TIKTOK"],
        "body": "TikTok's Community Guidelines apply to every video. Content that promotes a brand, product or service "
                "must turn on the content disclosure setting (branded content / promotional content). Dangerous acts "
                "that others might copy are restricted. Realistic AI-generated content must be labelled as AI-generated. "
                "Business accounts may only use music from the Commercial Music Library. Misleading claims and content "
                "about investments or financial schemes are restricted, and may be made ineligible for the For You feed.",
        "do_examples": "Turn on the content disclosure setting for promotional videos; use Commercial Music Library "
                       "tracks; label AI-generated visuals; show safety measures in demonstrations.",
        "dont_examples": "Trending songs outside the Commercial Music Library; unsafe stunts or drone flights near "
                         "people; unlabelled realistic AI footage; financial promises or 'easy money' framing.",
        "source": "https://www.tiktok.com/community-guidelines/en/ · "
                  "https://support.tiktok.com/en/business-and-creator/creator-and-business-accounts/commercial-use-of-music-on-tiktok",
    },
    {
        "seed_key": "policy:YOUTUBE",
        "kind": "PLATFORM_NORM",
        "title": "YouTube & Shorts content policy",
        "platforms": ["YOUTUBE", "YOUTUBE_SHORTS"],
        "body": "YouTube's Community Guidelines apply to videos and Shorts. Paid product placements, endorsements and "
                "sponsorships must be disclosed with the paid promotion setting (Branded Content Policies). Realistic "
                "altered or synthetic (AI) content must be disclosed when uploading. Copyrighted music or footage "
                "without rights leads to Content ID claims or copyright strikes. Harmful or dangerous acts, misleading "
                "metadata (titles, thumbnails that don't match the video) and spam are removed.",
        "do_examples": "Tick the paid promotion box for sponsored videos; disclose realistic AI or altered footage; use "
                       "licensed music; make titles and thumbnails match what the video shows.",
        "dont_examples": "Clickbait thumbnails or titles; unlicensed music; undisclosed sponsorships; risky "
                         "demonstrations without safety context.",
        "source": "https://www.youtube.com/howyoutubeworks/our-policies/ · "
                  "https://support.google.com/youtube/answer/154235?hl=en",
    },
    {
        "seed_key": "policy:X",
        "kind": "PLATFORM_NORM",
        "title": "X (Twitter) content policy",
        "platforms": ["X"],
        "body": "The X Rules apply to every post, and its Advertising Policies to promoted posts. Paid partnerships must "
                "be disclosed (Paid Partnerships Policy). Misleading or deceptive synthetic or manipulated media can be "
                "labelled or removed. Platform manipulation and spam (duplicate posts, artificial engagement) are "
                "prohibited. Financial products and services in ads are restricted and must follow local law.",
        "do_examples": "Disclose paid partnerships (e.g. #ad / #sponsored); link to sources; post original content.",
        "dont_examples": "Posting the same text across many accounts; manipulated media presented as real; "
                         "unqualified investment or price predictions.",
        "source": "https://help.x.com/en/rules-and-policies/x-rules · "
                  "https://help.x.com/en/rules-and-policies/paid-partnerships-policy",
    },
    {
        "seed_key": "audience:GENERAL",
        "kind": "AUDIENCE_PROFILE",
        "title": "General audience",
        "audiences": ["GENERAL"],
        "body": "Non-specialists with no background on drones, defense or enterprise software. They read quickly, take "
                "claims literally and react to tone and imagery more than detail. Military or surveillance themes can "
                "worry them; jargon loses them.",
        "do_examples": "Plain language, everyday benefits (safety, speed, savings), clear labels on concepts.",
        "dont_examples": "Acronyms, weapons imagery without context, anything that sounds like surveillance of the public.",
    },
    {
        "seed_key": "audience:INVESTORS",
        "kind": "AUDIENCE_PROFILE",
        "title": "Investors",
        "audiences": ["INVESTORS"],
        "body": "Retail and institutional investors in a Nasdaq-listed company (ZENA). They read posts as signals about "
                "revenue, growth and risk, and may act on them. Anything that sounds like a forecast, a partnership, a "
                "contract or commercial availability is weighed as material information.",
        "do_examples": "Facts that are already public, dated milestones, the safe-harbor line on forward-looking statements.",
        "dont_examples": "Price or return predictions, unannounced deals, 'shipping'/'deployed' for products still in trials.",
    },
    {
        "seed_key": "audience:DEFENSE",
        "kind": "AUDIENCE_PROFILE",
        "title": "Defense & government",
        "audiences": ["DEFENSE"],
        "body": "U.S. and allied defense buyers, program managers and operators. Highly sensitive to overclaiming, implied "
                "endorsement and operational security. They value precision about development status, test conditions "
                "and what has actually been demonstrated, and distrust marketing language.",
        "do_examples": "Exact maturity terms (prototype, in testing, pilot), specific test conditions, respect for the mission.",
        "dont_examples": "Implied DoD endorsement, 'battle-proven' claims, celebrating destruction, criticising agencies or officials.",
    },
    {
        "seed_key": "audience:COMMERCIAL",
        "kind": "AUDIENCE_PROFILE",
        "title": "Commercial customers",
        "audiences": ["COMMERCIAL"],
        "body": "Businesses evaluating drone services and software (agriculture, inspection, construction, enterprise "
                "productivity). They care about ROI, reliability, compliance with aviation rules, support and "
                "availability in their region.",
        "do_examples": "Concrete outcomes, where and when it's available, what's included.",
        "dont_examples": "Guarantees, regulatory claims that aren't exact, availability claims for regions not served.",
    },
]


# The tables: the entries, the sources they are read from, brand voice, and the policies that are watched.
_SCHEMA_SQL = """
            CREATE TABLE IF NOT EXISTS messaging_context (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                kind VARCHAR(30) NOT NULL,
                title VARCHAR(200) NOT NULL,
                body TEXT NOT NULL,
                do_examples TEXT,
                dont_examples TEXT,
                -- Scope: empty means every brand / audience / platform.
                brands TEXT[] NOT NULL DEFAULT '{}',
                audiences TEXT[] NOT NULL DEFAULT '{}',
                platforms TEXT[] NOT NULL DEFAULT '{}',
                source VARCHAR(300),
                as_of DATE,
                is_active BOOLEAN NOT NULL DEFAULT true,
                -- Starter entries are seeded once by this key; NULL for entries people add.
                seed_key VARCHAR(100) UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
                deleted_at TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_messaging_context_kind ON messaging_context(kind) WHERE deleted_at IS NULL;
            -- Facts read from a source (the ZenaTech newsroom): the article, the fact as last generated (so a person's
            -- rewrite can be told apart and kept) and a hash of the article text (so only changed articles are re-read).
            ALTER TABLE messaging_context ADD COLUMN IF NOT EXISTS source_url TEXT;
            ALTER TABLE messaging_context ADD COLUMN IF NOT EXISTS auto_source VARCHAR(30);
            ALTER TABLE messaging_context ADD COLUMN IF NOT EXISTS auto_body TEXT;
            ALTER TABLE messaging_context ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messaging_context_source_url ON messaging_context(source_url);
            -- Each sync of a source: when, what it found and did, and why it failed.
            CREATE TABLE IF NOT EXISTS messaging_context_syncs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source VARCHAR(30) NOT NULL,
                trigger VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
                started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMP WITH TIME ZONE,
                found INT NOT NULL DEFAULT 0,
                added INT NOT NULL DEFAULT 0,
                updated INT NOT NULL DEFAULT 0,
                unchanged INT NOT NULL DEFAULT 0,
                skipped INT NOT NULL DEFAULT 0,
                failed INT NOT NULL DEFAULT 0,
                error TEXT,
                started_by UUID REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messaging_context_syncs_source ON messaging_context_syncs(source, started_at DESC);

            -- Brand voice per brand, read from its brand manual (PDF): voice, tone, do's and don'ts, colours,
            -- typefaces and the logo. Kept in the Messaging Context as the brand's Brand Voice entry (auto_source
            -- BRAND_MANUAL) so the analysis uses it.
            CREATE TABLE IF NOT EXISTS brand_voice_profiles (
                brand_code VARCHAR(50) PRIMARY KEY REFERENCES brands(code) ON DELETE CASCADE,
                manual_name VARCHAR(300),
                manual_path TEXT,
                manual_size BIGINT,
                manual_uploaded_at TIMESTAMP WITH TIME ZONE,
                manual_uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'NONE',
                error TEXT,
                processing_started_at TIMESTAMP WITH TIME ZONE,
                voice TEXT,
                tone TEXT[] NOT NULL DEFAULT '{}',
                do_examples TEXT[] NOT NULL DEFAULT '{}',
                dont_examples TEXT[] NOT NULL DEFAULT '{}',
                colors JSONB NOT NULL DEFAULT '[]'::jsonb,
                typefaces JSONB NOT NULL DEFAULT '[]'::jsonb,
                logo_path TEXT,
                logo_mime VARCHAR(50),
                logo_notes TEXT,
                -- Rows of swatches in the manual (tints, shades...) by main colour: [{base_hex, label, colors}].
                palette JSONB NOT NULL DEFAULT '[]'::jsonb,
                extracted_at TIMESTAMP WITH TIME ZONE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL
            );
            ALTER TABLE brand_voice_profiles ADD COLUMN IF NOT EXISTS palette JSONB NOT NULL DEFAULT '[]'::jsonb;

            -- Platform content policies are watched (policy_watch_service): each official policy page's text as last
            -- read, so a change can be spotted and shown to the AI that drafts the norm's update.
            CREATE TABLE IF NOT EXISTS policy_norm_pages (
                url TEXT PRIMARY KEY,
                text_hash VARCHAR(64),
                page_text TEXT,
                fetched_at TIMESTAMP WITH TIME ZONE,
                changed_at TIMESTAMP WITH TIME ZONE,
                error TEXT
            );
            -- An update to a content-policy norm drafted after its policy page changed. A super admin approves it
            -- (it then replaces the norm's wording) or rejects it; nothing changes the norm on its own.
            CREATE TABLE IF NOT EXISTS policy_norm_proposals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                context_id UUID NOT NULL REFERENCES messaging_context(id) ON DELETE CASCADE,
                source_urls TEXT[] NOT NULL DEFAULT '{}',
                -- Short points saying what changed in the policy and in the norm.
                changes JSONB NOT NULL DEFAULT '[]'::jsonb,
                -- The norm's wording when this was drafted, and the drafted wording.
                current_body TEXT,
                current_do_examples TEXT,
                current_dont_examples TEXT,
                body TEXT NOT NULL,
                do_examples TEXT,
                dont_examples TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMP WITH TIME ZONE,
                decided_by UUID REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_policy_norm_proposals_status ON policy_norm_proposals(status, created_at DESC);
"""


async def _seed_starter_entries(conn) -> None:
    """The starter entries, added once each (by their key); what people edit or turn off is left alone."""
    await conn.execute(
        """
        INSERT INTO messaging_context (seed_key, kind, title, body, do_examples, dont_examples, brands, audiences,
                                       platforms, source)
        SELECT e.seed_key, e.kind, e.title, e.body, e.do_examples, e.dont_examples,
               ARRAY(SELECT jsonb_array_elements_text(coalesce(e.brands, '[]'))),
               ARRAY(SELECT jsonb_array_elements_text(coalesce(e.audiences, '[]'))),
               ARRAY(SELECT jsonb_array_elements_text(coalesce(e.platforms, '[]'))),
               e.source
        FROM jsonb_to_recordset($1::jsonb) AS e(
            seed_key text, kind text, title text, body text, do_examples text, dont_examples text,
            brands jsonb, audiences jsonb, platforms jsonb, source text
        )
        ON CONFLICT (seed_key) DO NOTHING
        """,
        json.dumps(SEED_ENTRIES),
    )


async def setup() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await _seed_starter_entries(conn)
    logger.info("Messaging context schema ready")
