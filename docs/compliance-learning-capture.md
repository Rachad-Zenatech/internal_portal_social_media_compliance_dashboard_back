# Compliance learning capture

This feature records training evidence. It does not train XGBoost, predict scores, or change scoring and approval gates.
The original automated score remains separate from the existing score calculated after reviewer dismissals.

## Deployment

The implementation is isolated on `feature/compliance-learning-capture`. The demo checkout and running container stay on `main`.
Deploy this branch after the demo to start capture. The existing submission-schema startup routine creates the new tables
and nullable links. No migration or branch deployment has been run against the demo database.

Old checks and verdicts are not reconstructed using today's post content or rule definitions. Run a new check to obtain
snapshot IDs. Existing review workflows remain usable for old checks, but they cannot create invented training examples.

## Stored evidence

| Table | Purpose |
| --- | --- |
| `compliance_check_snapshots` | A unique ID per computed run; frozen post details, original/effective request, evaluated rule definitions, complete result, original score, input hash, schema version, and capture time. |
| `compliance_finding_snapshots` | A unique ID per finding occurrence, linked to its check, with its complete finding JSON, original penalty, contextual confidence if available, and rule definition. |
| `compliance_check_post_links` | Links a script check computed before a post existed to the post that later adopts it, without rewriting the original snapshot. |
| `compliance_feedback_events` | Append-only reviewer decisions and retractions linked to exact finding IDs, with actor, reason, note, resolution, selection provenance, and deterministic event order. |

Database triggers reject ordinary updates and deletes of these four history tables. They intentionally do not cascade
from live posts, rules or users: deleting a never-submitted draft must not erase captured evidence. Retention and access
controls for these copies must be considered separately from deletion of live records.

Post details include the title, captions, brand, audience, platforms, scope, source links, and edit timestamps available
on the checked row. Evaluated inputs also retain the extracted image text, transcript segments, document text, supplied
source evidence, and media identifiers available to the check. Original image/video binaries are not duplicated; preserve
attachment storage separately if future work requires replaying visual models. Built-in/derived findings retain their
finding definition and check rulebook version rather than pretending they came from a mutable database rule.

Every full-post check and script check saves history before returning snapshot IDs. Storage failure fails the check;
it does not publish IDs that were never persisted. A superseded or incomplete computation may exist in history without
ever becoming the live saved check. Such a row is not automatically a training example.

## Frontend review payload

The saved check contains `check_snapshot_id`. Each entry in its `violations` contains its own `finding_snapshot_id`
and `check_snapshot_id`. The frontend should send the displayed finding ID when requesting a dismissal or confirming
a flag. The existing request bodies accept this additional field:

```json
{
  "finding_snapshot_id": "<ID from the displayed finding>",
  "note": "Reviewer explanation"
}
```

A dismissal additionally supplies its existing `reason`. Apply this to the existing flag-confirmation, flag-dismissal,
and creator-dismissal-request endpoints. A stale supplied ID returns HTTP 409. Multiple findings for the same rule also
require this ID rather than silently selecting the first one.

Omitting the ID stays compatible with older clients for unambiguous findings. The server still records the exact current
check and rejects a recheck racing the database write, but cannot know whether an older screen was displayed before
the request arrived. These feedback events carry `selection_verified=false`. Explicit finding selection, including
approval of a persisted creator request bound to that finding, records `selection_verified=true`. This is identity
provenance, not a claim that the reviewer's judgment is infallible. Update the frontend payload before relying on
direct confirmations/dismissals as verified training labels.

## Human label meaning

- Pending creator requests create no training label. Approval applies to the saved request's exact finding; a changed
  check requires reviewing/requesting the dismissal again.
- Explicit reviewer confirmation records `CORRECT`; a false-positive dismissal records `OVER_FLAGGED`.
- `EVIDENCE_ON_FILE` records `CORRECT` with resolution `EVIDENCE_SUPPLIED`, not a false-positive label.
- Undo appends `RETRACTED`. Replacing a verdict keeps its earlier events. Whole-post approval and rejection of a creator
  request do not imply a verdict on every finding; use explicit flag confirmation for an accurate-flag label.

For a future dataset, first select the latest event per finding by `event_sequence`, then exclude retracted/unverified
labels and incomplete checks. Do not filter out retractions before determining the latest event. Join the finding to
its immutable check and post details; use adoption links for pre-post scripts. Group reused checks, post revisions and
duplicate inputs together when splitting training/test data. A feedback count is not a count of independent posts.

## Validation

`tests/test_check_snapshot_capture.py`, `tests/test_check_snapshot_feedback.py`, and
`tests/test_check_snapshot_database.py` cover frozen inputs, rules and scores, async isolation, exact linkage, legacy
behavior, feedback provenance, evidence resolutions, undo history, stale checks, atomic rollback and script adoption.
Database tests opt in with `COMPLIANCE_SNAPSHOT_TEST_DSN`; use a dedicated disposable database, never the demo DSN.
