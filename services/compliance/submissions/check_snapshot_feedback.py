"""Bind human labels to immutable findings, rejecting feedback after a racing recheck."""

import json
from uuid import UUID

from fastapi import HTTPException


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def select_finding(check, rule_id, expected_id=None):
    matches = [v for v in check.get("violations") or [] if str(v.get("rule_id")) == str(rule_id)]
    if expected_id is not None:
        matches = [v for v in matches if v.get("finding_snapshot_id") == str(expected_id)]
        if not matches:
            raise HTTPException(status_code=409, detail="This check changed. Refresh its findings before reviewing.")
    if not matches:
        raise HTTPException(status_code=404, detail="That rule is not flagged on this check")
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Choose a finding_snapshot_id: this rule has multiple findings.")
    return matches[0]


async def validate_link(conn, submission_id, version, rule_id, finding):
    finding_id = finding.get("finding_snapshot_id")
    if not finding_id:
        # Preserve legacy review workflows without inventing a check-time snapshot or training label.
        return None
    live = await conn.fetchrow(
        "SELECT current_version, latest_check FROM content_submissions WHERE id=$1 FOR UPDATE", submission_id,
    )
    check = decoded(live["latest_check"]) if live else {}
    if not live or live["current_version"] != version or (
        check or {}
    ).get("check_snapshot_id") != finding.get("check_snapshot_id"):
        raise HTTPException(status_code=409, detail="This check changed. Refresh its findings before reviewing.")
    saved = await conn.fetchrow(
        """SELECT f.finding, f.rule_id, c.submission_id, c.id AS check_id,
                  EXISTS (SELECT 1 FROM compliance_check_post_links p
                          WHERE p.check_snapshot_id=c.id AND p.submission_id=$2) AS linked
           FROM compliance_finding_snapshots f
           JOIN compliance_check_snapshots c ON c.id=f.check_snapshot_id WHERE f.id=$1""",
        UUID(str(finding_id)), submission_id,
    )
    if not saved or (saved["submission_id"] != submission_id and not saved.get("linked")) or str(saved["rule_id"]) != str(rule_id) or (
        str(saved["check_id"]) != finding.get("check_snapshot_id") or decoded(saved["finding"]) != finding
    ):
        raise HTTPException(status_code=409, detail="This finding does not match its saved check. Run the check again.")
    return UUID(str(finding_id))


async def record(conn, finding_id, version, verdict, reason, note, user_id, *, resolution=None):
    if finding_id is None:
        return
    resolution = resolution or (
        "EVIDENCE_SUPPLIED" if reason == "EVIDENCE_ON_FILE"
        else "FALSE_POSITIVE" if verdict == "OVER_FLAGGED" else "CONFIRMED"
    )
    await conn.execute(
        """INSERT INTO compliance_feedback_events
           (finding_snapshot_id, submission_version, verdict, resolution, reason, note, decided_by)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        finding_id, version, verdict, resolution, reason, note, user_id,
    )


async def save_verdict(conn, submission_id, version, rule_id, finding, verdict, reason, note, user_id):
    finding_id = await validate_link(conn, submission_id, version, rule_id, finding)
    evaluation = finding.get("evaluation") or {}
    verdict_id = await conn.fetchval(
        """INSERT INTO finding_verdicts
           (submission_id, version_number, rule_id, rule_number, verdict, reason, note, passage, context,
            flagged_terms, finding_status, ai_decision, decided_by, finding_snapshot_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
           ON CONFLICT (submission_id, version_number, rule_id) DO UPDATE SET
               verdict=EXCLUDED.verdict, reason=EXCLUDED.reason, note=EXCLUDED.note,
               passage=EXCLUDED.passage, context=EXCLUDED.context, flagged_terms=EXCLUDED.flagged_terms,
               finding_status=EXCLUDED.finding_status, ai_decision=EXCLUDED.ai_decision,
               decided_by=EXCLUDED.decided_by, decided_at=NOW(), finding_snapshot_id=EXCLUDED.finding_snapshot_id
           RETURNING id""",
        submission_id, version, rule_id, finding.get("rule_number"), verdict, reason, note,
        finding.get("passage"), finding.get("context"), list(finding.get("flagged_terms") or []),
        finding.get("status") or "VIOLATION", evaluation.get("decision"), user_id, finding_id,
    )
    await record(conn, finding_id, version, verdict, reason, note, user_id)
    return verdict_id


async def save_request(conn, row, rule_id, finding, req, note, user_id):
    async with conn.transaction():
        finding_id = await validate_link(conn, row["id"], row["current_version"], rule_id, finding)
        await conn.execute(
            """INSERT INTO creator_flag_dismissal_requests
               (submission_id, version_number, rule_id, rule_number, flagged_terms, traffic_light,
                reason, note, requested_by, finding_snapshot_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (submission_id, version_number, rule_id) DO UPDATE SET
                   reason=EXCLUDED.reason, note=EXCLUDED.note, requested_by=EXCLUDED.requested_by,
                   requested_at=NOW(), status='PENDING', finding_snapshot_id=EXCLUDED.finding_snapshot_id,
                   flagged_terms=EXCLUDED.flagged_terms, traffic_light=EXCLUDED.traffic_light""",
            row["id"], row["current_version"], rule_id, finding.get("rule_number"),
            list(finding.get("flagged_terms") or []), finding.get("traffic_light") or "YELLOW",
            req.reason, note, user_id, finding_id,
        )


async def remove_verdict(conn, submission_id, version, rule_id, verdict, user_id):
    gone = await conn.fetchrow(
        """DELETE FROM finding_verdicts
           WHERE submission_id=$1 AND version_number=$2 AND rule_id=$3 AND verdict=$4
           RETURNING id, rule_number, finding_snapshot_id""",
        submission_id, version, rule_id, verdict,
    )
    if gone:
        await record(conn, gone["finding_snapshot_id"], version, "RETRACTED", None, None, user_id,
                     resolution="UNDO_CONFIRM" if verdict == "CORRECT" else "UNDO_DISMISS")
    return gone
