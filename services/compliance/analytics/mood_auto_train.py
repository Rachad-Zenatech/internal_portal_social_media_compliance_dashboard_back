"""Training the decision model again whenever a reviewer's decision changes, without anyone running it.

Only a review triggers it: a decision given, changed, confirmed, resolved or withdrawn. A scrape does not, because it adds
evidence but no new labelled examples, so it can't change what the model learns from. (Corrections to matching or to
sentiment don't either: they don't change the decision labels.)

A trigger only asks; it never blocks what caused it. The work is done by `run_once`, in its own Python process
(`scripts.mood_ml auto-train`), so an API request is never held up by training and a crash can't take the API down.
Each attempt first checks whether there is anything new to learn from, so most attempts are no-ops:

    - an attempt that finds the same reviewed examples as the last training stops;
    - one that finds too few examples of some class, or too few independent groups, stops and says what is missing;
      it creates no dataset, so unready attempts leave nothing behind.

Only when there is enough new reviewed data does it make a new fixed dataset (auto_<timestamp>) and train on it. The
result is recorded as a TRAINED run (and compared with the model in use, as any training is). It never registers a model
for shadowing and never promotes one: those stay deliberate (`scripts.mood_ml shadow` / `promote`), so a model that
just happened to train never starts deciding anything.

Triggers are debounced: a burst of reviews becomes one run, at most one run at a time, and a trigger that arrives
during a run causes one more run afterwards. MOOD_AUTO_TRAIN=off turns it all off.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.ai.core import mood_store
from services.compliance.analytics import mood_dataset, mood_model, mood_training

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[3]
STATE_ID = "state"


def enabled() -> bool:
    return mood_model.auto_train_enabled()


def min_interval_seconds() -> float:
    try:
        return float(os.getenv("MOOD_AUTO_TRAIN_MIN_MINUTES", "10")) * 60
    except ValueError:
        return 600.0


def timeout_seconds() -> float:
    try:
        return float(os.getenv("MOOD_AUTO_TRAIN_TIMEOUT_SECONDS", "900"))
    except ValueError:
        return 900.0


def signature(examples: List[Dict[str, Any]]) -> str:
    """What the reviewed data amounts to: which assessments, labelled how. Equal signatures mean nothing new to learn."""
    rows = sorted((e["assessment_id"], e["reviewer_label"], e.get("label_source") or "") for e in examples)
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


async def read_state() -> Dict[str, Any]:
    return await mood_store.get(mood_store.AUTO_TRAINING, STATE_ID) or {}


async def _remember(fields: Dict[str, Any]) -> None:
    state = await read_state()
    state.update({"_id": STATE_ID, **fields})
    await mood_store.upsert(mood_store.AUTO_TRAINING, state)


async def run_once(trigger: str) -> Dict[str, Any]:
    """One attempt, here and now. Returns what happened (outcome TRAINED, SKIPPED or FAILED, and why) and records it."""
    outcome: Dict[str, Any] = {"trigger": trigger, "at": mood_store.now(), "outcome": "SKIPPED", "reason": None,
                               "dataset_version": None, "run_id": None}
    trained_signature: Optional[str] = None
    try:
        if not mood_store.is_enabled():
            outcome["reason"] = "the vector database isn't configured"
        elif not mood_model.is_xgboost_available():
            outcome["reason"] = "xgboost isn't installed"
        else:
            try:
                examples, excluded = await mood_dataset.select_examples("preview")
            except mood_dataset.DatasetError as e:
                outcome["reason"] = f"not enough reviewed data yet: {e}"
            else:
                sig = signature(examples)
                if (await read_state()).get("trained_signature") == sig:
                    outcome["reason"] = "no new reviewed examples since the last training"
                else:
                    ready = mood_training.readiness(examples)
                    if not ready["ready"]:
                        outcome["reason"] = "not enough reviewed examples yet: " + "; ".join(ready["shortfalls"])
                    else:
                        version = f"auto_{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
                        await mood_dataset.build_dataset(version)
                        run = await mood_training.train(version)
                        trained_signature = sig
                        outcome.update(outcome="TRAINED", dataset_version=version, run_id=run["_id"], run_status=run["status"],
                                       guarded=(run.get("guarded") or {}).get("decision") or "no model in use to compare with",
                                       reason="trained on new reviewed data; not registered, not promoted")
    except (mood_training.NotReady, mood_dataset.DatasetError) as e:
        outcome["reason"] = str(e)
    except Exception as e:
        logger.exception("Automatic training failed")
        outcome.update(outcome="FAILED", reason=f"{type(e).__name__}: {str(e)[:300]}")
    try:
        fields: Dict[str, Any] = {"last_attempt": outcome}
        if trained_signature:
            fields["trained_signature"] = trained_signature
        await _remember(fields)
    except Exception as e:
        logger.warning("Couldn't record the automatic training attempt: %s", e)
    return outcome


# --- triggers: ask, never wait ----------------------------------------------------------------------------------

_runner: Dict[str, Any] = {"task": None, "pending": None, "last_start": None}


def schedule(trigger: str) -> None:
    """Ask for an attempt (a review). Returns at once; does nothing when auto-training is off, there is no
    vector database, or there is no running event loop. If an attempt is already waiting or running, this one is folded
    into it (or, if it is already running, causes one more afterwards)."""
    if not enabled() or not mood_store.is_enabled():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _runner["task"]
    if task is not None and not task.done():
        _runner["pending"] = trigger
        return
    _runner["task"] = asyncio.create_task(_worker(trigger))


def _spawn(trigger: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "scripts.mood_ml", "auto-train", "--trigger", trigger],
        cwd=str(ROOT_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout_seconds(), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


async def _attempt_in_own_process(trigger: str) -> None:
    try:
        done = await asyncio.to_thread(_spawn, trigger)
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-2:]
            raise RuntimeError(f"the training process exited with {done.returncode}: {' | '.join(tail)[:300]}")
    except subprocess.TimeoutExpired:
        await _record_failure(trigger, f"took longer than {timeout_seconds():.0f}s and was stopped")
    except Exception as e:
        await _record_failure(trigger, str(e))


async def _record_failure(trigger: str, reason: str) -> None:
    logger.warning("Automatic training (%s) failed: %s", trigger, reason)
    try:
        await _remember({"last_attempt": {"trigger": trigger, "at": mood_store.now(), "outcome": "FAILED", "reason": reason,
                                          "dataset_version": None, "run_id": None}})
    except Exception as e:
        logger.warning("Couldn't record the automatic training failure: %s", e)


async def _worker(trigger: Optional[str]) -> None:
    """Wait out the debounce interval, run once in a separate process, and go again if something arrived meanwhile."""
    while trigger:
        if _runner["last_start"] is not None:
            wait = _runner["last_start"] + min_interval_seconds() - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)  # a burst of triggers becomes one run
        _runner["pending"] = None
        _runner["last_start"] = time.monotonic()
        await _attempt_in_own_process(trigger)
        trigger = _runner["pending"]
