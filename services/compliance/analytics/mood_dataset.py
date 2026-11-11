"""A fixed training dataset made from reviewed assessments.

What goes in: an assessment that a reviewer has decided, whose features were saved at assessment time, that isn't mock
data, and whose reviewers don't disagree (or whose disagreement a lead reviewer resolved). Its label is the accepted
reviewer decision; its inputs are the features saved with it, never rebuilt from data that has since changed.

What comes out: a manifest in `training_datasets` and one record per example in `training_dataset_examples` naming the
exact assessment and review records used, the split it belongs to, and the group it travels with. A dataset version is
fixed: it is never rebuilt or extended, a new version is made instead, so any model can be traced to the exact data
it learned from.

Splits are by time (oldest train, then validation, then the newest, untouched, test) and by group: assessments of the
same draft, and assessments that rest on the same stories (one incident behind several drafts), stay together, so a
model can't be tested on what it has effectively already seen.
"""

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.ai.core import mood_store
from services.compliance.analytics import mood_reviews
from services.compliance.analytics.mood_features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION

logger = logging.getLogger(__name__)

SPLIT_FRACTIONS = (0.6, 0.2, 0.2)  # train, validation, test (of the examples, by whole groups, oldest first)
SPLITS = ("train", "validation", "test")


class DatasetError(Exception):
    pass


# Every training row has exactly these attributes (one example record):
#   {"assessment_id": "assessment_123", "assessed_at": "2026-11-11T14:00:00Z", "feature_schema_version": "v1",
#    "features": {"relevant_item_count": 24, "negative_share": 0.35, "severe_event_count": 1, "snapshot_age_minutes": 20, ...},
#    "system_recommendation": "WAIT", "reviewer_label": "HOLD", "review_reason": "Relevant safety incident"}
# plus the bookkeeping that makes the dataset fixed and leakage-safe (split, group_id, review_ids, label_source).
REQUIRED_ROW_FIELDS = ("assessment_id", "assessed_at", "feature_schema_version", "features", "system_recommendation",
                       "reviewer_label", "review_reason")


def _decisive_reason(reviews: List[Dict[str, Any]], resolution: "mood_reviews.Resolution") -> Tuple[Optional[str], str]:
    """(reason code, reason in words) of the review the accepted label rests on."""
    from services.compliance.analytics.conversation_feedback import CORRECTION_REASONS

    by_id = {r["_id"]: r for r in reviews}
    decisive = by_id.get(resolution.review_ids[-1]) if resolution.review_ids else None
    if decisive is None or (resolution.source != "RESOLUTION" and decisive.get("reviewer_label") != resolution.label):
        decisive = next((r for r in reviews if r.get("reviewer_label") == resolution.label and not r.get("retracted_at")), decisive)
    if decisive is None:
        return None, ""
    code = decisive.get("reason_code")
    return code, (decisive.get("comment") or CORRECTION_REASONS.get(code or "", "") or "").strip()


def training_row(example: Dict[str, Any]) -> Dict[str, Any]:
    """An example as a plain training row (JSON-safe), in the shape above."""
    at = example["assessed_at"]
    out = {k: example.get(k) for k in REQUIRED_ROW_FIELDS}
    out["assessed_at"] = (at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(at, datetime) else at)
    out.update({k: example.get(k) for k in ("review_reason_code", "label_source", "review_ids", "split", "group_id",
                                            "submission_id", "dataset_version")})
    return out


class _Groups:
    """Union-find: items that share a draft or a story end up in one group."""

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def group_assessments(assessments: List[Dict[str, Any]]) -> Dict[str, str]:
    """assessment_id -> group id. Assessments of one draft, or resting on a story in common, share a group."""
    g = _Groups()
    for a in assessments:
        aid = a["_id"]
        g.union(f"a:{aid}", f"d:{a['submission_id']}")
        for e in a.get("evidence") or []:
            if e.get("counted") and e.get("story_key"):
                g.union(f"a:{aid}", f"s:{e['story_key']}")
    roots: Dict[str, str] = {}
    out = {}
    for a in assessments:
        root = g.find(f"a:{a['_id']}")
        out[a["_id"]] = roots.setdefault(root, f"group_{len(roots) + 1:04d}")
    return out


def assign_splits(examples: List[Dict[str, Any]], fractions: Tuple[float, float, float] = SPLIT_FRACTIONS) -> None:
    """Set each example's split, whole groups at a time, oldest groups first: train, then validation, then test. The
    newest group is always test and each split gets at least one group."""
    by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ex in examples:
        by_group[ex["group_id"]].append(ex)
    if len(by_group) < 3:
        raise DatasetError(f"Only {len(by_group)} independent group(s) of drafts: at least 3 are needed to keep a "
                           "train, a validation and a test period apart. Keep collecting reviews.")
    ordered = sorted(by_group.values(), key=lambda grp: min(e["assessed_at"] for e in grp))
    total = len(examples)
    train_until, validation_until = fractions[0] * total, (fractions[0] + fractions[1]) * total
    seen, given = 0, set()
    for i, grp in enumerate(ordered):
        after = len(ordered) - i - 1  # groups still to come
        if after == 0:
            split = "test"
        elif after == 1 and "validation" not in given:
            split = "validation"
        elif seen < train_until and after >= 2:
            split = "train"
        elif seen < validation_until:
            split = "validation"
        else:
            split = "test"
        given.add(split)
        for ex in grp:
            ex["split"] = split
        seen += len(grp)


async def select_examples(version: str, feature_schema_version: str = FEATURE_SCHEMA_VERSION,
                          fractions: Tuple[float, float, float] = SPLIT_FRACTIONS) -> Tuple[List[Dict[str, Any]], Counter]:
    """The examples a dataset made now would hold, already split, and what was left out and why. Writes nothing, so it
    can answer "is there enough yet?" without creating a dataset. Raises DatasetError if there are no reviewed
    assessments or too few independent groups to separate the splits."""
    assessments = await mood_store.find(mood_store.ASSESSMENTS, {}, sort=[("assessed_at", 1)])
    reviews: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if assessments:
        ids = [a["_id"] for a in assessments]
        for r in await mood_store.find(mood_store.REVIEWS, {"assessment_id": {"$in": ids}}):
            r["reviewed_at"] = mood_store.aware(r.get("reviewed_at"))
            reviews[r["assessment_id"]].append(r)

    excluded: Counter = Counter()
    chosen: List[Dict[str, Any]] = []
    for a in assessments:
        a["assessed_at"] = mood_store.aware(a.get("assessed_at"))
        if a.get("feature_schema_version") != feature_schema_version or not a.get("features"):
            excluded["other_feature_schema_or_no_features"] += 1
        elif a.get("is_mock"):
            excluded["mock_data"] += 1
        else:
            res = mood_reviews.resolve_decision(reviews.get(a["_id"], []))
            if res.status == "UNREVIEWED":
                excluded["not_reviewed"] += 1
            elif res.status == "CONFLICT":
                excluded["reviewers_disagree_unresolved"] += 1
            else:
                chosen.append({"assessment": a, "resolution": res})

    groups = group_assessments([c["assessment"] for c in chosen])
    examples = []
    for c in chosen:
        a, res = c["assessment"], c["resolution"]
        code, reason = _decisive_reason(reviews.get(a["_id"], []), res)
        examples.append({
            "_id": f"{version}:{a['_id']}", "dataset_version": version,
            # the row, as one example record: what was assessed, what it was measured as, what the system said,
            # what the reviewer decided, and why
            "assessment_id": a["_id"], "assessed_at": a["assessed_at"],
            "feature_schema_version": a["feature_schema_version"], "features": a["features"],
            "system_recommendation": a.get("system_recommendation"),
            "reviewer_label": res.label, "review_reason": reason, "review_reason_code": code,
            # and what makes the dataset fixed and traceable
            "submission_id": a["submission_id"], "review_ids": res.review_ids, "label_source": res.source,
            "group_id": groups[a["_id"]], "split": None,
        })
    if not examples:
        raise DatasetError("No reviewed assessments to export yet (" + ", ".join(f"{k}: {v}" for k, v in excluded.items()) + ").")
    assign_splits(examples, fractions)
    return examples, excluded


async def build_dataset(version: str, feature_schema_version: str = FEATURE_SCHEMA_VERSION,
                        fractions: Tuple[float, float, float] = SPLIT_FRACTIONS) -> Dict[str, Any]:
    """Select, label and split the reviewed assessments and save them as dataset `version`. Raises DatasetError if the
    version exists or there aren't enough independent examples to separate the splits."""
    if not mood_store.is_enabled():
        raise DatasetError("The vector database isn't configured (MONGODB_URI), so there is nothing to export from.")
    if await mood_store.get(mood_store.DATASETS, version):
        raise DatasetError(f"Dataset {version} already exists. A dataset is fixed: make a new version instead.")

    examples, excluded = await select_examples(version, feature_schema_version, fractions)

    by_split = Counter(e["split"] for e in examples)
    manifest = {
        "_id": version, "dataset_version": version, "feature_schema_version": feature_schema_version,
        "features": list(FEATURE_NAMES), "status": "BUILDING", "created_at": mood_store.now(),
        "row_attributes": list(REQUIRED_ROW_FIELDS),
        "selection": {"reviewed_only": True, "mock_excluded": True, "unresolved_reviews_excluded": True,
                      "label": "accepted reviewer decision", "inputs": "features saved at assessment time",
                      "split": "by time, whole groups of related drafts and stories kept together",
                      "fractions": list(fractions)},
        "example_count": len(examples), "examples_collection": mood_store.EXAMPLES,
        "by_split": dict(by_split),
        "by_label": dict(Counter(e["reviewer_label"] for e in examples)),
        "by_split_and_label": {s: dict(Counter(e["reviewer_label"] for e in examples if e["split"] == s)) for s in SPLITS},
        "excluded": dict(excluded),
        "period": {s: [min(e["assessed_at"] for e in examples if e["split"] == s).isoformat(),
                       max(e["assessed_at"] for e in examples if e["split"] == s).isoformat()]
                   for s in SPLITS if by_split.get(s)},
    }
    if not await mood_store.insert_once(mood_store.DATASETS, manifest):
        raise DatasetError(f"Dataset {version} was created by someone else a moment ago.")
    for ex in examples:
        await mood_store.insert_once(mood_store.EXAMPLES, ex)
    await mood_store.update(mood_store.DATASETS, version, {"status": "READY"})
    manifest["status"] = "READY"
    return manifest


async def load_dataset(version: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A dataset's manifest and its examples. Each example is a complete training row (see REQUIRED_ROW_FIELDS): the
    features and the rules' recommendation are the ones saved at assessment time, copied into the row when the dataset
    was made, so the dataset doesn't change if anything else does."""
    manifest = await mood_store.get(mood_store.DATASETS, version)
    if not manifest or manifest.get("status") != "READY":
        raise DatasetError(f"Dataset {version} doesn't exist or isn't finished.")
    examples = await mood_store.find(mood_store.EXAMPLES, {"dataset_version": version}, sort=[("assessed_at", 1)])
    for e in examples:
        e["assessed_at"] = mood_store.aware(e.get("assessed_at"))
    return manifest, examples
