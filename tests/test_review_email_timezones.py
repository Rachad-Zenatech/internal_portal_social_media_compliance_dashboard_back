"""Recipient-local schedules and authenticated browser time-zone preferences."""
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models.notifications.review_email_model import EmailTimezonePreference
from services.notifications.review_email import config, policy, preferences, scheduler, outbox, templates
from tests.test_review_emails import NOW, ENV, USERS, item


class LocalScheduleTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',ENV);self.env.start();self.addCleanup(self.env.stop)
        self.users={uid:dict(user) for uid,user in USERS.items()}
        self.users['reviewer']['time_zone']='America/New_York'
        self.users['other']['time_zone']='America/Los_Angeles'
        self.work=item(reviewer_ids=['reviewer','other'])

    def test_each_reviewer_gets_their_own_nine_am(self):
        early=scheduler.scheduled_messages([self.work],self.users,NOW)
        self.assertEqual([m['user_id'] for m in early if m['kind']=='DIGEST'],['reviewer'])
        later=scheduler.scheduled_messages([self.work],self.users,NOW+timedelta(hours=3))
        self.assertIn('other',[m['user_id'] for m in later if m['kind']=='DIGEST'])

    def test_previous_day_is_recipient_local_including_half_hour_zones(self):
        work=item(sent_at=datetime(2026,11,17,19,tzinfo=timezone.utc))
        self.assertTrue(policy.in_digest(work,NOW,'America/New_York'))
        self.assertFalse(policy.in_digest(work,NOW,'Asia/Kolkata'))
        at_nine=datetime(2026,11,18,3,30,tzinfo=timezone.utc)
        self.assertIsNotNone(policy.digest_window(at_nine,'Asia/Kolkata'))
        self.assertIsNone(policy.digest_window(at_nine-timedelta(minutes=1),'Asia/Kolkata'))

    def test_per_recipient_local_date_controls_the_dedupe_key(self):
        self.users['reviewer']['time_zone']='Pacific/Auckland'
        now=datetime(2026,11,18,20,tzinfo=timezone.utc)  # Nov 19, 09:00 NZDT
        work=item(sent_at=datetime(2026,11,17,20,tzinfo=timezone.utc))
        messages=scheduler.scheduled_messages([work],self.users,now)
        digest=next(m for m in messages if m['kind']=='DIGEST' and m['user_id']=='reviewer')
        self.assertEqual(digest['key'],'digest:2026-11-19:reviewer')
        self.assertEqual(digest['payload']['date'],'2026-11-19')

    def test_daylight_saving_uses_named_zone_instead_of_fixed_offset(self):
        winter=datetime(2026,11,18,17,tzinfo=timezone.utc)
        summer=datetime(2026,7,1,16,tzinfo=timezone.utc)
        for instant in (winter,summer):
            self.assertIsNotNone(policy.digest_window(instant,'America/Los_Angeles'))
            self.assertIsNone(policy.digest_window(instant-timedelta(minutes=1),'America/Los_Angeles'))

    def test_missing_or_invalid_saved_zone_uses_configured_fallback(self):
        self.assertEqual(str(config.user_timezone({})),'America/Toronto')
        self.assertEqual(str(config.user_timezone({'time_zone':'bad-zone'})),'America/Toronto')

    def test_queued_digest_cannot_send_before_recipient_local_nine(self):
        saved=templates.item_payload(self.work)
        job={'kind':'DIGEST','user_id':'other','payload':{'date':'2026-11-18','items':[saved]}}
        self.assertIsNone(outbox.current_payload(job,{self.work.key:self.work},self.users,NOW))
        self.assertIsNotNone(outbox.current_payload(job,{self.work.key:self.work},self.users,NOW+timedelta(hours=3)))

    def test_dates_in_reminders_and_creator_outcomes_use_recipient_zone(self):
        payload={'stage':'REVIEW','items':[templates.item_payload(self.work)]}
        _,_,text=templates.render('REMINDER',payload,'Reviewer','America/Los_Angeles')
        self.assertIn('07:00 AM PST',text)
        payload={'label':'Changes requested','message':'Update the opening','title':'Script','reference':'Script','kind':'Script','path':'/script-docs/one','changes_due_at':self.work.due_at.isoformat()}
        _,_,text=templates.render('OUTCOME',payload,'Creator','America/Los_Angeles')
        self.assertIn('Changes due:',text);self.assertIn('07:00 AM PST',text)


class PreferenceTests(unittest.IsolatedAsyncioTestCase):
    def test_only_iana_timezones_are_accepted(self):
        for invalid in ('','UTC-5','not/a-zone','/etc/passwd'):
            with self.assertRaises(ValidationError):EmailTimezonePreference(time_zone=invalid)
        self.assertEqual(EmailTimezonePreference(time_zone='Asia/Kolkata').time_zone,'Asia/Kolkata')

    async def test_service_saves_the_authenticated_identity(self):
        uid='10000000-0000-0000-0000-000000000001'
        with patch.object(preferences,'fetch_one',new=AsyncMock(return_value={'time_zone':'Europe/London'})) as fetch:
            result=await preferences.save_timezone(uid,EmailTimezonePreference(time_zone='Europe/London'))
        self.assertEqual(result,{'time_zone':'Europe/London'})
        self.assertEqual(fetch.await_args.args[1:],(UUID(uid),'Europe/London'))

    def test_endpoint_cannot_override_the_user_and_rejects_invalid_zone(self):
        router=importlib.import_module('tools.notifications.notification_router')
        app=FastAPI();app.include_router(router.notification_router,prefix='/api')
        uid='10000000-0000-0000-0000-000000000001'
        app.dependency_overrides[router.get_current_user_id]=lambda:uid
        with patch.object(router,'save_timezone',new=AsyncMock(return_value={'time_zone':'Europe/London'})) as save:
            with TestClient(app) as client:
                result=client.put('/api/notifications/email-timezone',json={'time_zone':'Europe/London','user_id':'someone-else'})
                self.assertEqual(result.status_code,200)
                self.assertEqual(save.await_args.args[0],uid)
                result=client.put('/api/notifications/email-timezone',json={'time_zone':'bad-zone'})
                self.assertEqual(result.status_code,422)
                self.assertEqual(save.await_count,1)


if __name__=='__main__':unittest.main()
