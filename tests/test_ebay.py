import unittest
import json
import tempfile
from types import SimpleNamespace
from contextlib import redirect_stdout
from decimal import Decimal
from io import BytesIO
from io import StringIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from auction_scout.ebay import EbayClient, EbayError


class EbayClientTests(unittest.TestCase):
    def test_browse_preflight_uses_application_token_and_encodes_exact_item_id(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"itemId":"v1|123456|0"}'

        client = EbayClient(token="buyer-token", application_token="browse-token", env="sandbox")
        with patch("auction_scout.ebay.urlopen", return_value=Response()) as request:
            result = client.get_item("v1|123456|0")
        self.assertEqual(result["itemId"], "v1|123456|0")
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer browse-token")
        self.assertIn("v1%7C123456%7C0", sent.full_url)

    def test_proxy_bid_is_never_retried_after_ambiguous_network_error(self):
        client = EbayClient(token="test-token", env="sandbox")
        with patch("auction_scout.ebay.urlopen", side_effect=URLError("timeout")) as request:
            with self.assertRaises(EbayError):
                client.place_proxy_bid("v1|123456|0", Decimal("99.00"), "USD")
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")
        self.assertIn("v1%7C123456%7C0", sent.full_url)
        self.assertNotIn("test-token", sent.full_url)

    def test_api_error_body_cannot_echo_user_token_into_logs(self):
        client = EbayClient(token="secret-user-token", env="sandbox")
        error = HTTPError("https://api.sandbox.ebay.com/bid", 400, "bad request", {},
                          BytesIO(b'{"message":"secret-user-token rejected"}'))
        with patch("auction_scout.ebay.urlopen", side_effect=error):
            with self.assertRaises(EbayError) as raised:
                client.place_proxy_bid("v1|123456|0", Decimal("99"), "USD")
        self.assertNotIn("secret-user-token", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_api_check_is_read_only_and_reports_browse_and_offer_reads(self):
        class FakeClient:
            tokens = SimpleNamespace(static_application_token="", can_mint_application_token=True,
                                     static_user_token="", can_refresh_user_token=True)
            def search_auctions(self, query, limit): return []
            def get_bidding(self, item_id): return {"itemId": item_id, "auctionStatus": "ACTIVE", "highBidder": False}
            def place_proxy_bid(self, *args, **kwargs): raise AssertionError("api-check must never submit a bid")

        output = StringIO()
        with tempfile.NamedTemporaryFile() as db, patch("auction_scout.cli.EbayClient", return_value=FakeClient()), redirect_stdout(output):
            from auction_scout.cli import main
            main(["--db", db.name, "api-check", "--query", "sandbox card", "--offer-item-id", "sandbox-item"])
        report = json.loads(output.getvalue())
        self.assertEqual(report["browse_search"], "passed")
        self.assertEqual(report["browse_item_detail"], "not_tested_no_search_result")
        self.assertEqual(report["offer_read"], "passed")
        self.assertFalse(report["bids_submitted"])

    def test_api_check_refuses_production_reads_without_explicit_flag(self):
        from auction_scout.cli import main
        with tempfile.NamedTemporaryFile() as db, patch.dict("os.environ", {"EBAY_API_ENV": "production"}), \
             patch("auction_scout.cli.EbayClient") as client:
            with self.assertRaisesRegex(ValueError, "require --live-read"):
                main(["--db", db.name, "api-check", "--query", "test"])
        client.assert_not_called()


if __name__ == "__main__": unittest.main()
