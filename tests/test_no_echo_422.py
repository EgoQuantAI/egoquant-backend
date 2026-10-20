"""A 422 never repeats what the client sent: a fake secret posted to /portfolio or /chat must not come back."""
import unittest

from fastapi.testclient import TestClient

from main import app

SECRET = "FAKESECRETTOKEN0123456789"


class NoEchoTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def refused(self, path: str, body: dict):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(SECRET, response.text)
        for error in response.json()["detail"]:
            self.assertEqual(set(error), {"type", "loc", "msg"})     # no `input`, no `ctx`
        return response

    def test_portfolio_bad_query_id_does_not_return_the_token(self):
        self.refused("/portfolio", {"ibkr_flex": {"token": SECRET, "query_id": "not-a-number"}})

    def test_portfolio_wrong_token_shape_does_not_return_the_token(self):
        self.refused("/portfolio", {"ibkr_flex": {"token": SECRET + "!!", "query_id": "123"}})
        self.refused("/portfolio", {"ibkr_flex": {"token": {"x": SECRET}, "query_id": "123"}})

    def test_portfolio_unknown_field_value_is_not_returned(self):
        self.refused("/portfolio", {"ibkr_flex": {"token": SECRET, "query_id": "1", "extra": SECRET}, "zzz": SECRET})

    def test_portfolio_wrong_type_report_is_not_returned(self):
        self.refused("/portfolio", {"ibkr_report": [SECRET], "horizon_years": SECRET})

    def test_chat_bad_fields_do_not_return_the_input(self):
        self.refused("/chat", {"messages": [{"role": SECRET, "content": SECRET}]})
        self.refused("/chat", {"messages": [{"role": "user", "content": "hi"}], "portfolio_context": SECRET * 6000})
        self.refused("/chat", {"messages": [{"role": "user", "content": "hi"}], "page": SECRET, "extra": SECRET})

    def test_chat_value_error_does_not_return_the_input(self):
        self.refused("/chat", {"messages": [{"role": "user", "content": "   "}, {"role": "assistant", "content": SECRET}]})

    def test_malformed_json_is_not_returned(self):
        response = self.client.post("/portfolio", content=f'{{"token": "{SECRET}"', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(SECRET, response.text)

    def test_the_field_and_reason_are_still_there_for_the_frontend(self):
        error = self.refused("/portfolio", {"ibkr_flex": {"token": SECRET, "query_id": "x"}}).json()["detail"][0]
        self.assertIsInstance(error["loc"], list)
        self.assertTrue(error["msg"])


if __name__ == "__main__":
    unittest.main()
