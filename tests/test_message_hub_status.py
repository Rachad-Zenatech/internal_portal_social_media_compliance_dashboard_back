"""The message-hub dropdown asks this service for /api/message-hub/status (ported from the administration backend)."""

import asyncio
import unittest

from services.notifications.rabbitmq_config import DEFAULT_SERVICE_KEY, HUB_APPLICATIONS
from services.notifications.rabbitmq_service import RabbitMQManager


class HubStatusTests(unittest.TestCase):
    def test_this_service_is_the_compliance_app(self):
        self.assertEqual(DEFAULT_SERVICE_KEY, "social_media_compliance")
        self.assertEqual(RabbitMQManager().service_key, "social_media_compliance")
        self.assertIn("social_media_compliance", HUB_APPLICATIONS)

    def test_offline_status_lists_every_hub_server_and_marks_itself(self):
        m = RabbitMQManager(amqp_url="amqp://compliance_service:secret@127.0.0.1:1/")

        async def no_broker(*a, **k):  # never reach a real broker: one may be running on this machine
            return False

        m.connect = no_broker
        out = asyncio.run(m.get_hub_live_status())
        self.assertEqual(out["status"], "offline")
        self.assertFalse(out["connected"])
        servers = {s["id"]: s for s in out["servers"]}
        self.assertEqual(set(servers), set(HUB_APPLICATIONS))
        self.assertTrue(servers["social_media_compliance"]["is_self"])
        self.assertFalse(servers["social_media_compliance"]["online"])
        self.assertNotIn("secret", str(out))  # the password is never returned

    def test_topology_keeps_the_social_media_queues(self):
        import inspect

        src = inspect.getsource(RabbitMQManager._setup_topology)
        for name in ("social_media.commands", "social_media.commands.dlq", "ceo.social_media.events"):
            self.assertIn(name, src)
        self.assertTrue(hasattr(RabbitMQManager, "publish_social_media_event"))

    def test_routes_exist(self):
        from starlette.routing import Match

        import server

        # Ask the router itself: newer FastAPI wraps included routers (no `.path` on app.routes entries).
        def routed(method, path):
            scope = {"type": "http", "method": method, "path": path, "root_path": ""}
            return any(r.matches(scope)[0] == Match.FULL for r in server.app.routes)

        for method, p in (
            ("GET", "/api/message-hub/status"),
            ("POST", "/api/message-hub/test"),
            ("GET", "/api/message-hub/stream"),
        ):
            self.assertTrue(routed(method, p), f"{method} {p} is not routed")
        self.assertFalse(routed("GET", "/api/message-hub/nope"))


if __name__ == "__main__":
    unittest.main()
