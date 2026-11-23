"""Freeze check-time inputs and results; live scores remain owned by their existing services."""

import hashlib
import json
from uuid import UUID, uuid4

from postgresql_db.database import get_pool


POST_FIELDS = (
    "id", "reference_code", "created_by", "current_version", "status", "title", "content_type",
    "caption_text", "caption_html", "platform_captions", "brand_code", "brand_codes", "audience",
    "audiences", "region", "platforms", "rule_sets", "account_id", "account_ids", "publish_at",
    "approval_needed_by", "content_edited_at", "media_edited_at", "script_required", "script_status",
    "ai_generated_images", "ai_generated_images_confirmed", "source_links",
)


def frozen(value):
    """Take a detached JSON copy before an awaited model call can outlive edits."""
    return json.loads(json.dumps(value, default=str))


def inputs(row, request, *, material=None, script=None):
    post = frozen({key: row.get(key) for key in POST_FIELDS if key in row})
    evaluated = {"request": request.model_dump(mode="json")}
    if material is not None:
        evaluated["media"] = frozen({
            "video": dict(material.video) if material.video else None,
            "images": [dict(item) for item in material.images],
            "documents": [dict(item) for item in material.documents],
            "script": material.script, "segments": material.segments,
            "visual_segments": material.visual_segments, "video_scored": material.video_scored,
        })
    if script is not None:
        evaluated["script_body"] = script
    return post, frozen(evaluated)


def prepare(post, evaluated, captured, check):
    """Give every run and finding its own identity, including two findings for one rule."""
    check = frozen(check)
    snapshot_id = uuid4()
    check["check_snapshot_id"] = str(snapshot_id)
    evaluated = frozen(evaluated)
    evaluated["effective_request"] = captured.get("request")
    rules = frozen(captured.get("rules", {}))
    evaluated["rules"] = rules
    findings = []
    for ordinal, finding in enumerate(check.get("violations") or []):
        finding_id = uuid4()
        finding["finding_snapshot_id"] = str(finding_id)
        finding["check_snapshot_id"] = str(snapshot_id)
        definition = rules.get(str(finding["rule_id"]))
        rule = {"source": "RULEBOOK", "definition": definition} if definition else {
            "source": "BUILTIN_OR_DERIVED", "definition": frozen(finding),
        }
        rule["rulebook_version"] = check.get("rulebook_version")
        findings.append((finding_id, ordinal, finding["rule_id"], rule, finding))
    digest = hashlib.sha256(json.dumps(
        {"post": post, "input": evaluated}, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return snapshot_id, digest, check, findings, evaluated


async def save(post, evaluated, captured, check):
    """Save before returning IDs to callers. Failure stops publication of an unsaved check.

    Computed checks can be superseded before adoption; they remain unlabeled history.
    No historical checks are reconstructed from today's mutable content or rulebook.
    """
    snapshot_id, digest, check, findings, evaluated = prepare(post, evaluated, captured, check)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO compliance_check_snapshots
                   (id, submission_id, submission_version, input_sha256, post_details, evaluated_input, check_result)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb)""",
                snapshot_id, UUID(post["id"]) if post.get("id") else None, post.get("current_version", 0), digest,
                json.dumps(post), json.dumps(evaluated), json.dumps(check),
            )
            if findings:
                await conn.execute(
                    """INSERT INTO compliance_finding_snapshots
                       (id, check_snapshot_id, ordinal, rule_id, rule_snapshot, finding)
                       SELECT f.id, $1, f.ordinal, f.rule_id, f.rule_snapshot::jsonb, f.finding::jsonb
                       FROM UNNEST($2::uuid[], $3::int[], $4::uuid[], $5::text[], $6::text[])
                         AS f(id, ordinal, rule_id, rule_snapshot, finding)""",
                    snapshot_id, [f[0] for f in findings], [f[1] for f in findings],
                    [UUID(str(f[2])) for f in findings], [json.dumps(f[3]) for f in findings],
                    [json.dumps(f[4]) for f in findings],
                )
    return check


async def link_to_post(run, submission_id, check):
    """Bind a pre-post script check when its result is adopted by a real post."""
    if not check.get("check_snapshot_id"):
        return
    await run(
        """INSERT INTO compliance_check_post_links (check_snapshot_id, submission_id, submission_version)
           SELECT c.id, s.id, s.current_version FROM compliance_check_snapshots c
           JOIN content_submissions s ON s.id=$1 WHERE c.id=$2
           ON CONFLICT DO NOTHING""",
        submission_id, UUID(str(check["check_snapshot_id"])),
    )
