import unittest
import json
from decimal import Decimal
from io import BytesIO
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


if __name__ == "__main__": unittest.main()
