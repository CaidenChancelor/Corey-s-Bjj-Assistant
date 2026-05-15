import importlib
import os
import tempfile
import unittest


class NotifyDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls.tmpdir.name, "test.db")
        os.environ["API_TOKEN"] = "test-token"
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        cls.bot = importlib.import_module("bot")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.bot.scheduler.shutdown(wait=False)
        except Exception:
            pass  # Scheduler may not be running in test context
        cls.tmpdir.cleanup()

    def test_send_reports_missing_twilio_credentials(self):
        result = self.bot.send("test message")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_credentials_missing")

    def test_notify_returns_503_when_delivery_fails(self):
        client = self.bot.app.test_client()

        response = client.post(
            "/api/notify",
            headers={"Authorization": "Bearer test-token"},
            json={"message": "test message"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "twilio_credentials_missing")

    def test_twilio_message_status_requires_api_token(self):
        client = self.bot.app.test_client()

        response = client.get("/api/twilio/message/SM123")

        self.assertEqual(response.status_code, 401)

    def test_twilio_message_status_returns_message_details(self):
        class FakeMessage:
            sid = "SM123"
            status = "delivered"
            error_code = None
            error_message = None
            to = "whatsapp:+15550000000"
            from_ = "whatsapp:+14155238886"
            date_created = None
            date_sent = None

        class FakeMessageFetcher:
            def fetch(self):
                return FakeMessage()

        class FakeMessages:
            def __call__(self, sid):
                self.sid = sid
                return FakeMessageFetcher()

        class FakeClient:
            messages = FakeMessages()

        original_client = self.bot.client
        original_account_sid = self.bot.ACCOUNT_SID
        original_auth_token = self.bot.AUTH_TOKEN
        self.bot.client = FakeClient()
        self.bot.ACCOUNT_SID = "AC123"
        self.bot.AUTH_TOKEN = "token"
        self.addCleanup(setattr, self.bot, "client", original_client)
        self.addCleanup(setattr, self.bot, "ACCOUNT_SID", original_account_sid)
        self.addCleanup(setattr, self.bot, "AUTH_TOKEN", original_auth_token)
        client = self.bot.app.test_client()

        response = client.get(
            "/api/twilio/message/SM123",
            headers={"Authorization": "Bearer test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
