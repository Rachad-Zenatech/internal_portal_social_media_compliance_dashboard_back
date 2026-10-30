import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services import analysis_progress_service as ap

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
MB = 1048576
T = ap.Timings(video_per_mb=4.0, video_min=20.0, image=10.0, document=8.0, from_history=True)


def file(kind="MAIN", mime="video/mp4", name="clip.mp4", size=10 * MB, **states):
    f = {
        "kind": kind, "mime_type": mime, "original_name": name, "size_bytes": size, "created_at": NOW,
        "transcript_status": None, "transcript_started_at": None,
        "image_text_status": None, "image_text_started_at": None,
        "doc_text_status": None, "doc_text_started_at": None,
    }
    f.update(states)
    return f


def image(**states):
    return file(kind="IMAGE", mime="image/png", name="slide.png", size=MB, **states)


class EstimateTests(unittest.TestCase):
    def test_a_video_being_read_counts_down_from_when_it_started(self):
        started = NOW - timedelta(seconds=15)
        p = ap.build([file(transcript_status="PROCESSING", transcript_started_at=started)], T, NOW)
        (step,) = p.steps
        self.assertEqual((step.kind, step.state, step.expected_seconds), ("VIDEO", "RUNNING", 40))  # 10 MB x 4 s
        self.assertEqual(step.expected_done_at, started + timedelta(seconds=40))
        self.assertEqual(p.expected_done_at, started + timedelta(seconds=40 + ap.RECHECK_SECONDS))
        self.assertEqual(p.started_at, started)

    def test_a_queued_video_hasnt_started_and_small_ones_get_the_floor(self):
        p = ap.build([file(size=MB, transcript_status="PENDING")], T, NOW)
        (step,) = p.steps
        self.assertEqual((step.state, step.started_at, step.expected_seconds), ("WAITING", None, 20))

    def test_images_are_read_a_few_at_a_time(self):
        p = ap.build([image() for _ in range(4)], T, NOW)
        (step,) = p.steps
        self.assertEqual(step.label, "Reading the text in 4 images")
        # 4 images, MAX_CONCURRENT at a time, 10 s each.
        batches = -(-4 // ap.image_text_service.MAX_CONCURRENT)
        self.assertEqual(step.expected_seconds, batches * 10)

    def test_the_score_waits_for_the_slowest_step(self):
        video = file(transcript_status="PROCESSING", transcript_started_at=NOW)
        p = ap.build([video, image(), file(kind="SCRIPT", mime="text/plain", name="script.docx", size=1000)], T, NOW)
        self.assertEqual([s.kind for s in p.steps], ["VIDEO", "IMAGES", "DOCUMENTS"])
        self.assertEqual(p.steps[2].label, "Reading the script")
        self.assertEqual(p.expected_done_at, NOW + timedelta(seconds=40 + ap.RECHECK_SECONDS))

    def test_finished_and_failed_reads_are_not_steps(self):
        p = ap.build([file(transcript_status="COMPLETED"), image(image_text_status="FAILED")], T, NOW)
        self.assertEqual(p.steps, [])
        # Reading is done; only the re-check is left.
        self.assertEqual(p.expected_done_at, NOW + timedelta(seconds=ap.RECHECK_SECONDS))

    def test_a_huge_video_is_capped(self):
        self.assertEqual(ap.video_seconds(10_000 * MB, T), ap.VIDEO_MAX)


class TimingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ap._timings = None

    def tearDown(self):
        ap._timings = None

    async def test_defaults_until_there_are_enough_finished_reads(self):
        rows = [{"kind": "VIDEO", "n": 2, "median_secs": 99, "median_per_mb": 9, "min_secs": 50}]
        with patch(f"{ap.__name__}.fetch_all", new=AsyncMock(return_value=rows)):
            t = await ap.timings()
        self.assertEqual((t.video_per_mb, t.image, t.from_history), (ap.DEFAULT_VIDEO_PER_MB, ap.DEFAULT_IMAGE, False))

    async def test_recent_reads_set_the_estimate_and_are_cached(self):
        rows = [
            {"kind": "VIDEO", "n": 10, "median_secs": 60, "median_per_mb": 5.5, "min_secs": 25},
            {"kind": "IMAGE", "n": 8, "median_secs": 7, "median_per_mb": None, "min_secs": 3},
        ]
        fetch = AsyncMock(return_value=rows)
        with patch(f"{ap.__name__}.fetch_all", new=fetch):
            t = await ap.timings()
            await ap.timings()
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual((t.video_per_mb, t.video_min, t.image, t.document), (5.5, 25.0, 7.0, ap.DEFAULT_DOCUMENT))
        self.assertTrue(t.from_history)

    async def test_progress_for_several_posts_in_one_query(self):
        a, b = uuid4(), uuid4()
        rows = [{"submission_id": a, **file(transcript_status="PENDING")}, {"submission_id": b, **image()}]
        fetch = AsyncMock(side_effect=[rows, []])
        with patch(f"{ap.__name__}.fetch_all", new=fetch):
            out = await ap.for_submissions([a, b])
        self.assertEqual([s.kind for s in out[a].steps], ["VIDEO"])
        self.assertEqual([s.kind for s in out[b].steps], ["IMAGES"])

    async def test_no_posts_no_query(self):
        fetch = AsyncMock()
        with patch(f"{ap.__name__}.fetch_all", new=fetch):
            self.assertEqual(await ap.for_submissions([]), {})
        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
