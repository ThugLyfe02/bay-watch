import base64
import json
import unittest
from io import BytesIO
from urllib.parse import parse_qs
from urllib.error import HTTPError
from unittest.mock import patch

from auction_scout.errors import EbayError
from auction_scout.oauth import EbayTokenProvider


class OAuthTokenProviderTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return self.payload

    def test_application_token_is_minted_once_and_cached_in_memory(self):
        provider = EbayTokenProvider(env="sandbox", client_id="client-id", client_secret="client-secret")
        with patch("auction_scout.oauth.urlopen", return_value=self.Response({"access_token": "app-access", "expires_in": 7200})) as request:
            self.assertEqual(provider.get_application_token(), "app-access")
            self.assertEqual(provider.get_application_token(), "app-access")
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.sandbox.ebay.com/identity/v1/oauth2/token")
        auth = sent.get_header("Authorization").removeprefix("Basic ")
        self.assertEqual(base64.b64decode(auth).decode(), "client-id:client-secret")
        form = parse_qs(sent.data.decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], [EbayTokenProvider.APP_SCOPE])
        self.assertEqual(provider._tokens["application"][0], "app-access")

    def test_user_token_uses_refresh_grant_and_preserves_consent_scope_set(self):
        provider = EbayTokenProvider(env="sandbox", client_id="client-id", client_secret="client-secret",
                                     user_refresh_token="refresh-secret")
        with patch("auction_scout.oauth.urlopen", return_value=self.Response({"access_token": "user-access", "expires_in": 7200})) as request:
            self.assertEqual(provider.get_user_token(), "user-access")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.sandbox.ebay.com/identity/v1/oauth2/token")
        form = parse_qs(sent.data.decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["refresh-secret"])
        self.assertNotIn("scope", form)

    def test_oauth_error_response_does_not_leak_credentials_or_body(self):
        provider = EbayTokenProvider(env="production", client_id="client-id", client_secret="super-secret")
        error = HTTPError("https://api.ebay.com/identity", 401, "unauthorized", {},
                          BytesIO(b'{"error_description":"super-secret rejected"}'))
        with patch("auction_scout.oauth.urlopen", side_effect=error):
            with self.assertRaises(EbayError) as raised:
                provider.get_application_token()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("super-secret", str(raised.exception))
        self.assertNotIn("error_description", str(raised.exception))

    def test_static_tokens_remain_available_for_short_manual_checks(self):
        provider = EbayTokenProvider(env="sandbox", application_token="static-app", user_token="static-user",
                                     client_id="", client_secret="", user_refresh_token="")
        with patch("auction_scout.oauth.urlopen") as request:
            self.assertEqual(provider.get_application_token(), "static-app")
            self.assertEqual(provider.get_user_token(), "static-user")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
