"""Edge hygiene of the API: CORS only for our site, and unplanned errors give one generic 500."""
import unittest

from fastapi.testclient import TestClient

from main import app

SITE = "https://fintechaton-frontend.vercel.app"


@app.get("/_test_boom")
def _boom():
    raise RuntimeError("secret detail FAKESECRET123 /home/user/app.py")


class HygieneTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def preflight(self, origin: str):
        return self.client.options("/portfolio", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})

    def test_our_site_may_call_the_api(self):
        response = self.preflight(SITE)
        self.assertEqual(response.headers.get("access-control-allow-origin"), SITE)
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_other_origins_get_no_cors_headers(self):
        for origin in ("https://evil.example", "http://fintechaton-frontend.vercel.app", "null"):
            response = self.preflight(origin)
            self.assertNotIn("access-control-allow-origin", response.headers, origin)
            response = self.client.get("/health", headers={"Origin": origin})
            self.assertNotIn("access-control-allow-origin", response.headers, origin)

    def test_unplanned_errors_are_one_generic_500(self):
        response = self.client.get("/_test_boom")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(response.json()), ["detail"])
        for leak in ("FAKESECRET123", "RuntimeError", "Traceback", ".py"):
            self.assertNotIn(leak, response.text)


if __name__ == "__main__":
    unittest.main()
