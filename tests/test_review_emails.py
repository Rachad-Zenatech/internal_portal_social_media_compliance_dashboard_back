"""Workflow email regressions. All delivery is mocked; these tests never send real email."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from models.notifications.review_email_model import ReviewEmailItem
from services.notifications.review_email import config, outbox, policy, scheduler, templates

NOW = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)  # 09:00 Toronto
ENV = {"FRONTEND_URL": "https://social.example.com", "REVIEW_EMAIL_TIMEZONE": "America/Toronto"}
USERS = {
    "reviewer": {"id": "reviewer", "name": "Reviewer", "email": "r@example.com", "groups": ["CONTENT_REVIEW_PUBLISH"], "brands": ["ZENA"]},
    "other": {"id": "other", "name": "Other", "email": "o@example.com", "groups": ["CONTENT_REVIEW_PUBLISH"], "brands": ["ZENA"]},
    "creator": {"id": "creator", "name": "Creator", "email": "c@example.com", "groups": [], "brands": ["ZENA"]},
}


def item(**overrides):
    values = dict(key="post:one:1", entity_id="one", kind="Post", title="Project update", reference="SUB-1",
                  path="/submissions/one", creator_id="creator", stage="REVIEW", due_at=NOW+timedelta(hours=1),
                  sent_at=NOW-timedelta(hours=20), brands=["ZENA"], reviewer_ids=["reviewer"])
    return ReviewEmailItem(**(values | overrides))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_digest_waits_for_nine_and_has_both_new_posts_and_resubmitted_scripts(self):
        work = [item(), item(key="script:two:2", kind="Script", resubmitted=True)]
        self.assertFalse(any(m["kind"] == "DIGEST" for m in scheduler.scheduled_messages(work, USERS, NOW-timedelta(minutes=1))))
        digest = [m for m in scheduler.scheduled_messages(work, USERS, NOW) if m["kind"] == "DIGEST"]
        self.assertEqual(len(digest), 1)
        self.assertEqual(digest[0]["user_id"], "reviewer")
        self.assertEqual([i["kind"] for i in digest[0]["payload"]["items"]], ["Post", "Script"])
        self.assertTrue(digest[0]["payload"]["items"][1]["resubmitted"])

    def test_digest_uses_previous_calendar_day_not_last_twenty_four_hours(self):
        self.assertFalse(policy.in_digest(item(sent_at=NOW-timedelta(hours=1)), NOW))
        self.assertTrue(policy.in_digest(item(sent_at=datetime(2026,11,17,5,tzinfo=timezone.utc)), NOW))
        self.assertFalse(policy.in_digest(item(sent_at=datetime(2026,11,17,4,59,tzinfo=timezone.utc)), NOW))

    def test_digest_can_catch_up_after_restart_without_a_new_dedupe_key(self):
        at_nine = [m for m in scheduler.scheduled_messages([item()], USERS, NOW) if m["kind"] == "DIGEST"][0]
        at_ten = [m for m in scheduler.scheduled_messages([item()], USERS, NOW+timedelta(hours=1)) if m["kind"] == "DIGEST"][0]
        self.assertEqual(at_nine["key"], at_ten["key"])

    def test_daylight_saving_calendar_windows(self):
        # The previous local day includes 25 hours when clocks fall back, and 23 when they advance.
        fall = datetime(2026,11,2,14,tzinfo=timezone.utc)
        spring = datetime(2026,3,9,13,tzinfo=timezone.utc)
        a,b = policy.digest_window(fall)
        self.assertEqual((b-a).total_seconds(),25*3600)
        a,b = policy.digest_window(spring)
        self.assertEqual((b-a).total_seconds(),23*3600)

    def test_reminders_only_inside_final_hour_and_never_after_due(self):
        for minutes, expected in ((61,False),(60,True),(30,True),(0,False),(-1,False)):
            self.assertEqual(policy.due_soon(item(due_at=NOW+timedelta(minutes=minutes)),NOW),expected)

    def test_changes_reminder_goes_to_creator_for_posts_and_scripts(self):
        for kind in ("Post", "Script"):
            messages=scheduler.scheduled_messages([item(stage="REVISION",kind=kind)],USERS,NOW)
            self.assertEqual([(m["kind"],m["user_id"]) for m in messages],[("REMINDER","creator")])

    def test_rescheduling_or_resubmission_makes_a_different_reminder(self):
        original=scheduler.scheduled_messages([item()],USERS,NOW)[0]["key"]
        self.assertNotEqual(original,scheduler.scheduled_messages([item(due_at=NOW+timedelta(minutes=30))],USERS,NOW)[0]["key"])
        self.assertNotEqual(original,scheduler.scheduled_messages([item(key="post:one:2")],USERS,NOW)[0]["key"])

    def test_no_empty_digests_or_reminders_for_absent_deadlines(self):
        self.assertEqual(scheduler.scheduled_messages([],USERS,NOW),[])
        messages=scheduler.scheduled_messages([item(due_at=None)],USERS,NOW)
        self.assertEqual([m["kind"] for m in messages],["DIGEST"])


class AssignmentTests(unittest.TestCase):
    def test_named_reviewer_and_approval_path_are_respected(self):
        self.assertEqual(policy.reviewers_for(item(),USERS),["reviewer"])
        self.assertEqual(policy.reviewers_for(item(approver_ids=["other"]),USERS),["reviewer","other"])

    def test_open_queue_goes_to_eligible_brand_reviewers_except_creator(self):
        self.assertEqual(policy.reviewers_for(item(reviewer_ids=[]),USERS),["reviewer","other"])
        self.assertEqual(policy.reviewers_for(item(brands=["OTHER"]),USERS),[])
        own=dict(USERS);own["creator"]=dict(USERS["reviewer"])
        self.assertNotIn("creator",policy.reviewers_for(item(reviewer_ids=[]),own))

    def test_script_checkins_stop_reminders_for_that_reviewer_and_round(self):
        self.assertEqual(policy.reviewers_for(item(document=True,checked_ids=["reviewer"]),USERS),[])
        self.assertEqual(policy.reviewers_for(item(document=True,checked_ids=[]),USERS),["reviewer"])

    def test_suggesting_reviewers_must_be_explicitly_assigned(self):
        users=dict(USERS);users["suggest"]={"groups":["CONTENT_REVIEW_SUGGEST"],"brands":[]}
        self.assertNotIn("suggest",policy.reviewers_for(item(reviewer_ids=[]),users))
        self.assertIn("suggest",policy.reviewers_for(item(reviewer_ids=["suggest"]),users))
        self.assertIn("suggest",policy.reviewers_for(item(document=True,reviewer_ids=["suggest"]),users))

    def test_level_two_goes_to_managers_only(self):
        users=dict(USERS);users["manager"]={"groups":["CONTENT_MANAGE_DRAFTS"],"brands":[]}
        self.assertEqual(policy.reviewers_for(item(document=True,stage="LEVEL2"),users),["manager"])


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict("os.environ",ENV);self.env.start();self.addCleanup(self.env.stop)

    def job(self, kind="REMINDER", work=None):
        work=work or item()
        return {"id":1,"kind":kind,"user_id":"reviewer","payload":{"items":[templates.item_payload(work)],"stage":work.stage,"date":"2026-11-18"}}

    def test_completed_deleted_or_withdrawn_work_is_cancelled(self):
        self.assertIsNone(outbox.current_payload(self.job(),{},USERS,NOW))

    def test_removed_assignees_inactive_users_and_changed_deadlines_are_cancelled(self):
        self.assertIsNone(outbox.current_payload(self.job(),{item().key:item(reviewer_ids=["other"])},USERS,NOW))
        self.assertIsNone(outbox.current_payload(self.job(),{item().key:item()}, {},NOW))
        self.assertIsNone(outbox.current_payload(self.job(),{item().key:item(due_at=NOW+timedelta(minutes=30))},USERS,NOW))

    def test_old_digest_expires_and_current_pending_work_stays(self):
        job=self.job("DIGEST")
        self.assertIsNotNone(outbox.current_payload(job,{item().key:item()},USERS,NOW))
        self.assertIsNone(outbox.current_payload(job,{item().key:item()},USERS,NOW+timedelta(days=1)))

    def test_html_escapes_creator_content_and_links_to_social_portal(self):
        work=item(title='<script>alert("x")</script>')
        subject,html,text=templates.render("REMINDER",self.job(work=work)["payload"],"<Creator>")
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('https://social.example.com/submissions/one',html)
        self.assertIn('https://social.example.com/submissions/one',text)
        self.assertIn('one hour',subject)

    def test_digest_has_separate_sections_and_creator_outcomes_have_direct_links(self):
        payload={"items":[templates.item_payload(item()),templates.item_payload(item(kind="Script",resubmitted=True))]}
        _,html,text=templates.render("DIGEST",payload,"Reviewer")
        self.assertIn('New submissions (1)',html);self.assertIn('Updated and resubmitted (1)',html)
        self.assertIn('Script',text)
        rows=[{"user_id":"creator","event_key":"event:1","event_kind":"REVIEW_DECISION","detail":{"decision":"REQUEST_CHANGES","publish_after_changes":True,"notes":"Update the opening"},"message":"", "title":"Post", "reference_code":"SUB-1","item_kind":"Post","path":"/submissions/one"}]
        message=scheduler.outcome_messages(rows,USERS)[0]
        self.assertEqual(message["payload"]["label"],"Make changes, then publish")
        _,html,text=templates.render(message["kind"],message["payload"],"Creator")
        self.assertIn('Update the opening',text);self.assertIn('https://social.example.com/submissions/one',html)

    def test_supplied_script_deadline_wins_and_linked_script_is_not_a_video_review(self):
        row={"id":"script","source":"document","item_kind":"Script","title":"Script","created_by":"creator","status":"IN_REVIEW","sent_at":NOW-timedelta(hours=20),"sent_event_id":3,"details":{"approval_needed_by":(NOW+timedelta(hours=1)).isoformat()},"reviewer_ids":["reviewer"]}
        converted=policy.item_from_row(row)
        self.assertEqual(converted.due_at,NOW+timedelta(hours=1))
        self.assertEqual(converted.path,"/script-docs/script")
        self.assertEqual(converted.stage,"REVIEW")

    def test_live_script_changes_use_same_revision_window_as_posts(self):
        row={"id":"script","source":"document","item_kind":"Script","title":"Script","created_by":"creator","status":"CHANGES_REQUESTED","decided_at":NOW,"details":{}}
        self.assertEqual(policy.item_from_row(row).due_at, NOW+timedelta(hours=policy.deadlines.REVISION_HOURS))

    def test_creator_script_outcomes_include_the_action_and_changes_deadline(self):
        base={"user_id":"creator","event_key":"outcome:doc:1","event_kind":"CHANGES_REQUESTED","detail":{"note":"Shorten the opening"},"message":"", "title":"Script", "reference_code":"Script","item_kind":"Script","path":"/script-docs/one","entity_id":"one"}
        changed=scheduler.outcome_messages([base],USERS,[item(stage="REVISION",kind="Script")])[0]
        self.assertEqual(changed["payload"]["label"],"Changes requested")
        self.assertEqual(changed["payload"]["changes_due_at"],item(stage="REVISION").due_at.isoformat())
        approved=scheduler.outcome_messages([base | {"event_kind":"APPROVED","detail":{}}],USERS)[0]
        self.assertEqual(approved["payload"]["label"],"Script approved - ready to film")
        self.assertEqual(approved["payload"]["path"],"/script-docs/one")
        edited=scheduler.outcome_messages([base | {"event_kind":"SCRIPT_EDITED"}],USERS)[0]
        self.assertEqual(edited["payload"]["label"],"Your reviewer updated your script")

    def test_invalid_external_or_missing_portal_url_is_rejected(self):
        with self.assertRaises(ValueError):config.portal_url('//outside.example.com')
        with patch.dict("os.environ",{"FRONTEND_URL":""}):
            with self.assertRaises(ValueError):config.portal_url('/submissions/one')


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_marks_sent_only_after_provider_accepts(self):
        job={"id":1,"kind":"OUTCOME","user_id":"creator","payload":{}}
        with patch.object(outbox,"current_payload",return_value={}),patch.object(templates,"render",return_value=("subject","html","text")),patch.object(outbox,"execute",new=AsyncMock()) as execute,patch.object(outbox.email_service,"send_email",new=AsyncMock(return_value=True)) as send:
            self.assertTrue(await outbox._deliver(job,{},USERS,asyncio.Semaphore(1)))
        send.assert_awaited_once_with('c@example.com','subject','html','text')
        self.assertIn("status = 'SENT'",execute.await_args.args[0])

    async def test_provider_failure_is_retried_not_marked_sent(self):
        job={"id":1,"kind":"OUTCOME","user_id":"creator","payload":{}}
        with patch.object(outbox,"current_payload",return_value={}),patch.object(templates,"render",return_value=("subject","html","text")),patch.object(outbox,"execute",new=AsyncMock()) as execute,patch.object(outbox.email_service,"send_email",new=AsyncMock(return_value=False)):
            self.assertFalse(await outbox._deliver(job,{},USERS,asyncio.Semaphore(1)))
        self.assertIn('available_at',execute.await_args.args[0])
        self.assertNotIn("status = 'SENT'",execute.await_args.args[0])

    async def test_stale_reminder_never_calls_email_provider(self):
        job={"id":1,"kind":"REMINDER","user_id":"reviewer","payload":{"items":[],"stage":"REVIEW"}}
        with patch.object(outbox,"execute",new=AsyncMock()) as execute,patch.object(outbox.email_service,"send_email",new=AsyncMock()) as send:
            self.assertFalse(await outbox._deliver(job,{},USERS,asyncio.Semaphore(1)))
        send.assert_not_awaited();self.assertIn('CANCELLED',execute.await_args.args[0])

    async def test_disabled_or_unconfigured_scheduler_never_queries_or_sends(self):
        with patch.object(config,"configured",return_value=False),patch.object(scheduler,"get_pool") as pool:
            self.assertEqual(await scheduler.run_once(NOW),0)
        pool.assert_not_called()


if __name__ == '__main__':unittest.main()
