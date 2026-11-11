"""The recommendation, with an optional trained model beside the rules.

Three modes (PUBLIC_MOOD_MODEL_MODE):
    off     the rules decide, nothing else happens.
    shadow  the rules decide; a registered model's recommendation is computed too and saved with the assessment, so the
            two can be compared (this is the default, and does nothing until a model is registered).
    assist  a PROMOTED model decides, inside guard rails the model cannot override. Promotion is a deliberate step
            (scripts/mood_ml.py promote), never automatic.

What stays outside the model, whatever it says:
    - a rules HOLD stands: the model can't lower a verified HOLD;
    - missing or stale evidence caps the answer at WAIT: the model can't approve what there is no (fresh) evidence for;
    - a model built for another feature schema is not used;
    - a predicted class the model saw too few examples of, or measurements outside the range it was trained on, are not
      trusted (the rules decide and the decision says why);
    - if the model can't run, the rules decide and the assessment says so (decision source RULES_FALLBACK).

A model is three files that travel together: the model itself (`<run>.json`), its classes (`<run>.labels.json`) and its
metadata (`<run>.metadata.json`: feature schema, training size, class counts, the range of every feature it saw).
They are read again whenever a file changes. Without xgboost installed the app runs on the rules.
"""

import importlib.util
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.ai.core import mood_store
from services.compliance.analytics.mood_features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, to_row

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[3]
MODEL_DIR = Path(os.getenv("MOOD_MODEL_DIR") or ROOT_DIR / ".data" / "mood_models")
LABELS = ["POST", "WAIT", "HOLD"]
# Evidence older than this can't support POST (the same age the reviewer is warned about).
STALE_EVIDENCE_MINUTES = float(os.getenv("PUBLIC_MOOD_STALE_EVIDENCE_HOURS", "48")) * 60
REGISTRY_TTL_SECONDS = 60.0


# A predicted class needs at least this many training examples before its answer is trusted on its own.
MIN_CLASS_SUPPORT = int(os.getenv("MOOD_MIN_CLASS_SUPPORT", "20"))


def is_xgboost_available() -> bool:
    return importlib.util.find_spec("xgboost") is not None


def artifact_paths(model_path: Any) -> Tuple[Path, Path, Path]:
    """(model, labels, metadata): the three files of one model."""
    model = Path(model_path)
    base = model.with_suffix("")
    return model, base.with_name(base.name + ".labels.json"), base.with_name(base.name + ".metadata.json")


_files: Dict[str, Tuple[float, Any]] = {}  # path -> (modified time, what was read): read again when the file changes


def _read_json(path: Path) -> Optional[Any]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _files.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = None
    _files[str(path)] = (mtime, value)
    return value


def load_metadata(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The model's metadata file, or None if it has none (then nothing about its training can be vouched for)."""
    if not run.get("model_path"):
        return None
    value = _read_json(artifact_paths(run["model_path"])[2])
    return value if isinstance(value, dict) else None


def load_labels(run: Dict[str, Any]) -> Optional[List[str]]:
    if not run.get("model_path"):
        return None
    value = _read_json(artifact_paths(run["model_path"])[1])
    return value if isinstance(value, list) else None


def auto_train_enabled() -> bool:
    """Whether a scrape or a review may start a training attempt (MOOD_AUTO_TRAIN=off turns it off)."""
    return os.getenv("MOOD_AUTO_TRAIN", "on").strip().lower() not in ("off", "0", "false", "no")


def mode() -> str:
    m = os.getenv("PUBLIC_MOOD_MODEL_MODE", "shadow").strip().lower()
    return m if m in ("off", "shadow", "assist") else "shadow"


def min_confidence() -> float:
    try:
        return float(os.getenv("PUBLIC_MOOD_MODEL_MIN_CONFIDENCE", "0"))
    except ValueError:
        return 0.0


@dataclass
class Decision:
    final: str                       # POST / WAIT / HOLD: what the assessment recommends
    source: str = "RULES"            # RULES | MODEL | RULES_FALLBACK
    note: Optional[str] = None       # why a fallback or a guard rail applied
    shadow: Optional[Dict[str, Any]] = None  # the model's own answer, when one was computed (saved, not shown)
    guards: List[str] = field(default_factory=list)

    def as_record(self) -> Dict[str, Any]:
        return {"source": self.source, "final": self.final, "note": self.note, "guards": self.guards,
                "mode": mode(), "shadow": self.shadow}


# --- the registry of trained models --------------------------------------------------------------------------

_registry: Dict[str, Any] = {"at": 0.0, "runs": {}}
_boosters: Dict[str, Any] = {}


def clear_cache() -> None:
    _registry.update(at=0.0, runs={})
    _boosters.clear()


async def registered(status: str) -> Optional[Dict[str, Any]]:
    """The newest model run with this status (PROMOTED or SHADOW), cached briefly: assessments are frequent."""
    if time.monotonic() - _registry["at"] > REGISTRY_TTL_SECONDS:
        runs: Dict[str, Any] = {}
        try:
            for st in ("PROMOTED", "SHADOW"):
                found = await mood_store.find(mood_store.MODEL_RUNS, {"status": st}, sort=[("created_at", -1)], limit=1)
                runs[st] = found[0] if found else None
        except Exception as e:
            logger.warning("Couldn't read the registered models (the rules decide): %s", e)
            runs = {}
        _registry.update(at=time.monotonic(), runs=runs)
    return _registry["runs"].get(status)


def _booster(run: Dict[str, Any]):
    """The model, loaded once and again whenever its file changes."""
    if not is_xgboost_available():
        raise RuntimeError("XGBoost is not installed")
    import xgboost  # imported when a model is actually used: the app runs without it

    path = artifact_paths(run["model_path"])[0]
    mtime = path.stat().st_mtime
    cached = _boosters.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    booster = xgboost.Booster()
    booster.load_model(str(path))
    _boosters[str(path)] = (mtime, booster)
    return booster


def _matrix(features: Dict[str, Any]):
    import numpy as np
    import xgboost

    return xgboost.DMatrix(np.array([to_row(features)], dtype=float), missing=np.nan, feature_names=list(FEATURE_NAMES))


def predict(run: Dict[str, Any], features: Dict[str, Any]) -> Dict[str, Any]:
    """The model's recommendation and its class probabilities for one assessment's features."""
    classes = load_labels(run) or run.get("classes") or LABELS
    probs = _booster(run).predict(_matrix(features))[0]
    by_class = {c: round(float(p), 4) for c, p in zip(classes, probs)}
    return {"recommendation": max(by_class, key=by_class.get), "probabilities": by_class}


def support_issues(run: Dict[str, Any], features: Dict[str, Any], label: str) -> List[str]:
    """Reasons not to trust this answer on its own: too few training examples of the predicted class, or measurements
    outside what the model was trained on (it would be extrapolating). Judged from the model's own metadata."""
    meta = load_metadata(run) or {}
    issues: List[str] = []
    seen = int((meta.get("class_counts") or {}).get(label, 0) or 0)
    if seen < MIN_CLASS_SUPPORT:
        issues.append(f"the predicted class {label} had {seen} training example(s); {MIN_CLASS_SUPPORT}+ needed to trust it")
    for name, bounds in (meta.get("feature_ranges") or {}).items():
        value = features.get(name)
        if value is not None and bounds and (value < bounds[0] or value > bounds[1]):
            issues.append(f"{name} = {value:g} is outside the range seen in training ({bounds[0]:g} to {bounds[1]:g})")
    return issues


def explain(run: Dict[str, Any], features: Dict[str, Any], label: str, top: int = 3) -> List[Dict[str, Any]]:
    """What pushed the model towards `label`: the largest feature contributions (positive raises it, negative lowers
    it), from XGBoost's own attribution. Empty if it can't be computed."""
    try:
        classes = load_labels(run) or run.get("classes") or LABELS
        contribs = _booster(run).predict(_matrix(features), pred_contribs=True)[0]
        row = contribs[classes.index(label)] if getattr(contribs, "ndim", 1) == 2 else contribs
        pairs = sorted(zip(FEATURE_NAMES, row[:-1]), key=lambda p: -abs(p[1]))[:top]  # the last value is the baseline
        return [{"feature": n, "value": features.get(n), "effect": round(float(v), 3)} for n, v in pairs if abs(v) > 1e-6]
    except Exception:
        return []


# --- the decision ----------------------------------------------------------------------------------------------

def insufficient_evidence(features: Dict[str, Any]) -> Optional[str]:
    """Why there isn't (fresh) evidence to approve on, or None."""
    if not features.get("distinct_story_count"):
        return "no verified relevant evidence"
    age = features.get("snapshot_age_minutes")
    if age is None:
        return "the age of the evidence is unknown"
    if age > STALE_EVIDENCE_MINUTES:
        return f"the evidence is {age / 60:.0f} hours old"
    return None


def apply_guards(model_label: str, rules_rec: Optional[str], features: Dict[str, Any]) -> Decision:
    """The model's answer, kept inside the limits it can't override."""
    final, guards = model_label, []
    if rules_rec == "HOLD" and final != "HOLD":
        final = "HOLD"
        guards.append("RULES_HOLD_STANDS")
    reason = insufficient_evidence(features)
    if reason and final == "POST":
        final = "WAIT"
        guards.append("INSUFFICIENT_EVIDENCE_CAPS_AT_WAIT")
    note = None if not guards else ("A rules HOLD stands over the model. " if "RULES_HOLD_STANDS" in guards else "") + (
        f"Capped at WAIT: {reason}." if "INSUFFICIENT_EVIDENCE_CAPS_AT_WAIT" in guards else "")
    return Decision(final=final, source="MODEL", note=note.strip() if note else None, guards=guards)


async def decide(rules_rec: Optional[str], features: Dict[str, Any]) -> Decision:
    """What this assessment recommends, and (in shadow and assist modes) what the model says. Never raises: any
    trouble means the rules decide and the decision says why."""
    rules = rules_rec or "WAIT"
    if mode() == "off":
        return Decision(final=rules)
    try:
        promoted = await registered("PROMOTED")
        run = promoted if (mode() == "assist" and promoted) else (await registered("SHADOW") or promoted)
        if run is None:
            return Decision(final=rules)
        assisting = mode() == "assist" and run.get("status") == "PROMOTED"
        if run.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
            note = f"model {run['_id']} was built for feature schema {run.get('feature_schema_version')}, not {FEATURE_SCHEMA_VERSION}"
            return Decision(final=rules, source="RULES_FALLBACK" if assisting else "RULES", note=note,
                            shadow={"model_version": run["_id"], "status": "INCOMPATIBLE"})
        try:
            guess = predict(run, features)
        except Exception as e:
            logger.warning("The decision model failed (the rules decide): %s", e)
            return Decision(final=rules, source="RULES_FALLBACK" if assisting else "RULES",
                            note=f"the model couldn't run: {type(e).__name__}",
                            shadow={"model_version": run["_id"], "status": "ERROR", "error": str(e)[:200]})
        issues = support_issues(run, features, guess["recommendation"])
        confidence = max(guess["probabilities"].values())
        meta = load_metadata(run) or {}
        seen = int((meta.get("class_counts") or {}).get(guess["recommendation"], 0) or 0)
        shadow = {"model_version": run["_id"], "status": "OK", **guess, "confidence": round(confidence, 4),
                  # confidence can't outrun the evidence behind it: capped by how many examples of that class it learned from
                  "support_adjusted_confidence": round(min(confidence, seen / MIN_CLASS_SUPPORT if MIN_CLASS_SUPPORT else 1.0), 4),
                  "support_issues": issues, "explanation": explain(run, features, guess["recommendation"])}
        if not assisting:
            return Decision(final=rules, shadow=shadow)
        if issues:
            return Decision(final=rules, source="RULES_FALLBACK", note="the model is not trusted here: " + "; ".join(issues),
                            shadow=shadow)
        floor = min_confidence()
        if floor and shadow["support_adjusted_confidence"] < floor:
            return Decision(final=rules, source="RULES_FALLBACK", note=f"the model was not confident enough (< {floor:g})",
                            shadow=shadow)
        decision = apply_guards(guess["recommendation"], rules, features)
        decision.shadow = shadow
        return decision
    except Exception as e:
        logger.warning("Model decision unavailable (the rules decide): %s", e)
        return Decision(final=rules, source="RULES_FALLBACK", note=f"model decision unavailable: {type(e).__name__}")


async def model_status() -> Dict[str, Any]:
    """The state of the decision model for an administrator: is xgboost installed, which model is registered and loaded,
    what it was trained on, which features matter most, and how it did in training. Reads, changes nothing."""
    out: Dict[str, Any] = {"xgboost_installed": is_xgboost_available(), "mode": mode(),
                           "support_thresholds": {"min_class_support_for_auto": MIN_CLASS_SUPPORT,
                                                  "stale_evidence_hours": STALE_EVIDENCE_MINUTES / 60,
                                                  "min_confidence": min_confidence()}}
    try:
        runs = {st: await registered(st) for st in ("PROMOTED", "SHADOW")}
    except Exception as e:
        return {**out, "error": f"couldn't read the registered models: {type(e).__name__}"}
    run = runs["PROMOTED"] or runs["SHADOW"]
    out["registered"] = {st: (r["_id"] if r else None) for st, r in runs.items()}
    try:
        out["auto_training"] = {"enabled": auto_train_enabled(),
                                "last_attempt": (await mood_store.get(mood_store.AUTO_TRAINING, "state") or {}).get("last_attempt")}
    except Exception:
        out["auto_training"] = {"enabled": auto_train_enabled(), "last_attempt": None}
    if run is None:
        return {**out, "model_loaded": False, "note": "No model is registered: the rules decide every assessment."}
    model_path, labels_path, metadata_path = artifact_paths(run["model_path"])
    meta, labels = load_metadata(run), load_labels(run)
    loaded, importance, rounds = False, [], 0
    try:
        booster = _booster(run)
        loaded, rounds = True, booster.num_boosted_rounds()
        importance = sorted(({"feature": f, "importance": round(float(v), 4)}
                             for f, v in booster.get_score(importance_type="total_gain").items()), key=lambda i: -i["importance"])
    except Exception as e:
        out["load_error"] = f"{type(e).__name__}: {e}"[:200]
    stat = model_path.stat() if model_path.exists() else None
    return {
        **out, "model_loaded": loaded, "label_mapping_present": labels is not None, "metadata_present": meta is not None,
        "run": run["_id"], "run_status": run.get("status"), "model_path": str(model_path), "labels_path": str(labels_path),
        "metadata_path": str(metadata_path),
        "model_updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat() if stat else None,
        "model_size_bytes": stat.st_size if stat else None,
        "training_summary": {"dataset_version": run.get("dataset_version"), "feature_schema_version": run.get("feature_schema_version"),
                             "training_rows": (meta or {}).get("training_rows"), "class_count": len(labels or []),
                             "feature_count": len((meta or {}).get("feature_names") or []),
                             "boosting_rounds": rounds},
        "class_distribution": (meta or {}).get("class_counts"),
        "feature_importance": importance,
        "training_quality": {"test": ((run.get("metrics") or {}).get("test") or {}).get("model"), "guarded": run.get("guarded"),
                             "calibration": (run.get("calibration") or {}).get("test")},
    }
