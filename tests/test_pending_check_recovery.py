"""A completed media callback cannot be lost behind an older pending-check save."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from services.compliance.submissions import pending_check_recovery as recovery, manual_check_service as manual


def pending(snapshot="pending"):
    return {"manual_requested_at":"2026-11-26T14:00:00+00:00","analysis_pending":True,"check_snapshot_id":snapshot}


def file(status="COMPLETED",mime="image/png",kind="MAIN",name="photo.png"):
    return {"id":"file","kind":kind,"mime_type":mime,"original_name":name,
            "image_text_status":status,"doc_text_status":status,"transcript_status":status}


class Context:
    def __init__(self,value): self.value=value
    async def __aenter__(self): return self.value
    async def __aexit__(self,*args): return False


def services(row,current=None):
    conn=SimpleNamespace(execute=AsyncMock(),fetchval=AsyncMock(return_value=current if current is not None else pending()))
    conn.transaction=lambda:Context(conn)
    result=SimpleNamespace(score=85,traffic_light="YELLOW",violations_count=0)
    check={"score":85,"analysis_pending":False,"ai_unavailable":False,"check_snapshot_id":"finished"}
    subs=SimpleNamespace(fetch_one=AsyncMock(return_value=row),manual_check_service=manual,
        media_freshness=SimpleNamespace(REVISION_SQL="NULL"),_run_check=AsyncMock(return_value=(result,check)),
        _json=lambda v:json.loads(v) if isinstance(v,str) else v,get_pool=lambda:SimpleNamespace(acquire=lambda:Context(conn)),
        _check_still_current=AsyncMock(return_value=True),_mirror_to_script_version=AsyncMock(),_add_event=AsyncMock(),
        _notify=AsyncMock(),logger=Mock(),analysis_pending=lambda c:c.get("analysis_pending",False))
    return subs,conn


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        recovery._last_attempt.clear()
        recovery._finishing.clear()

    async def test_callback_before_pending_save_is_recovered_after_save(self):
        row={"id":"sub","latest_check":{"analysis_pending":False},"current_version":1}
        subs,conn=services(row)
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file()])):
            await recovery.finish_for_file("file",["MAIN"],"image text",services=subs)
            subs._run_check.assert_not_called()
            row["latest_check"]=pending()
            await recovery.recover("sub",services=subs)
        self.assertEqual(subs._run_check.await_count,1)
        self.assertEqual(conn.execute.await_count,2)
        written=json.loads(conn.execute.await_args_list[0].args[2])
        self.assertFalse(written["analysis_pending"])
        self.assertEqual(written["manual_requested_at"],pending()["manual_requested_at"])

    async def test_an_image_still_reading_does_not_start_partial_rechecks(self):
        row={"id":"sub","latest_check":pending(),"current_version":1}
        subs,_=services(row)
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file("PROCESSING")])):
            await recovery.finish_for_file("file",["MAIN"],"image text",services=subs)
        subs._run_check.assert_not_called()

    async def test_a_newer_saved_run_cannot_be_overwritten(self):
        row={"id":"sub","latest_check":pending(),"current_version":1}
        subs,conn=services(row,current=pending("newer-snapshot"))
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file()])):
            await recovery.finish_for_file("file",["MAIN"],"image text",services=subs)
        subs._run_check.assert_awaited_once()
        conn.execute.assert_not_called()

    async def test_a_completed_result_cannot_be_replaced_by_duplicate_finisher(self):
        row={"id":"sub","latest_check":pending(),"current_version":1}
        subs,conn=services(row,current={"analysis_pending":False})
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file()])):
            await recovery.finish_for_file("file",["MAIN"],"image text",services=subs)
        conn.execute.assert_not_called()

    async def test_completed_media_and_failed_media_are_both_terminal(self):
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file("FAILED")])):
            self.assertTrue(await recovery.media_ready("sub"))

    async def test_repeated_polls_share_one_recovery_task(self):
        gate=asyncio.Event()
        async def blocked(*args,**kwargs): await gate.wait()
        row={"id":"sub","latest_check":pending()}
        with patch.object(recovery,"recover",AsyncMock(side_effect=blocked)) as recover:
            recovery.schedule(row); recovery.schedule(row)
            self.assertEqual(len(recovery._queued),1)
            task=recovery._queued["sub"]
            await asyncio.sleep(0)
            gate.set(); await task
            self.assertEqual(recover.await_count,1)
            self.assertNotIn("sub",recovery._queued)

    async def test_unrequested_or_completed_checks_never_start_ai(self):
        with patch.object(recovery,"recover",AsyncMock()) as recover:
            recovery.schedule({"id":"sub","latest_check":{"analysis_pending":True}})
            recovery.schedule({"id":"sub","latest_check":{"analysis_pending":False}})
            await asyncio.sleep(0)
            recover.assert_not_called()

    async def test_poll_restarts_missing_reader_without_running_compliance_early(self):
        from services.compliance.media import image_text_service
        with patch.object(recovery,"files_for",AsyncMock(return_value=[file(None)])), \
             patch.object(recovery,"finish_for_file",AsyncMock()) as finish, \
             patch.object(image_text_service,"schedule_missing",AsyncMock()) as images:
            await recovery.recover("sub")
        images.assert_awaited_once_with(["file"])
        finish.assert_not_called()

    async def test_changed_media_requires_a_new_manual_request(self):
        row={"id":"sub","media_edited_at":"2026-11-26T14:05:00+00:00", "latest_check":{
            **pending(), "checked_media_edited_at":"2026-11-26T14:00:00+00:00"}}
        with patch.object(recovery,"recover",AsyncMock()) as recover:
            recovery.schedule(row)
            await asyncio.sleep(0)
            recover.assert_not_called()

    def test_changed_request_marker_cannot_be_completed_by_old_worker(self):
        newer={**pending(),"manual_requested_at":"2026-11-26T15:00:00+00:00"}
        self.assertFalse(recovery.same_run(pending(),newer))

    def test_older_replaced_video_is_not_waited_on(self):
        self.assertEqual(recovery.pending_files([file("COMPLETED","video/mp4"),file("PROCESSING","video/mp4")]),[])

    def test_documents_wait_for_their_own_reader(self):
        self.assertEqual(len(recovery.pending_files([file(None,"application/pdf","SCRIPT","script.pdf")])),1)
