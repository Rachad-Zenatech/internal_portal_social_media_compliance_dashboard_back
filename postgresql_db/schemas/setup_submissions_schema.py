"""Setup Content Submission Schema
Creates content_submissions with their files, versions, review jobs and activity events,
and (in development only) seeds clearly marked demo social accounts so the
"connected accounts only" picker has something to show before real OAuth exists."""
import asyncio
import logging
import os

from postgresql_db.database import get_pool, close_pool
from postgresql_db.schemas.submission_tables_schema import setup_submission_tables
from postgresql_db.schemas.creator_flag_dismissal_requests_schema import ensure_creator_flag_dismissal_requests
from postgresql_db.schemas.setup_source_links_schema import ensure_source_links_column
from postgresql_db.schemas.setup_ai_usage_schema import ensure_ai_usage_events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
async def setup():
    pool = get_pool()
    async with pool.acquire() as conn:
        logger.info("Setting up content submission schema...")
        await setup_submission_tables(conn)
        await ensure_ai_usage_events(conn)
        await ensure_creator_flag_dismissal_requests(conn)

        # The five brands content is filed under. The software products (ZooOffice, DeskFlex, Othership,
        # Zena Studios) roll up into "Software Brands"; Shaun's own posts are their own brand.
        await conn.execute("""
            INSERT INTO brands (code, name, sort_order, is_active)
            VALUES
                ('ZENATECH', 'ZenaTech', 10, true),
                ('ZENADRONE', 'ZenaDrone', 20, true),
                ('DAAS', 'Drone as a Service', 30, true),
                ('SOFTWARE', 'Software Brands', 40, true),
                ('CEO_THOUGHT_LEADERSHIP', 'Thought Leadership', 50, true)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sort_order = EXCLUDED.sort_order, is_active = true;
        """)
        await _retire_product_brands(conn)
        await _sync_script_checks(conn)

        if os.getenv("APP_ENV", "development").lower() != "production":
            await _seed_demo_social_accounts(conn)

        logger.info("Content submission schema setup successfully completed!")


async def _sync_script_checks(conn):
    await ensure_source_links_column(conn)
    """While a script is still being decided, the post's saved check and its latest script version's check are the same
    check (see script_review_service._adopt_check). Brings older rows into line: the newer of the two is copied to the
    other, so the creator and the reviewer see one score. Safe on every start: matching rows are left as they are."""
    await conn.execute("""
        WITH latest AS (
            SELECT s.id, s.latest_check AS post, v.check_result AS script, v.version_number,
                   coalesce((s.latest_check->>'checked_at')::timestamptz, 'epoch'::timestamptz) AS post_at, v.created_at AS script_at
            FROM content_submissions s
            JOIN content_script_versions v ON v.submission_id = s.id
             AND v.version_number = (SELECT max(version_number) FROM content_script_versions WHERE submission_id = s.id)
            WHERE s.deleted_at IS NULL AND s.script_status IN ('IN_REVIEW', 'CHANGES_REQUESTED', 'EDITS_PROPOSED')
              AND v.check_result IS NOT NULL AND s.latest_check IS DISTINCT FROM v.check_result
        ), to_script AS (
            UPDATE content_script_versions v SET check_result = l.post
            FROM latest l
            WHERE l.post IS NOT NULL AND l.post_at > l.script_at AND v.submission_id = l.id AND v.version_number = l.version_number
            RETURNING v.submission_id
        )
        UPDATE content_submissions s
        SET latest_check = coalesce(l.post, '{}'::jsonb) || l.script || '{"analysis_pending": false, "documents_pending": 0}'::jsonb
        FROM latest l
        WHERE s.id = l.id AND (l.post IS NULL OR l.post_at <= l.script_at)
    """)


# Individual software products that used to be separate brands; they're filed under "Software Brands" now.
RETIRED_PRODUCT_BRANDS = ["DESKFLEX", "OTHERSHIP", "ZOOOFFICE", "ZENASTUDIOS"]


async def _retire_product_brands(conn):
    """Hide the old product brands and move everything filed under them to SOFTWARE, and Shaun's accounts and posts
    to CEO_THOUGHT_LEADERSHIP. Safe to run on every start: once moved there's nothing left to change. The retired
    brands stay in the table (inactive) so nothing that pointed at them breaks."""
    retired = RETIRED_PRODUCT_BRANDS
    await conn.execute("UPDATE brands SET is_active = false WHERE code = ANY($1::text[]) AND is_active", retired)
    await conn.execute(
        "UPDATE content_submissions SET brand_code = 'SOFTWARE' WHERE brand_code = ANY($1::text[])", retired
    )
    # Brand access: anyone on a retired brand works on Software Brands instead.
    await conn.execute(
        """
        INSERT INTO user_brands (user_id, brand_code, assigned_by)
        SELECT DISTINCT user_id, 'SOFTWARE', NULL::uuid FROM user_brands WHERE brand_code = ANY($1::text[])
        ON CONFLICT (user_id, brand_code) DO NOTHING
        """,
        retired,
    )
    await conn.execute("DELETE FROM user_brands WHERE brand_code = ANY($1::text[])", retired)
    await conn.execute(
        """
        UPDATE compliance_rules
        SET brands = ARRAY(SELECT DISTINCT CASE WHEN b = ANY($1::text[]) THEN 'SOFTWARE' ELSE b END FROM unnest(brands) b)
        WHERE brands && $1::text[]
        """,
        retired,
    )
    # Connected accounts carry their brand in metadata: product accounts move to SOFTWARE, Shaun's to his brand.
    await conn.execute(
        """
        UPDATE oauth_connections
        SET connection_metadata = jsonb_set(connection_metadata, '{brand_code}', to_jsonb(
            CASE WHEN (connection_metadata->>'is_ceo')::boolean THEN 'CEO_THOUGHT_LEADERSHIP' ELSE 'SOFTWARE' END))
        WHERE (connection_metadata->>'brand_code' = ANY($1::text[]))
           OR ((connection_metadata->>'is_ceo')::boolean AND connection_metadata->>'brand_code' IS DISTINCT FROM 'CEO_THOUGHT_LEADERSHIP')
        """,
        retired,
    )
    # Posts going out through Shaun's accounts are his thought leadership.
    await conn.execute(
        """
        UPDATE content_submissions s SET brand_code = 'CEO_THOUGHT_LEADERSHIP'
        FROM oauth_connections c
        WHERE c.id = s.account_id AND (c.connection_metadata->>'is_ceo')::boolean
          AND s.brand_code IS DISTINCT FROM 'CEO_THOUGHT_LEADERSHIP'
        """
    )
    # Tables created by later setup steps; on a fresh database they don't exist yet (and hold nothing to move).
    if await conn.fetchval("SELECT to_regclass('public.messaging_context') IS NOT NULL"):
        await conn.execute(
            """
            UPDATE messaging_context
            SET brands = ARRAY(SELECT DISTINCT CASE WHEN b = ANY($1::text[]) THEN 'SOFTWARE' ELSE b END FROM unnest(brands) b)
            WHERE brands && $1::text[]
            """,
            retired,
        )
    if await conn.fetchval("SELECT to_regclass('public.approval_paths') IS NOT NULL"):
        await conn.execute("UPDATE approval_paths SET brand_code = 'SOFTWARE' WHERE brand_code = ANY($1::text[])", retired)


# Zenatech's real social accounts, as listed by the team: provider, account name, handle (when it has one), brand code,
# company code, and whether it's Shaun's own. Nothing here is OAuth-connected yet: they exist so posts can be written
# for them (sends to Sprout are simulated), and a real connection replaces the placeholder token later.
_ZT, _ZD, _DAAS, _SW, _CEO = "ZENATECH", "ZENADRONE", "DAAS", "SOFTWARE", "CEO_THOUGHT_LEADERSHIP"
SOCIAL_ACCOUNTS = [
    # Facebook pages
    ("FACEBOOK_PAGE", "Augmum Inc", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "DeskFlex", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "DeskFlex Brazil", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "Drone as a Service", None, _DAAS, "ZDR", False),
    ("FACEBOOK_PAGE", "ESM Software", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "FlexFridge", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "Jadian Inc", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "Provitrac", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "PsPortals Inc", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "Shaun Passley, PhD", None, _CEO, "ZTG", True),
    ("FACEBOOK_PAGE", "TillerStack", None, _SW, "ZTG", False),
    ("FACEBOOK_PAGE", "ZenaDrone", None, _ZD, "ZDR", False),
    ("FACEBOOK_PAGE", "ZenaTech Inc.", None, _ZT, "ZTG", False),
    # Instagram
    ("INSTAGRAM", "zenatech", "zenatech", _ZT, "ZTG", False),
    ("INSTAGRAM", "daasplatform", "daasplatform", _DAAS, "ZDR", False),
    ("INSTAGRAM", "deskflex_software", "deskflex_software", _SW, "ZTG", False),
    ("INSTAGRAM", "deskflexbr", "deskflexbr", _SW, "ZTG", False),
    ("INSTAGRAM", "flex_fridge", "flex_fridge", _SW, "ZTG", False),
    ("INSTAGRAM", "tillerstack", "tillerstack", _SW, "ZTG", False),
    ("INSTAGRAM", "zenadrone_inc", "zenadrone_inc", _ZD, "ZDR", False),
    # LinkedIn company pages
    ("LINKEDIN_PAGE", "DeskFlex Inc.", None, _SW, "ZTG", False),
    ("LINKEDIN_PAGE", "Drone as a Service", None, _DAAS, "ZDR", False),
    ("LINKEDIN_PAGE", "Othership", None, _SW, "ZTG", False),
    ("LINKEDIN_PAGE", "WorkAware", None, _SW, "ZTG", False),
    ("LINKEDIN_PAGE", "ZenaDrone Inc.", None, _ZD, "ZDR", False),
    ("LINKEDIN_PAGE", "ZenaTech Inc.", None, _ZT, "ZTG", False),
    # LinkedIn personal profile
    ("LINKEDIN_PROFILE", "Shaun Passley, PhD", None, _CEO, "ZTG", True),
    # YouTube
    ("YOUTUBE", "Drone as a Service", None, _DAAS, "ZDR", False),
    ("YOUTUBE", "Drone as the Future Podcast", None, _DAAS, "ZDR", False),
    ("YOUTUBE", "ZenaDrone Inc.", None, _ZD, "ZDR", False),
    ("YOUTUBE", "ZenaTech Inc.", None, _ZT, "ZTG", False),
    # TikTok
    ("TIKTOK", "DroneAsAService", "DroneAsAService", _DAAS, "ZDR", False),
]


def _account_key(provider: str, name: str, handle=None) -> str:
    """The stable id a listed account is matched by (so the list can grow without duplicating earlier ones)."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (handle or name).lower()).strip("-")
    return f"listed-{provider.lower()}-{slug}"


# The earlier demo accounts, and where each one's posts move to. An account with no counterpart is just removed.
_DEMO_TO_LISTED = {
    "demo-facebook_page-dronesasaservice": ("FACEBOOK_PAGE", "Drone as a Service", None),
    "demo-facebook_page-ZenaDroneOfficial": ("FACEBOOK_PAGE", "ZenaDrone", None),
    "demo-facebook_page-ZenaTechInc": ("FACEBOOK_PAGE", "ZenaTech Inc.", None),
    "demo-instagram-@zenadrone": ("INSTAGRAM", "zenadrone_inc", "zenadrone_inc"),
    "demo-instagram-@zenatechinc": ("INSTAGRAM", "zenatech", "zenatech"),
    "demo-linkedin_page-drone-as-a-service": ("LINKEDIN_PAGE", "Drone as a Service", None),
    "demo-linkedin_page-zenadrone": ("LINKEDIN_PAGE", "ZenaDrone Inc.", None),
    "demo-linkedin_page-zenatech": ("LINKEDIN_PAGE", "ZenaTech Inc.", None),
    "demo-linkedin_profile-shaun-ceo": ("LINKEDIN_PROFILE", "Shaun Passley, PhD", None),
    "demo-youtube-@ZenaDrone": ("YOUTUBE", "ZenaDrone Inc.", None),
    "demo-youtube-@ZenaTech": ("YOUTUBE", "ZenaTech Inc.", None),
}


async def _retire_demo_social_accounts(conn):
    """Remove the earlier demo accounts. Posts that used one move to the matching listed account first, so none loses
    its account. Safe to run on every start: once they're gone it finds nothing."""
    old = await conn.fetch(
        "SELECT id, provider_account_id FROM oauth_connections WHERE provider_account_id LIKE 'demo-%' "
        "AND (connection_metadata->>'demo_seed')::boolean"
    )
    if not old:
        return
    listed = {r["provider_account_id"]: r["id"] for r in await conn.fetch(
        "SELECT id, provider_account_id FROM oauth_connections WHERE provider_account_id LIKE 'listed-%'"
    )}
    new_for = {}
    for o in old:
        target = _DEMO_TO_LISTED.get(o["provider_account_id"])
        if target and _account_key(*target) in listed:
            new_for[o["id"]] = listed[_account_key(*target)]
    old_ids = [o["id"] for o in old]
    posts = await conn.fetch(
        "SELECT id, account_id, account_ids FROM content_submissions WHERE account_id = ANY($1::uuid[]) OR account_ids && $1::uuid[]",
        old_ids,
    )
    for p in posts:
        ids = [new_for.get(i, i) for i in (p["account_ids"] or ([p["account_id"]] if p["account_id"] else []))]
        ids = [i for i in dict.fromkeys(ids) if i not in old_ids]
        await conn.execute(
            "UPDATE content_submissions SET account_ids = $2::uuid[], account_id = $3 WHERE id = $1",
            p["id"], ids, ids[0] if ids else None,
        )
    await conn.execute("DELETE FROM oauth_connections WHERE id = ANY($1::uuid[])", old_ids)
    logger.info("Replaced %d demo social accounts with the listed ones (%d posts moved).", len(old_ids), len(posts))


async def _seed_demo_social_accounts(conn):
    """Insert the team's listed social accounts that aren't there yet, each matched by its stable id so accounts added
    to SOCIAL_ACCOUNTS later reach databases seeded earlier. Then retire the earlier demo accounts."""
    org_id = await conn.fetchval("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
    if not org_id:
        return

    keys = [_account_key(a[0], a[1], a[2]) for a in SOCIAL_ACCOUNTS]
    result = await conn.execute(
        """
        INSERT INTO oauth_connections (
            organization_id, company_id, provider, provider_account_id, account_name,
            access_token_encrypted, status, expires_at, connection_metadata
        )
        SELECT
            $1,
            (SELECT id FROM companies WHERE organization_id = $1 AND code = a.company_code LIMIT 1),
            a.provider,
            a.key,
            a.account_name,
            'not-connected-placeholder-token',
            'CONNECTED',
            NOW() + INTERVAL '365 days',
            jsonb_strip_nulls(jsonb_build_object('listed', true, 'handle', a.handle, 'brand_code', a.brand_code, 'is_ceo', a.is_ceo))
        FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::bool[], $8::text[])
            AS a(provider, account_name, handle, brand_code, company_code, is_ceo, key)
        WHERE NOT EXISTS (SELECT 1 FROM oauth_connections c WHERE c.provider_account_id = a.key)
        """,
        org_id,
        [a[0] for a in SOCIAL_ACCOUNTS],
        [a[1] for a in SOCIAL_ACCOUNTS],
        [a[2] for a in SOCIAL_ACCOUNTS],
        [a[3] for a in SOCIAL_ACCOUNTS],
        [a[4] for a in SOCIAL_ACCOUNTS],
        [a[5] for a in SOCIAL_ACCOUNTS],
        keys,
    )
    added = int(result.split()[-1])
    if added:
        logger.info("Added %d listed social accounts.", added)
    await _retire_demo_social_accounts(conn)


async def _main():
    from postgresql_db.database import create_pool
    await create_pool()
    try:
        await setup()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
