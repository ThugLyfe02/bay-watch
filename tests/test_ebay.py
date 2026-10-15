import unittest
from decimal import Decimal
from urllib.error import URLError
from unittest.mock import patch

from auction_scout.ebay import EbayClient, EbayError


class EbayClientTests(unittest.TestCase):
    def test_proxy_bid_is_never_retried_after_ambiguous_network_error(self):
        client = EbayClient(token="test-token", env="sandbox")
        with patch("auction_scout.ebay.urlopen", side_effect=URLError("timeout")) as request:
            with self.assertRaises(EbayError):
                client.place_proxy_bid("v1|123456|0", Decimal("99.00"), "USD")
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.args[0]
        self.assertIn("v1%7C123456%7C0", sent.full_url)
        self.assertNotIn("test-token", sent.full_url)


if __name__ == "__main__": unittest.main()
