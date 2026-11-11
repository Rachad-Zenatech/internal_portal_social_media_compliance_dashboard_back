# Public mood: conversation assessment (API contract for the frontend)

`GET /api/submissions/{id}/public-mood` returns the topic mood as before, plus `conversation_mood`: what the news and
X posts found for **this** post say. It is advisory only. Nothing here affects the compliance score or approval.

`conversation_mood` is `null` only when the post has no text, or the listening source is the mock one.

## Status: what the rest of the object means

| `status` | Meaning | `recommendation` | Show |
|---|---|---|---|
| `READY` | Current assessment. | `POST` / `WAIT` / `HOLD` | Everything below. |
| `NO_RELEVANT_MATCHES` | Conversations were found, none could be verified as about this post. | `WAIT` (never `POST`) | "Nothing we found is about this post, so there is nothing to base a decision on." |
| `INDEXING_PENDING` | Collected items are not searchable yet. | `null` | "Still indexing (`pending_index_count` items). Try again shortly." |
| `UNAVAILABLE` | Search or the vector store cannot be used now. | `null` | The reason from `recommendation_reason`. |
| `STALE` | An earlier assessment, shown because a new one could not be made. | the earlier value | The earlier result **plainly labelled as earlier**, with `warnings`. |

Always show `recommendation_reason`. Never show a recommendation chip when `recommendation` is `null`.

## Reason codes and warnings

`reason_codes` are stable machine-readable strings; `warnings` are sentences ready to display.

| Code | Meaning |
|---|---|
| `REVIEW_REQUIRED` | Recommendation is not `POST`. |
| `HOLD_NEGATIVE` | The matched conversations are strongly negative. |
| `SMALL_SAMPLE` | Fewer than 10 distinct stories. |
| `RETRIEVAL_CAPPED` | The search limit was reached: a sample, not every relevant conversation online. |
| `NO_RELEVANT_EVIDENCE` | Nothing retrieved was verified as relevant. |
| `COLLECTION_PARTIAL`, `COLLECTION_FAILED`, `COLLECTION_EMPTY`, `COLLECTION_AUTH_REQUIRED`, `COLLECTION_RATE_LIMITED` | How the **last collection** went (as of now, not as of the assessment). Some sources may be missing. |
| `INDEXING_INCOMPLETE` | Some collected items are not searchable yet. |
| `EVIDENCE_AGING` | The newest evidence was collected more than 48 hours ago. |
| `STALE_ASSESSMENT` | Status is `STALE`. |
| `STORE_NOT_CONFIGURED`, `INDEX_EMPTY`, `SEARCH_UNAVAILABLE`, `ASSESSMENT_ERROR` | Why the status is `UNAVAILABLE`. |

## What to display

- **Recommendation and reason**: `recommendation`, `recommendation_reason`.
- **Evidence timestamp**: `collected_at` (when the evidence was gathered) and `assessed_at` / `expires_at`.
- **Sentiment among matched conversations**: `positive_pct`, `neutral_pct`, `negative_pct` over `count` distinct
  stories. Label it "among conversations matched to this post", not "public opinion".
- **Sample size**: `retrieved_count` → `verified_relevant_count` → `distinct_story_count` → `displayed_count`.
  The mood uses every verified story; only `displayed_count` examples are returned in `matched_items`
  (alias of `matches`).
- **Source links**: each item has `url`, `author`, `published_at`, `topic`, `sentiment`.
- **Why it matched**: `relevance_status` (`RELEVANT`), `relevance_reason`, `supporting_text`. `similarity_score` is
  the search score, not a probability.
- **Duplicates**: `also_reported_by` is how many other items tell the same story (counted once).
- **Warnings**: `warnings` and `collection_status`, `indexing_status`.

## Reviewer actions

Reasons come from `GET /api/listening/feedback-reasons` → `{"unrelated": {code: label}, "correction": {code: label}}`.
Every action returns the updated `PublicMood`. Feedback is stored against the exact `assessment_id` the reviewer saw.

| Action | Request | Effect |
|---|---|---|
| This item is not about the post | `PUT /submissions/{id}/public-mood/conversations/{assessment_id}/items/{item_id}/unrelated` `{"reason_code": "DIFFERENT_COMPANY", "note": "…"}` | Item is left out of this post's assessments; a new assessment is made; the item appears in `excluded_by_reviewers`. |
| Put it back | `DELETE /submissions/{id}/public-mood/conversations/items/{item_id}/unrelated` | Item counts again. |
| This recommendation is wrong | `PUT /submissions/{id}/public-mood/conversations/{assessment_id}/correction` `{"recommendation": "POST", "reason_code": "TOO_CAUTIOUS", "note": "…"}` | Recorded in `reviewer_corrections` (one per person, changeable). The computed assessment is unchanged. |
| Take the correction back | `DELETE /submissions/{id}/public-mood/conversations/{assessment_id}/correction` | Removes your correction. |
| See exactly what an assessment looked at | `GET /submissions/{id}/public-mood/conversations/{assessment_id}/evidence` | Every item (counted or not, with its verdict) as it read when the assessment ran, even if the feed has changed since. |

An unknown `reason_code` returns `422`; an assessment of another post, or an item that was not in the assessment,
returns `404`.

## Example

```json
{
  "status": "READY",
  "recommendation": "WAIT",
  "recommendation_reason": "Mixed sentiment (29% positive, 71% neutral, 0% negative). Review timing and angle before posting.",
  "reason_codes": ["REVIEW_REQUIRED", "RETRIEVAL_CAPPED", "COLLECTION_PARTIAL"],
  "warnings": ["The last collection was partial: some sources could not be read, so the evidence may be incomplete."],
  "count": 21, "positive_pct": 28.6, "neutral_pct": 71.4, "negative_pct": 0.0,
  "retrieved_count": 50, "verified_relevant_count": 21, "distinct_story_count": 21, "displayed_count": 12,
  "retrieval_capped": true,
  "assessment_id": "…", "assessed_at": "…", "expires_at": "…", "evidence_version": "…",
  "collection_status": "PARTIAL", "collected_at": "…", "indexing_status": "COMPLETE", "pending_index_count": 0,
  "matched_items": [{
    "item_id": "…", "text": "ZenaTech Anticipates Faster Survey Delivery …", "url": "https://…", "author": "@…",
    "published_at": "…", "sentiment": "POSITIVE", "similarity_score": 0.861,
    "relevance_status": "RELEVANT", "relevance_reason": "Both mention 'ZenaTech' (…).",
    "supporting_text": "ZenaTech Anticipates Faster Survey Delivery …", "also_reported_by": 0
  }],
  "excluded_by_reviewers": [], "reviewer_corrections": []
}
```

## Added for learning from reviews

More reviewer actions (each returns the updated `PublicMood`; each is also kept for training the decision model):

| Action | Request | Meaning |
|---|---|---|
| The recommendation is right | `PUT /submissions/{id}/public-mood/conversations/{assessment_id}/confirmation` | Recorded as the reviewer's decision (`reason_code` `AGREE`). Agreement is a label too: the model must see the cases where the system was right. |
| This item's sentiment is wrong | `PUT .../conversations/{assessment_id}/items/{item_id}/sentiment` `{"correct_sentiment": "NEGATIVE", "note": "…"}` · `DELETE` to withdraw | Improves the sentiment analysis. It does not change the assessment. |
| Settle a disagreement (lead reviewer) | `PUT .../conversations/{assessment_id}/resolution` `{"recommendation": "HOLD", "reason_code": "…"}` | Needs the same permission as tuning the listening topics. Until reviewers who disagree are resolved, the assessment is left out of training. |

New response fields: `decision_source` (`RULES`, `MODEL` or `RULES_FALLBACK`) and `decision_note` (why a fallback or guard rail applied). A shadow model's own answer is saved with the assessment but never shown, so it can't anchor the reviewer.

`GET /listening/model/status` (administrators): whether xgboost is installed, which model is registered and loaded, what it was trained on, which measurements matter most, how it did in training, and the limits that keep it in check.

Training starts by itself after a reviewer's decision changes (given, changed, confirmed, resolved or withdrawn), and only then: a scrape does not start it, and neither do match or sentiment corrections. It runs in its own process, at most one at a time and no more often than every 10 minutes (`MOOD_AUTO_TRAIN_MIN_MINUTES`), and only trains when there is enough new reviewed data. It never registers or promotes a model. `MOOD_AUTO_TRAIN=off` turns it off; `GET /listening/model/status` shows the last attempt.
