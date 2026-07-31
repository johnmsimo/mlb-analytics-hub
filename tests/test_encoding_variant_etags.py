import gzip
import hashlib
import unittest
from unittest.mock import patch

import app as mlb_app


class EncodingVariantEtagTests(unittest.TestCase):
    @staticmethod
    def _json_representation():
        body = ('{"padding":"' + ('x' * 5000) + '"}\n').encode('utf-8')
        compressed = gzip.compress(body, compresslevel=5)
        return {
            'version': ('test',),
            'body': body,
            'gzip': compressed,
            'etag': hashlib.sha256(body).hexdigest(),
            'gzip_etag': hashlib.sha256(compressed).hexdigest(),
        }

    def setUp(self):
        with mlb_app._HTML_GZ_LOCK:
            mlb_app._HTML_GZ_CACHE.clear()

    def _assert_variant_revalidation(self, responder):
        with mlb_app.app.test_request_context():
            identity = responder()
        with mlb_app.app.test_request_context(
            headers={'Accept-Encoding': 'gzip'}
        ):
            compressed = responder()

        self.assertEqual(identity.status_code, 200)
        self.assertEqual(compressed.status_code, 200)
        self.assertIsNone(identity.headers.get('Content-Encoding'))
        self.assertEqual(compressed.headers.get('Content-Encoding'), 'gzip')
        self.assertNotEqual(
            identity.headers.get('ETag'),
            compressed.headers.get('ETag'),
        )

        with mlb_app.app.test_request_context(
            headers={'If-None-Match': identity.headers['ETag']}
        ):
            identity_match = responder()
        with mlb_app.app.test_request_context(
            headers={
                'Accept-Encoding': 'gzip',
                'If-None-Match': compressed.headers['ETag'],
            }
        ):
            compressed_match = responder()
        self.assertEqual(identity_match.status_code, 304)
        self.assertEqual(compressed_match.status_code, 304)
        self.assertEqual(identity_match.get_data(), b'')
        self.assertEqual(compressed_match.get_data(), b'')

        with mlb_app.app.test_request_context(
            headers={'If-None-Match': compressed.headers['ETag']}
        ):
            identity_cross = responder()
        with mlb_app.app.test_request_context(
            headers={
                'Accept-Encoding': 'gzip',
                'If-None-Match': identity.headers['ETag'],
            }
        ):
            compressed_cross = responder()
        self.assertEqual(identity_cross.status_code, 200)
        self.assertEqual(compressed_cross.status_code, 200)
        self.assertEqual(
            identity_cross.headers.get('ETag'),
            identity.headers.get('ETag'),
        )
        self.assertEqual(
            compressed_cross.headers.get('ETag'),
            compressed.headers.get('ETag'),
        )

    def test_html_page_etags_follow_selected_encoding(self):
        html = '<main>' + ('x' * 5000) + '</main>'
        self._assert_variant_revalidation(
            lambda: mlb_app._page_response(html)
        )

    def test_memory_etags_follow_selected_encoding(self):
        representation = self._json_representation()
        with patch.object(
            mlb_app,
            '_mlb_memory_latest_representation',
            return_value=representation,
        ):
            self._assert_variant_revalidation(
                lambda: mlb_app._mlb_memory_latest_response()
            )

    def test_tracker_etags_follow_selected_encoding(self):
        representation = self._json_representation()
        with patch.object(
            mlb_app,
            '_tracker_day_representation',
            return_value=representation,
        ):
            self._assert_variant_revalidation(
                lambda: mlb_app._tracker_day_response(
                    'today',
                    '2026-07-30',
                )
            )

    def test_cached_etags_hash_the_actual_response_bytes(self):
        payload = {'padding': 'x' * 5000}
        representation = mlb_app._tracker_encode_day_representation(
            payload,
            ('test',),
        )
        self.assertEqual(
            representation['etag'],
            hashlib.sha256(representation['body']).hexdigest(),
        )
        self.assertEqual(
            representation['gzip_etag'],
            hashlib.sha256(representation['gzip']).hexdigest(),
        )


if __name__ == '__main__':
    unittest.main()

