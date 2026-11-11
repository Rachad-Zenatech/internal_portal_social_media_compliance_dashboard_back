"""Training and judging the decision model. This runs as an offline job (scripts/mood_ml.py), never in a request.

    train      learns from a fixed dataset's older reviewed assessments, picks its stopping point on a later period, and is
               scored on the newest period it never saw, side by side with the rules that were operating at the time AND
               with the model currently in use: a candidate that does worse than the active model is not eligible
               (guarded training). Each model is written as three files: model, classes, metadata.
    readiness  refuses to call a model ready when any class has too few examples: that means keep collecting reviews
    shadow     registers a run so assessments record what it would say beside what the rules say (nothing changes)
    promote    lets a model decide (inside the guard rails in mood_model), only if its test results meet criteria and a
               named person approves; the criteria below are PLACEHOLDERS to be agreed with the reviewers

The inputs are the features saved with each assessment. Reviewer decisions and post-publication outcomes are the label
and the outcome and are never inputs (mood_features.FORBIDDEN_FEATURES).
"""

import asyncio
import json
import logging
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.ai.core import mood_store
from services.compliance.analytics import mood_dataset, mood_model
from services.compliance.analytics.mood_features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FORBIDDEN_FEATURES, to_row

logger = logging.getLogger(__name__)

CLASSES = ["POST", "WAIT", "HOLD"]
# Examples of each class needed before a model can be called ready (train / validation / test).
MIN_PER_CLASS = {"train": int(os.getenv("MOOD_MIN_TRAIN_PER_CLASS", "20")),
                 "validation": int(os.getenv("MOOD_MIN_VALIDATION_PER_CLASS", "5")),
                 "test": int(os.getenv("MOOD_MIN_TEST_PER_CLASS", "5"))}

# PLACEHOLDER promotion criteria. They are a starting point for the conversation with the reviewers, not an agreement.
DEFAULT_CRITERIA: Dict[str, float] = {
    "min_test_examples": 30,
    "max_missed_hold_rate": 0.0,        # reviewer HOLD that the model did not HOLD (of reviewer HOLDs)
    "max_unsafe_approval_rate": 0.05,   # reviewer HOLD or WAIT that the model approved (of those)
    "max_unnecessary_hold_rate": 0.25,  # model HOLD where the reviewer did not (of the model's HOLDs)
    "min_post_precision": 0.90,         # of the model's POSTs, how many the reviewers also approved
    "max_wait_rate": 0.60,              # share of drafts the model defers
    "max_calibration_error": 0.15,      # expected calibration error: its confidence must mean what it says
}


# A candidate may be this much less accurate than the active model on the same held-out examples (their 2 points).
GUARD_ACCURACY_TOLERANCE = 0.02
_training_lock = asyncio.Lock()  # one training at a time


class NotReady(Exception):
    """Too little data (or the wrong state) to proceed; the message says what's missing."""


def validate_rows(rows: List[Dict[str, Any]]) -> None:
    """Every training row must carry the required attributes, and nothing that would leak the answer. Raises
    NotReady naming the first problem, so a malformed export is refused instead of silently trained on."""
    if not rows:
        raise NotReady("There are no training rows.")
    for i, row in enumerate(rows):
        missing = [f for f in mood_dataset.REQUIRED_ROW_FIELDS if f not in row or row[f] in (None, "") and f not in ("review_reason", "features")]
        if missing:
            raise NotReady(f"Training row {i} ({row.get('assessment_id')}) is missing: {', '.join(missing)}.")
        if not isinstance(row["features"], dict):
            raise NotReady(f"Training row {i}: features must be a mapping of measurement to value.")
        if row["feature_schema_version"] != FEATURE_SCHEMA_VERSION:
            raise NotReady(f"Training row {i} is feature schema {row['feature_schema_version']}; the code builds {FEATURE_SCHEMA_VERSION}.")
        leaked = FORBIDDEN_FEATURES & set(row["features"])
        if leaked:
            raise NotReady(f"Training row {i} has the label or an outcome among its features: {sorted(leaked)}.")
        unknown = set(row["features"]) - set(FEATURE_NAMES)
        if unknown:
            raise NotReady(f"Training row {i} has features the schema doesn't define: {sorted(unknown)}.")
        if row["reviewer_label"] not in CLASSES:
            raise NotReady(f"Training row {i}: reviewer_label '{row['reviewer_label']}' is not one of {CLASSES}.")
    if len({r["reviewer_label"] for r in rows}) < 2:
        raise NotReady("The training rows have only one reviewer label: at least two classes are needed.")


def rows_from_jsonl(path: str) -> List[Dict[str, Any]]:
    """Training rows from a file with one JSON record per line, in the dataset's row shape."""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _rate(n: int, d: int) -> Optional[float]:
    return None if d == 0 else round(n / d, 4)


def metrics(y_true: Sequence[str], y_pred: Sequence[str]) -> Dict[str, Any]:
    """How a set of recommendations compares with what the reviewers decided. None where a rate has no cases."""
    n = len(y_true)
    pairs = list(zip(y_true, y_pred))
    true_hold = [p for t, p in pairs if t == "HOLD"]
    stoppable = [p for t, p in pairs if t in ("HOLD", "WAIT")]
    pred_hold = [t for t, p in pairs if p == "HOLD"]
    pred_post = [t for t, p in pairs if p == "POST"]
    return {
        "n": n,
        "accuracy": _rate(sum(1 for t, p in pairs if t == p), n),
        # Did it approve (or lower) something reviewers would stop?
        "missed_hold_rate": _rate(sum(1 for p in true_hold if p != "HOLD"), len(true_hold)),
        "missed_hold_count": sum(1 for p in true_hold if p != "HOLD"),
        "unsafe_approval_rate": _rate(sum(1 for p in stoppable if p == "POST"), len(stoppable)),
        "unsafe_approval_count": sum(1 for p in stoppable if p == "POST"),
        # Did it block acceptable drafts?
        "unnecessary_hold_rate": _rate(sum(1 for t in pred_hold if t != "HOLD"), len(pred_hold)),
        "unnecessary_hold_count": sum(1 for t in pred_hold if t != "HOLD"),
        # When it said POST, how often was approval right?
        "post_precision": _rate(sum(1 for t in pred_post if t == "POST"), len(pred_post)),
        # Does it defer too often?
        "wait_rate": _rate(sum(1 for p in y_pred if p == "WAIT"), n),
        "confusion": {t: dict(Counter(p for tt, p in pairs if tt == t)) for t in CLASSES},
    }


def calibration(probs: Sequence[Sequence[float]], y_true: Sequence[str], classes: Sequence[str] = CLASSES,
                bins: int = 5) -> Dict[str, Any]:
    """Whether the model's confidence means what it says: Brier score, and expected calibration error over equal-width
    bins of its top probability (confident answers should be right about that often)."""
    n = len(y_true)
    if n == 0:
        return {"n": 0, "brier": None, "expected_calibration_error": None, "bins": []}
    brier = sum(sum((p - (1.0 if c == t else 0.0)) ** 2 for p, c in zip(row, classes)) for row, t in zip(probs, y_true)) / n
    table, ece = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(max(row), classes[max(range(len(row)), key=row.__getitem__)] == t)
                   for row, t in zip(probs, y_true) if lo <= max(row) < hi or (b == bins - 1 and max(row) == 1.0)]
        if members:
            conf = sum(c for c, _ in members) / len(members)
            acc = sum(1 for _, ok in members if ok) / len(members)
            ece += len(members) / n * abs(acc - conf)
            table.append({"from": lo, "to": hi, "n": len(members), "confidence": round(conf, 3), "accuracy": round(acc, 3)})
    return {"n": n, "brier": round(brier, 4), "expected_calibration_error": round(ece, 4), "bins": table}


def readiness(examples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Whether each split has enough examples of every class. Not ready means: keep collecting reviews."""
    counts = {s: Counter(e["reviewer_label"] for e in examples if e["split"] == s) for s in mood_dataset.SPLITS}
    shortfalls = [f"{s}: {c} has {counts[s].get(c, 0)} of {MIN_PER_CLASS[s]} needed"
                  for s in mood_dataset.SPLITS for c in CLASSES if counts[s].get(c, 0) < MIN_PER_CLASS[s]]
    return {"ready": not shortfalls, "shortfalls": shortfalls,
            "class_counts": {s: {c: counts[s].get(c, 0) for c in CLASSES} for s in counts}}


def _matrix(examples: List[Dict[str, Any]]):
    import numpy as np

    return np.array([to_row(e["features"]) for e in examples], dtype=float), [e["reviewer_label"] for e in examples]


def _evaluate_active(run: Optional[Dict[str, Any]], rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """How the model currently in use does on these same held-out rows (None if there isn't one or it can't run)."""
    if run is None:
        return None
    try:
        pred = [mood_model.predict(run, r["features"])["recommendation"] for r in rows]
    except Exception as e:
        logger.warning("The active model couldn't be evaluated on the new data: %s", e)
        return None
    return {"run": run["_id"], **metrics([r["reviewer_label"] for r in rows], pred)}


def guard(candidate: Dict[str, Any], active: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Guarded training: is the candidate at least as good as the model in use on the same held-out examples? It must not
    lose more than a little accuracy, and must not miss more HOLDs or approve more drafts reviewers would stop."""
    if active is None:
        return {"compared_with": None, "not_worse": True, "reasons": []}
    reasons = []
    if candidate["accuracy"] is not None and active["accuracy"] is not None and \
            candidate["accuracy"] < active["accuracy"] - GUARD_ACCURACY_TOLERANCE:
        reasons.append(f"accuracy dropped from {active['accuracy']:g} to {candidate['accuracy']:g} "
                       f"(more than {GUARD_ACCURACY_TOLERANCE * 100:g} points)")
    for key, what in (("missed_hold_count", "misses more HOLDs"), ("unsafe_approval_count", "approves more drafts reviewers would stop")):
        if candidate[key] > active[key]:
            reasons.append(f"it {what} than the active model ({candidate[key]} vs {active[key]})")
    return {"compared_with": active["run"], "not_worse": not reasons, "reasons": reasons,
            "decision": "eligible" if not reasons else "rejected_keep_active_model"}


async def train(dataset_version: str, allow_small: bool = False, params: Optional[Dict[str, Any]] = None,
                rounds: int = 200, seed: int = 7) -> Dict[str, Any]:
    """Train on a fixed dataset (see train_from_rows)."""
    manifest, examples = await mood_dataset.load_dataset(dataset_version)
    if manifest.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise NotReady(f"Dataset {dataset_version} is feature schema {manifest.get('feature_schema_version')}; "
                       f"the code builds {FEATURE_SCHEMA_VERSION}.")
    return await train_from_rows(examples, dataset_version, allow_small=allow_small, params=params, rounds=rounds, seed=seed)


async def train_from_rows(rows: List[Dict[str, Any]], dataset_version: str, allow_small: bool = False,
                          params: Optional[Dict[str, Any]] = None, rounds: int = 200, seed: int = 7) -> Dict[str, Any]:
    """Train on training rows (each with the required attributes and a split) and record the run: status TRAINED, or
    EXPERIMENT when it was allowed on too little data (an experiment can never be promoted). Raises NotReady when a
    class has too few examples. Writes the model, its classes and its metadata as three files."""
    if not mood_model.is_xgboost_available():
        raise NotReady("XGBoost is not installed (pip install xgboost): no model can be trained.")
    import numpy as np
    import xgboost

    validate_rows(rows)
    ready = readiness(rows)
    if not ready["ready"] and not allow_small:
        raise NotReady("Not enough reviewed examples yet, so no model was trained. Keep collecting reviews. "
                       + "; ".join(ready["shortfalls"]))
    by_split = {s: [e for e in rows if e.get("split") == s] for s in mood_dataset.SPLITS}
    if not all(by_split.values()):
        raise NotReady("A split is empty: " + ", ".join(f"{s}={len(v)}" for s, v in by_split.items()))

    async with _training_lock:
        X, y = {}, {}
        for s, part in by_split.items():
            X[s], labels = _matrix(part)
            y[s] = np.array([CLASSES.index(label) for label in labels])
        counts = Counter(y["train"].tolist())
        weights = np.array([len(y["train"]) / (len(CLASSES) * counts[c]) for c in y["train"]])  # rarer classes count more
        cfg = {"objective": "multi:softprob", "num_class": len(CLASSES), "eval_metric": "mlogloss",
               "tree_method": os.getenv("XGBOOST_TRAIN_TREE_METHOD", "hist"),
               "max_depth": max(1, int(os.getenv("XGBOOST_TRAIN_MAX_DEPTH", "4"))),
               "nthread": max(1, int(os.getenv("XGBOOST_TRAIN_NTHREAD", "1"))),
               "eta": 0.1, "subsample": 0.8, "min_child_weight": 1, "seed": seed, "verbosity": 0, **(params or {})}
        names = list(FEATURE_NAMES)
        dtrain = xgboost.DMatrix(X["train"], label=y["train"], weight=weights, missing=np.nan, feature_names=names)
        dval = xgboost.DMatrix(X["validation"], label=y["validation"], missing=np.nan, feature_names=names)
        booster = xgboost.train(cfg, dtrain, rounds, evals=[(dval, "validation")], early_stopping_rounds=20, verbose_eval=False)

        active_run = await mood_model.registered("PROMOTED") or await mood_model.registered("SHADOW")
        results: Dict[str, Any] = {}
        cal: Dict[str, Any] = {}
        for s in ("validation", "test"):
            probs = booster.predict(xgboost.DMatrix(X[s], missing=np.nan, feature_names=names),
                                    iteration_range=(0, booster.best_iteration + 1)).tolist()
            pred = [CLASSES[max(range(len(r)), key=r.__getitem__)] for r in probs]
            truth = [e["reviewer_label"] for e in by_split[s]]
            results[s] = {"model": metrics(truth, pred),
                          "rules": metrics(truth, [e.get("system_recommendation") or "WAIT" for e in by_split[s]]),
                          "active": _evaluate_active(active_run, by_split[s])}
            cal[s] = calibration(probs, truth)
        guarded = guard(results["test"]["model"], results["test"]["active"])
        guarded["validation"] = guard(results["validation"]["model"], results["validation"]["active"])

        run_id = f"{dataset_version}-xgb-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        mood_model.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        model_path, labels_path, metadata_path = mood_model.artifact_paths(mood_model.MODEL_DIR / f"{run_id}.json")
        train_features = [e["features"] for e in by_split["train"]]
        ranges = {}
        for name in names:
            seen = [f[name] for f in train_features if f.get(name) is not None]
            if seen:
                ranges[name] = [min(seen), max(seen)]
        metadata = {
            "metadata_version": 1, "feature_schema_version": FEATURE_SCHEMA_VERSION, "feature_names": names,
            "dataset_version": dataset_version, "training_rows": len(by_split["train"]), "class_count": len(CLASSES),
            "class_counts": {c: counts.get(i, 0) for i, c in enumerate(CLASSES)},
            "feature_ranges": ranges, "params": cfg, "best_iteration": int(booster.best_iteration),
            "guarded_training": guarded, "trained_at": datetime.now(timezone.utc).isoformat(),
        }
        booster.save_model(str(model_path))
        labels_path.write_text(json.dumps(CLASSES, indent=2), encoding="utf-8")
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        mood_model.clear_cache()

        run = {
            "_id": run_id, "model_version": run_id, "algorithm": "xgboost", "xgboost_version": xgboost.__version__,
            "dataset_version": dataset_version, "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "features": names, "classes": CLASSES, "params": cfg, "best_iteration": int(booster.best_iteration),
            "metrics": results, "calibration": cal, "guarded": guarded, "readiness": ready,
            "model_path": str(model_path), "labels_path": str(labels_path), "metadata_path": str(metadata_path),
            "status": "TRAINED" if ready["ready"] else "EXPERIMENT", "created_at": mood_store.now(),
        }
        await mood_store.insert_once(mood_store.MODEL_RUNS, run)
        return run


def check_promotion(run: Dict[str, Any], criteria: Optional[Dict[str, float]] = None) -> List[str]:
    """Why a run can't be promoted (empty when it can). Judged on the newest, untouched test period, against the criteria."""
    c = {**DEFAULT_CRITERIA, **(criteria or {})}
    problems: List[str] = []
    if run.get("status") not in ("TRAINED", "SHADOW"):
        problems.append(f"status is {run.get('status')}: only a TRAINED or SHADOW run on enough data can be promoted")
    if not run.get("readiness", {}).get("ready"):
        problems.append("it was trained on too few examples of some class")
    if run.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        problems.append(f"it was built for feature schema {run.get('feature_schema_version')}, not {FEATURE_SCHEMA_VERSION}")
    test = (run.get("metrics") or {}).get("test", {}).get("model")
    if not test:
        return problems + ["it has no test results"]
    if test["n"] < c["min_test_examples"]:
        problems.append(f"only {test['n']} test examples (need {c['min_test_examples']:g})")

    def need(name: str, value: Optional[float], limit: float, upper: bool) -> None:
        if value is None:
            problems.append(f"{name} can't be judged: no such cases in the test period")
        elif (value > limit) if upper else (value < limit):
            problems.append(f"{name} is {value:g} ({'above' if upper else 'below'} {limit:g})")

    need("missed HOLD rate", test["missed_hold_rate"], c["max_missed_hold_rate"], True)
    need("unsafe approval rate", test["unsafe_approval_rate"], c["max_unsafe_approval_rate"], True)
    need("unnecessary HOLD rate", test["unnecessary_hold_rate"], c["max_unnecessary_hold_rate"], True)
    need("POST precision", test["post_precision"], c["min_post_precision"], False)
    need("WAIT rate", test["wait_rate"], c["max_wait_rate"], True)
    need("calibration error", ((run.get("calibration") or {}).get("test") or {}).get("expected_calibration_error"),
         c["max_calibration_error"], True)
    guarded = run.get("guarded") or {}
    if guarded.get("not_worse") is False:
        problems.append("it is worse than the model in use (" + "; ".join(guarded.get("reasons") or []) + ")")
    rules = (run.get("metrics") or {}).get("test", {}).get("rules") or {}
    if rules.get("missed_hold_count") is not None and test["missed_hold_count"] > rules["missed_hold_count"]:
        problems.append(f"it misses more HOLDs ({test['missed_hold_count']}) than the rules do ({rules['missed_hold_count']})")
    return problems


async def _set_status(run_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    await mood_store.update(mood_store.MODEL_RUNS, run_id, {"status": status, **(extra or {})})
    mood_model.clear_cache()


async def shadow(run_id: str) -> Dict[str, Any]:
    """Register a run so assessments record its recommendation beside the rules'. Changes no decision."""
    run = await mood_store.get(mood_store.MODEL_RUNS, run_id)
    if not run:
        raise NotReady(f"No model run {run_id}.")
    if run["status"] not in ("TRAINED", "SHADOW", "PROMOTED"):
        raise NotReady(f"Run {run_id} is {run['status']}: it was trained on too little data to be tried even in shadow.")
    for old in await mood_store.find(mood_store.MODEL_RUNS, {"status": "SHADOW"}):
        await _set_status(old["_id"], "RETIRED", {"retired_at": mood_store.now()})
    if run["status"] != "PROMOTED":
        await _set_status(run_id, "SHADOW", {"shadow_since": mood_store.now()})
    return await mood_store.get(mood_store.MODEL_RUNS, run_id)


async def promote(run_id: str, approved_by: str, criteria: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Let a model decide, inside the guard rails. Refused unless it meets the criteria and a named person approves."""
    if not (approved_by or "").strip():
        raise NotReady("Promotion needs the name of the person who approved it.")
    run = await mood_store.get(mood_store.MODEL_RUNS, run_id)
    if not run:
        raise NotReady(f"No model run {run_id}.")
    problems = check_promotion(run, criteria)
    if problems:
        raise NotReady("Not promoted: " + "; ".join(problems))
    for old in await mood_store.find(mood_store.MODEL_RUNS, {"status": "PROMOTED"}):
        await _set_status(old["_id"], "RETIRED", {"retired_at": mood_store.now()})
    await _set_status(run_id, "PROMOTED", {"promoted_by": approved_by.strip(), "promoted_at": mood_store.now(),
                                           "criteria": {**DEFAULT_CRITERIA, **(criteria or {})}})
    return await mood_store.get(mood_store.MODEL_RUNS, run_id)


async def retire(run_id: str) -> None:
    """Take a model out of use at once: assessments go back to the rules."""
    await _set_status(run_id, "RETIRED", {"retired_at": mood_store.now()})


async def disagreements(limit: int = 50) -> List[Dict[str, Any]]:
    """Assessments where the shadow model and the rules disagreed (newest first), with what a reviewer decided, if any."""
    docs = await mood_store.find(mood_store.ASSESSMENTS, {"decision.shadow.status": "OK"}, sort=[("assessed_at", -1)])
    out = []
    for a in docs:
        shadow_rec = a["decision"]["shadow"]["recommendation"]
        if shadow_rec != a["decision"].get("final", a.get("system_recommendation")):
            from services.compliance.analytics import mood_reviews

            res = mood_reviews.resolve_decision(await mood_reviews.decisions_for(a["_id"]))
            out.append({"assessment_id": a["_id"], "submission_id": a["submission_id"], "assessed_at": a["assessed_at"],
                        "rules": a["decision"].get("final"), "shadow": shadow_rec,
                        "probabilities": a["decision"]["shadow"].get("probabilities"),
                        "reviewer": res.label if res.status == "ACCEPTED" else res.status})
        if len(out) >= limit:
            break
    return out
