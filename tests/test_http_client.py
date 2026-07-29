import os
import unittest
from unittest.mock import patch

import requests

import http_client
from http_client import build_http_session, build_retry_policy, get_http_session


class HttpClientTests(unittest.TestCase):
    def tearDown(self):
        http_client._GLOBAL_SESSION = None

    def test_retry_policy_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            retry = build_retry_policy()
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 0.5)
        self.assertIn(429, retry.status_forcelist)
        self.assertIn("GET", retry.allowed_methods)
        self.assertNotIn("POST", retry.allowed_methods)

    def test_retry_policy_honors_environment(self):
        with patch.dict(
            os.environ,
            {"HTTP_RETRY_TOTAL": "5", "HTTP_RETRY_BACKOFF": "1.25"},
            clear=True,
        ):
            retry = build_retry_policy()
        self.assertEqual(retry.total, 5)
        self.assertEqual(retry.backoff_factor, 1.25)

    def test_session_mounts_retry_adapters(self):
        with patch.dict(os.environ, {}, clear=True):
            session = build_http_session()
        self.assertEqual(session.get_adapter("https://").max_retries.total, 3)
        self.assertEqual(session.get_adapter("http://").max_retries.total, 3)

    def test_global_install_is_idempotent(self):
        with patch.dict(os.environ, {}, clear=True):
            first = http_client.install_global_http_session()
            second = http_client.install_global_http_session()
        self.assertIs(first, second)
        self.assertEqual(requests.get.__self__, first)

    def test_shared_session_does_not_require_global_install(self):
        with patch.dict(os.environ, {}, clear=True):
            first = get_http_session()
            second = get_http_session()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
