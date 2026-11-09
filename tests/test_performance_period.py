import os
import unittest
from datetime import date, datetime, timedelta, timezone

# The router imports auth, which needs a signing secret; CI has no .env (same as tests/test_security_fixes.py).
os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.analytics import performance_service
from services.compliance.submissions import review_deadline_service
from tools.compliance.analytics import performance_router

USER = uuid4()


class PeriodTests(unittest.TestCase):
    def test_last_n_days_ends_now(self):
        since, until = performance_service.period(7)
        self.assertAlmostEqual((until - since).total_seconds(), 7 * 86400, delta=1)
        self.assertLess(abs((until - datetime.now(timezone.utc)).total_seconds()), 5)

    def test_a_custom_range_covers_whole_days_in_the_team_time_zone(self):
        since, until = performance_service.period(start=date(2026, 3, 1), end=date(2026, 3, 31))
        tz = review_deadline_service.WORK_TZ
        self.assertEqual(since.astimezone(tz).replace(tzinfo=None), datetime(2026, 3, 1, 0, 0))
        # The end day is included: the window runs to the start of the next day.
        self.assertEqual(until.astimezone(tz).replace(tzinfo=None), datetime(2026, 4, 1, 0, 0))

    def test_a_one_day_range_is_that_whole_day(self):
        since, until = performance_service.period(start=date(2026, 7, 4), end=date(2026, 7, 4))
        self.assertEqual(until - since, timedelta(days=1))


class RangeRequestTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, **kwargs):
        with patch.object(performance_service, "report", new=AsyncMock(return_value="report")) as report:
            result = await performance_router.performance(
                USER, days=kwargs.get("days", 30), start=kwargs.get("start"), end=kwargs.get("end"),
                platform=None, brand=None, mine=None,
            )
        return result, report

    async def test_a_custom_range_is_passed_on(self):
        _, report = await self.call(start=date(2026, 1, 1), end=date(2026, 1, 31))
        self.assertEqual(report.await_args.kwargs["start"], date(2026, 1, 1))
        self.assertEqual(report.await_args.kwargs["end"], date(2026, 1, 31))

    async def test_last_n_days_still_works(self):
        _, report = await self.call(days=7)
        self.assertEqual(report.await_args.kwargs["days"], 7)
        self.assertIsNone(report.await_args.kwargs["start"])

    async def assert_refused(self, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            await self.call(**kwargs)
        self.assertEqual(caught.exception.status_code, 422)
        return caught.exception.detail

    async def test_both_dates_are_needed(self):
        self.assertIn("both", await self.assert_refused(start=date(2026, 1, 1)))
        self.assertIn("both", await self.assert_refused(end=date(2026, 1, 1)))

    async def test_start_cant_be_after_end(self):
        self.assertIn("on or before", await self.assert_refused(start=date(2026, 2, 1), end=date(2026, 1, 1)))

    async def test_a_range_is_at_most_a_year(self):
        await self.assert_refused(start=date(2025, 1, 1), end=date(2026, 1, 2))
        await self.call(start=date(2025, 1, 1), end=date(2025, 12, 31))


class LivePostsWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_query_uses_the_window(self):
        since, until = performance_service.period(start=date(2026, 5, 1), end=date(2026, 5, 31))
        with patch.object(performance_service, "fetch_all", new=AsyncMock(return_value=[])) as fetch_all:
            await performance_service._live_posts(USER, True, since, until)
        sql, *params = fetch_all.await_args.args
        self.assertIn("s.publish_at >= $1 AND s.publish_at < $4", sql)
        self.assertEqual((params[0], params[3]), (since, until))


if __name__ == "__main__":
    unittest.main()
