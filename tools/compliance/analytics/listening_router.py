"""Listening topics: what social listening tracks (Sprout Social or X/Twitter JSON feed), trend discovery,
and the team's tuning of how posts and scripts are matched to it."""

from typing import Annotated, Any, Dict, List, Literal, Optional
from uuid import UUID

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from models.compliance.public_mood_model import (
    ListeningTopic,
    ListeningTopicUpdate,
    MatchTestRequest,
    MatchTestResult,
    NavPublicMoodResponse,
    XIngestRequest,
    XTrendOpportunity,
)
from postgresql_db.database import get_pool
from services.compliance.analytics import conversation_feedback, daily_scrape_schedule, mood_model, mood_training_examples, nav_public_mood_service, post_queries, public_mood_service as svc
from services.compliance.analytics.listening_providers import feed_in_database, load_feed, reset_provider_cache
from services.shared.auth.auth_service import get_current_user_id_dependency, require_permission

router = APIRouter(prefix="/listening")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
# Tuning sits with editing the compliance rulebook and the messaging context.
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


@router.get("/topics", response_model=List[ListeningTopic])
async def list_topics(user_id: CurrentUser):
    """Every topic with its current mood, the team's keyword tuning and how often it was matched wrongly."""
    return await svc.list_topics()


@router.post("/topics/sync", response_model=List[ListeningTopic])
async def sync_topics(user_id: ManageUser):
    """Pull the topic list from the listening source now instead of waiting for the daily sync."""
    return await svc.list_topics(refresh=True)


@router.put("/topics/{topic_id}", response_model=ListeningTopic)
async def update_topic(topic_id: UUID, data: ListeningTopicUpdate, user_id: ManageUser):
    return await svc.update_topic(topic_id, data, user_id)


@router.post("/match-test", response_model=MatchTestResult)
async def match_test(data: MatchTestRequest, user_id: CurrentUser):
    """Which topics this text would match and why, including the near misses. Nothing is saved."""
    return await svc.match_test(data.text)


@router.get("/feedback-reasons")
async def feedback_reasons(user_id: CurrentUser):
    """The reasons a reviewer can give when marking a match unrelated or correcting a recommendation (code -> label)."""
    return conversation_feedback.reasons()


@router.get("/model/status")
async def mood_model_status(user_id: CurrentUser):
    """The decision model: whether xgboost is installed, which model is registered and loaded, what it was trained on,
    which measurements matter most, how it did in training, and the limits that keep it in check."""
    return await mood_model.model_status()


@router.get("/model/training-data")
async def mood_model_training_data(user_id: CurrentUser, version: Optional[str] = Query(None, max_length=80)):
    """The reviewed assessments the decision model was trained on (features, reviewer label, split, source draft)."""
    return await mood_training_examples.training_examples(version)


@router.get("/trends", response_model=List[XTrendOpportunity])
async def get_trends(user_id: CurrentUser, refresh: bool = Query(False)):
    """What is trending right now, whether sentiment is positive or negative, and whether the team can jump on
    the trend (POST), wait (WAIT), or avoid/escalate due to crisis/backlash (HOLD)."""
    return await svc.list_trend_opportunities(refresh=refresh)


@router.get("/nav", response_model=NavPublicMoodResponse)
async def get_nav_public_mood(user_id: CurrentUser, refresh: bool = Query(False)):
    """Return both nav scrapings: (1) Financial sentiment about ZenaTech and (2) today's posts public mood."""
    return await nav_public_mood_service.get_nav_public_mood(
        refresh=refresh,
        scrape_state=str(_scrape.get("state") or "idle"),
    )


@router.post("/x-ingest", response_model=List[XTrendOpportunity])
async def ingest_x_comments(data: XIngestRequest, user_id: ManageUser):
    """Ingest scraped or collected X (Twitter) topics and comments, classify each comment as POSITIVE/NEGATIVE/NEUTRAL
    with confidence, store them as the current feed (the vector database's x_feed_data), and sync into the listening
    pipeline."""
    return await svc.ingest_x_json(data)

logger = logging.getLogger("listening_router")


ROOT_DIR = Path(__file__).resolve().parents[3]
SCRAPE_TIMEOUT_SECONDS = float(os.getenv("LISTENING_SCRAPE_TIMEOUT_SECONDS", "600"))

# What the scrape button last did, for GET /listening/scrape/status (in memory: it starts "idle" after a restart).
_scrape: Dict[str, Any] = {"state": "idle", "scope": None, "started_at": None, "finished_at": None, "error": None, "collection": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_scope(scope: Any) -> str:
    return scope if isinstance(scope, str) and scope in ("all", "financial", "today") else "all"


def _run_scraper_process(post_queries_file: Optional[str] = None, scope: str = "all") -> "subprocess.CompletedProcess[str]":
    """The scraper in its own Python process. Playwright needs an event loop that can start subprocesses, which the
    server's loop is not always (uvicorn --reload on Windows), and a crash or a hang there can't take the API down."""
    extra_env = {
        **({post_queries.FILE_ENV: post_queries_file} if post_queries_file else {}),
        **({"SCRAPE_SCOPE": scope} if scope != "all" else {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "scripts.scrape_x_playwright"],
        cwd=str(ROOT_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=SCRAPE_TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **extra_env},
    )


async def _collection_summary() -> Optional[Dict[str, Any]]:
    """How the scrape that just finished went (status and per-source results, as the scraper recorded them), read from
    where the feed is stored (the vector database's x_feed_data, or the JSON file when there is none)."""
    try:
        data = await load_feed()
    except Exception:
        return None
    if not data:
        return None
    info = dict(data.get("collection") or {})
    info["collected_at"] = data.get("collected_at")
    info["topics"] = len(data.get("topics") or [])
    info["stored_in"] = "vector database (x_feed_data)" if feed_in_database() else "JSON file"
    return info


async def _run_scrape_and_refresh(scope: str = "all") -> None:
    """Scrape, then bring the topics, trends and conversation index up to date. Runs only from the scrape button."""
    scope = _normalize_scope(scope)
    _scrape.update(state="running", scope=scope, started_at=_now(), finished_at=None, error=None, collection=None)
    try:
        queries_file: Optional[str] = None
        if scope != "financial":
            try:
                searches = (
                    await post_queries.today_post_searches()
                    if scope == "today"
                    else await post_queries.active_draft_searches()
                )
            except Exception:
                logger.warning("Could not read posts to build searches from", exc_info=True)
                searches = []
            queries_file = post_queries.write_file(searches)
        try:
            done = (
                await asyncio.to_thread(_run_scraper_process, queries_file)
                if scope == "all"
                else await asyncio.to_thread(_run_scraper_process, queries_file, scope)
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"The scrape took longer than {SCRAPE_TIMEOUT_SECONDS:.0f}s and was stopped.")
        finally:
            if queries_file:
                try:
                    os.remove(queries_file)
                except OSError:
                    pass
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError("The scraper failed: " + (" | ".join(tail)[:400] or f"exit code {done.returncode}"))
        reset_provider_cache()  # the feed may not have existed when the provider was first chosen
        await svc.record_conversations()  # first: the topic trends count the items collected so far
        await svc.list_trend_opportunities(refresh=True)
        _scrape.update(state="finished", collection=await _collection_summary())
        asyncio.create_task(svc.index_conversations(), name="listening-index-conversations")
    except Exception as exc:
        logger.error("Scrape from the button failed: %s", exc)
        _scrape.update(state="failed", error=str(exc)[:500])
    finally:
        _scrape["finished_at"] = _now()


@router.post("/scrape")
async def trigger_live_scrape(
    background_tasks: BackgroundTasks,
    user_id: CurrentUser,
    scope: Annotated[Literal["all", "financial", "today"], Query()] = "all",
):
    """The scrape button: collect ZenaTech financial coverage and/or today's posts public mood (scope=all|financial|today),
    then refresh topics, trends and the conversation index in the background."""
    chosen_scope = _normalize_scope(scope)
    if _scrape.get("state") == "running":
        raise HTTPException(status_code=409, detail="A scrape is already running.")
    _scrape.update(state="running", scope=chosen_scope, started_at=_now(), finished_at=None, error=None, collection=None)
    background_tasks.add_task(_run_scrape_and_refresh, chosen_scope)
    return {
        "status": "started",
        "scope": chosen_scope,
        "message": "Scraping live ZenaTech financial & today's post sentiment in the background. Check /listening/scrape/status.",
        "source": "Playwright Scraper (ZenaTech Financial & Today's Posts) + sentiment model",
    }


@router.get("/scrape/status")
async def scrape_status(user_id: CurrentUser):
    """Whether a scrape is running, and how the last one went: state (idle | running | finished | failed), start and
    finish times, the error if it failed, and the collection summary (status and each source's result) if it finished."""
    return dict(_scrape)


_daily_lock_conn: Any = None


async def _last_collected_at() -> Optional[datetime]:
    try:
        feed = await load_feed()
    except Exception:
        return None
    raw = (feed or {}).get("collected_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def _try_daily_scrape_lock() -> bool:
    global _daily_lock_conn
    try:
        pool = get_pool()
        conn = await pool.acquire()
        if await conn.fetchval("SELECT pg_try_advisory_lock(8009, 830)"):
            _daily_lock_conn = conn
            return True
        await pool.release(conn)
        return False
    except Exception:
        return True  # allow running when DB lock isn't available


async def _release_daily_scrape_lock() -> None:
    global _daily_lock_conn
    conn = _daily_lock_conn
    _daily_lock_conn = None
    if conn is None:
        return
    try:
        await conn.execute("SELECT pg_advisory_unlock(8009, 830)")
    finally:
        await get_pool().release(conn)


async def daily_public_scrape_loop() -> None:
    """Run both nav public-mood scrapings daily at 8:30 AM (default America/New_York)."""
    await daily_scrape_schedule.daily_scrape_loop(
        lambda: _run_scrape_and_refresh("all"),
        lambda: _scrape.get("state") == "running",
        _last_collected_at,
        try_lock=_try_daily_scrape_lock,
        release_lock=_release_daily_scrape_lock,
    )
