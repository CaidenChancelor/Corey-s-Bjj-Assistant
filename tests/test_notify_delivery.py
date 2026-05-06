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
        cls.bot.scheduler.shutdown(wait=False)
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


if __name__ == "__main__":
    unittest.main()
