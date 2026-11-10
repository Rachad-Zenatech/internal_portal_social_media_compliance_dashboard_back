"""Teams direct messages: checking what Teams sends, saving chats, and delivering notifications. No network, no database:
Teams, the token service and the database are stood in for."""

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.notifications.notification_model import NotificationResponse
from services.notifications import teams_service as ts
from tools.notifications import teams_router

APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
SERVICE = "https://smba.trafficmanager.net/amer"
ENV = {"TEAMS_BOT_APP_ID": APP_ID, "TEAMS_BOT_APP_PASSWORD": "secret", "TEAMS_BOT_TENANT_ID": TENANT}

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(**over):
    claims = {"iss": ts.BOT_ISSUER, "aud": APP_ID, "exp": int(time.time()) + 600, "serviceurl": SERVICE + "/"}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def activity(kind="message", text="hello", conversation_type="personal", tenant=TENANT, service=SERVICE + "/", **extra):
    return {
        "type": kind, "text": text, "serviceUrl": service,
        "from": {"id": "29:user", "aadObjectId": "oid-1"}, "recipient": {"id": "28:bot"},
        "conversation": {"id": "a:conv1", "conversationType": conversation_type, "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}}, **extra,
    }


def notification(link="/submissions/abc"):
    return NotificationResponse(
        id=1, user_id=str(uuid4()), type="SUBMISSION", title="Sent for review", message="Your post is with a reviewer.",
        link_url=link, created_at=datetime.now(timezone.utc),
    )


class ServiceUrlTests(unittest.TestCase):
    def test_microsoft_addresses_pass(self):
        self.assertEqual(ts.safe_service_url("https://smba.trafficmanager.net/amer/"), "https://smba.trafficmanager.net/amer")
        self.assertTrue(ts.safe_service_url("https://smba.infra.gcc.teams.microsoft.com/teams"))

    def test_anything_else_is_refused(self):
        for url in ("http://smba.trafficmanager.net/amer/", "https://evil.example/", "https://smba.trafficmanager.net.evil.com/",
                    "https://user:pw@smba.trafficmanager.net/", "https://127.0.0.1/", "", None, "not a url"):
            with self.subTest(url=url):
                self.assertIsNone(ts.safe_service_url(url))


class VerifyInboundTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, header, service=SERVICE + "/"):
        with patch.dict("os.environ", ENV), patch.object(ts, "_signing_key", return_value=KEY.public_key()):
            return await ts.verify_inbound(header, service)

    async def test_a_valid_token_passes(self):
        self.assertTrue(await self.verify(f"Bearer {token()}"))

    async def test_no_header_or_wrong_scheme_fails(self):
        self.assertFalse(await self.verify(None))
        self.assertFalse(await self.verify("Basic abc"))

    async def test_wrong_audience_issuer_or_expiry_fails(self):
        self.assertFalse(await self.verify(f"Bearer {token(aud='someone-else')}"))
        self.assertFalse(await self.verify(f"Bearer {token(iss='https://evil.example')}"))
        self.assertFalse(await self.verify(f"Bearer {token(exp=int(time.time()) - 10)}"))

    async def test_a_token_signed_with_another_key_fails(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode({"iss": ts.BOT_ISSUER, "aud": APP_ID, "exp": int(time.time()) + 600}, other, algorithm="RS256")
        self.assertFalse(await self.verify(f"Bearer {forged}"))

    async def test_an_address_that_isnt_the_tokens_fails(self):
        self.assertFalse(await self.verify(f"Bearer {token()}", service="https://smba.trafficmanager.net/emea/"))


class CardTests(unittest.TestCase):
    def test_a_relative_link_becomes_a_button_to_the_portal(self):
        with patch.dict("os.environ", {"FRONTEND_URL": "https://portal.example.com/"}):
            c = ts.card("Title", "Message", "/submissions/abc")
        self.assertEqual(c["actions"][0]["url"], "https://portal.example.com/submissions/abc")
        self.assertEqual(c["type"], "AdaptiveCard")

    def test_an_absolute_link_is_kept_and_no_link_means_no_button(self):
        self.assertEqual(ts.card("T", "M", "https://x.example/a")["actions"][0]["url"], "https://x.example/a")
        self.assertNotIn("actions", ts.card("T", "M", None))

    def test_long_text_is_cut(self):
        c = ts.card("t" * 500, "m" * 5000, None)
        self.assertEqual((len(c["body"][0]["text"]), len(c["body"][1]["text"])), (ts.MAX_TITLE, ts.MAX_MESSAGE))


class HandleActivityTests(unittest.IsolatedAsyncioTestCase):
    def patched(self, linked=True):
        return (
            patch.dict("os.environ", ENV),
            patch.object(ts, "_member_email", new=AsyncMock(return_value="sam@zenatech.com")),
            patch.object(ts, "_save_conversation", new=AsyncMock(return_value=linked)),
            patch.object(ts, "_set_enabled", new=AsyncMock()),
            patch.object(ts, "_say", new=AsyncMock()),
        )

    async def run_with(self, act, linked=True):
        env, email, save, enable, say = self.patched(linked)
        with env, email, save as s, enable as e, say as sa:
            await ts.handle_activity(act)
        return s, e, sa

    async def test_installing_the_bot_saves_the_chat_and_welcomes(self):
        act = activity("conversationUpdate", membersAdded=[{"id": "29:user", "aadObjectId": "oid-1"}, {"id": "28:bot"}])
        save, _, say = await self.run_with(act)
        save.assert_awaited_once_with("oid-1", "a:conv1", SERVICE, TENANT, "sam@zenatech.com")
        self.assertEqual(say.await_args.args[2], ts.WELCOME)

    async def test_a_person_who_has_no_portal_account_is_told_so(self):
        _, _, say = await self.run_with(activity("message"), linked=False)
        self.assertEqual(say.await_args.args[2], ts.UNKNOWN_USER)

    async def test_pause_and_resume(self):
        _, enable, say = await self.run_with(activity("message", text="<at>Portal</at> Pause."))
        enable.assert_awaited_once_with("oid-1", False)
        self.assertIn("paused", say.await_args.args[2])
        _, enable, say = await self.run_with(activity("message", text="resume"))
        enable.assert_awaited_once_with("oid-1", True)

    async def test_another_tenant_or_a_non_microsoft_address_is_ignored(self):
        for act in (activity(tenant="someone-elses-tenant"), activity(service="https://evil.example/")):
            save, enable, say = await self.run_with(act)
            save.assert_not_awaited()
            say.assert_not_awaited()

    async def test_group_chats_are_not_saved(self):
        save, _, say = await self.run_with(activity(conversation_type="groupChat"))
        save.assert_not_awaited()
        self.assertIn("direct messages", say.await_args.args[2])

    async def test_the_bot_being_added_to_a_chat_alone_does_nothing(self):
        save, _, say = await self.run_with(activity("conversationUpdate", membersAdded=[{"id": "28:bot"}]))
        save.assert_not_awaited()

    async def test_an_unknown_kind_of_activity_does_nothing(self):
        save, _, say = await self.run_with(activity("typing"))
        save.assert_not_awaited()
        say.assert_not_awaited()


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    ROW = {"id": 7, "conversation_id": "a:conv1", "service_url": SERVICE}

    async def notify(self, row=ROW, response=None, enabled=True):
        n = notification()
        post = AsyncMock(return_value=response or httpx.Response(201))
        with patch.dict("os.environ", ENV if enabled else {"TEAMS_BOT_APP_ID": ""}), \
                patch.object(ts, "fetch_one", new=AsyncMock(return_value=row)), \
                patch.object(ts, "execute", new=AsyncMock()) as db, \
                patch.object(ts, "_post_card", new=post):
            sent = await ts.notify_user(n)
        return sent, post, db

    async def test_a_notification_is_posted_to_the_users_chat(self):
        sent, post, db = await self.notify()
        self.assertTrue(sent)
        self.assertEqual(post.await_args.args[:2], (SERVICE, "a:conv1"))
        self.assertIn("last_sent_at", db.await_args.args[0])

    async def test_nobody_to_send_to(self):
        sent, post, _ = await self.notify(row=None)
        self.assertFalse(sent)
        post.assert_not_awaited()

    async def test_off_when_the_bot_isnt_set_up(self):
        sent, post, _ = await self.notify(enabled=False)
        self.assertFalse(sent)
        post.assert_not_awaited()

    async def test_a_removed_bot_stops_further_tries(self):
        sent, _, db = await self.notify(response=httpx.Response(403, text="bot removed"))
        self.assertFalse(sent)
        self.assertIn("active = FALSE", db.await_args.args[0])

    async def test_a_server_error_is_not_fatal_and_keeps_the_chat(self):
        sent, _, db = await self.notify(response=httpx.Response(500, text="oops"))
        self.assertFalse(sent)
        db.assert_not_awaited()

    async def test_a_stored_address_that_isnt_microsoft_is_never_used(self):
        sent, post, db = await self.notify(row={**self.ROW, "service_url": "https://evil.example"})
        self.assertFalse(sent)
        post.assert_not_awaited()
        self.assertIn("active = FALSE", db.await_args.args[0])

    async def test_it_never_raises(self):
        n = notification()
        with patch.dict("os.environ", ENV), patch.object(ts, "fetch_one", new=AsyncMock(side_effect=RuntimeError("db down"))):
            self.assertFalse(await ts.notify_user(n))


class CallTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        real = httpx.AsyncClient

        def make(*a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real(*a, **kw)

        return patch.object(ts.httpx, "AsyncClient", new=make)

    async def test_a_429_is_retried_once_after_the_wait(self):
        seen = []

        def handler(request):
            seen.append(request.headers["authorization"])
            return httpx.Response(429, headers={"retry-after": "0"}) if len(seen) == 1 else httpx.Response(201)

        with self.client(handler), patch.object(ts, "_bot_token", new=AsyncMock(return_value="tok")):
            r = await ts._call("POST", SERVICE + "/v3/conversations/a:c/activities", json={})
        self.assertEqual((r.status_code, len(seen)), (201, 2))

    async def test_a_401_gets_a_fresh_token(self):
        tokens = iter(["old", "new"])
        used = []

        def handler(request):
            used.append(request.headers["authorization"])
            return httpx.Response(401) if len(used) == 1 else httpx.Response(200)

        async def get(force=False):
            return next(tokens)

        with self.client(handler), patch.object(ts, "_bot_token", new=get):
            r = await ts._call("GET", SERVICE + "/v3/x")
        self.assertEqual((r.status_code, used), (200, ["Bearer old", "Bearer new"]))

    async def test_the_conversation_id_is_kept_readable_in_the_path(self):
        self.assertEqual(ts._conversation_path(SERVICE, "19:abc@thread.v2"), SERVICE + "/v3/conversations/19:abc@thread.v2")
        self.assertIn("%2F", ts._conversation_path(SERVICE, "a/b"))


class RouterTests(unittest.TestCase):
    def client(self):
        app = FastAPI()
        app.include_router(teams_router.teams_bot_router, prefix="/api")
        return TestClient(app)

    def test_unset_bot_answers_503(self):
        with patch.dict("os.environ", {"TEAMS_BOT_APP_ID": ""}):
            self.assertEqual(self.client().post("/api/teams/messages", json=activity()).status_code, 503)

    def test_an_unsigned_request_is_refused(self):
        with patch.dict("os.environ", ENV), patch.object(ts, "verify_inbound", new=AsyncMock(return_value=False)), \
                patch.object(ts, "handle_activity", new=AsyncMock()) as handle:
            self.assertEqual(self.client().post("/api/teams/messages", json=activity()).status_code, 401)
        handle.assert_not_awaited()

    def test_a_good_request_is_handled(self):
        with patch.dict("os.environ", ENV), patch.object(ts, "verify_inbound", new=AsyncMock(return_value=True)), \
                patch.object(ts, "handle_activity", new=AsyncMock()) as handle:
            r = self.client().post("/api/teams/messages", json=activity())
        self.assertEqual(r.status_code, 200)
        handle.assert_awaited_once()

    def test_a_bug_in_handling_doesnt_make_teams_retry(self):
        with patch.dict("os.environ", ENV), patch.object(ts, "verify_inbound", new=AsyncMock(return_value=True)), \
                patch.object(ts, "handle_activity", new=AsyncMock(side_effect=RuntimeError("bug"))):
            self.assertEqual(self.client().post("/api/teams/messages", json=activity()).status_code, 200)

    def test_garbage_is_a_400(self):
        with patch.dict("os.environ", ENV):
            c = self.client()
            self.assertEqual(c.post("/api/teams/messages", content=b"nope").status_code, 400)
            self.assertEqual(c.post("/api/teams/messages", json=["a list"]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
