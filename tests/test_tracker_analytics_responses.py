import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as mlb_app


class TrackerAnalyticsResponseTests(unittest.TestCase):
    def setUp(self):
        self._clear_caches()

    def tearDown(self):
        self._clear_caches()

    @staticmethod
    def _clear_caches():
        with mlb_app._TRACKER_READ_LOCK:
            mlb_app._TRACKER_READ_CACHE.update({
                'sig': None,
                'pickled': None,
                'day_pickles': {},
                'day_json': {},
                'date_keys': (),
            })
        with mlb_app._TRACKER_ANALYTICS_RESPONSE_LOCK:
            mlb_app._TRACKER_ANALYTICS_RESPONSE_CACHE.clear()

    @staticmethod
    def _write_json(path, payload):
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, separators=(',', ':'))

    @classmethod
    def _replace_json_preserving_mtime(cls, path, payload):
        original = os.stat(path)
        replacement = f'{path}.new'
        cls._write_json(replacement, payload)
        os.utime(
            replacement,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )
        os.replace(replacement, path)

    def test_dashboard_routes_reuse_bytes_gzip_and_etags(self):
        calls = []

        def payload(kind, date_str, window):
            calls.append((kind, date_str, window))
            return {
                'success': True,
                'kind': kind,
                'date': date_str,
                'window': window,
                'padding': 'x' * 5000,
            }

        routes = [
            ('performance', '/api/tracker/performance?date=2026-07-30&window=14'),
            ('calibration', '/api/tracker/calibration/dashboard/2026-07-30?window=14'),
            ('value', '/api/tracker/value/dashboard/2026-07-30?window=14'),
        ]
        with (
            patch.object(
                mlb_app,
                '_tracker_analytics_input_version',
                return_value=('stable',),
            ),
            patch.object(
                mlb_app,
                '_tracker_analytics_payload',
                side_effect=payload,
            ),
            mlb_app.app.test_client() as client,
        ):
            for kind, route in routes:
                identity = client.get(route)
                repeated = client.get(route)
                compressed = client.get(
                    route,
                    headers={'Accept-Encoding': 'gzip'},
                )
                revalidated = client.get(
                    route,
                    headers={'If-None-Match': identity.headers['ETag']},
                )

                self.assertEqual(identity.status_code, 200)
                self.assertEqual(repeated.data, identity.data)
                self.assertEqual(identity.get_json()['kind'], kind)
                self.assertEqual(compressed.headers.get('Content-Encoding'), 'gzip')
                self.assertNotEqual(
                    identity.headers.get('ETag'),
                    compressed.headers.get('ETag'),
                )
                self.assertEqual(revalidated.status_code, 304)
                self.assertEqual(revalidated.data, b'')

        self.assertEqual(
            calls,
            [
                ('performance', '2026-07-30', 14),
                ('calibration', '2026-07-30', 14),
                ('value', '2026-07-30', 14),
            ],
        )

    def test_date_and_window_variants_are_isolated(self):
        def payload(kind, date_str, window):
            return {
                'success': True,
                'kind': kind,
                'date': date_str,
                'window': window,
            }

        with (
            patch.object(
                mlb_app,
                '_tracker_analytics_input_version',
                return_value=('stable',),
            ),
            patch.object(
                mlb_app,
                '_tracker_analytics_payload',
                side_effect=payload,
            ) as builder,
            mlb_app.app.test_client() as client,
        ):
            seven = client.get(
                '/api/tracker/performance?date=2026-07-30&window=7'
            )
            thirty = client.get(
                '/api/tracker/performance?date=2026-07-30&window=30'
            )
            prior = client.get(
                '/api/tracker/performance?date=2026-07-29&window=7'
            )

        self.assertEqual(builder.call_count, 3)
        self.assertEqual(seven.get_json()['window'], 7)
        self.assertEqual(thirty.get_json()['window'], 30)
        self.assertEqual(prior.get_json()['date'], '2026-07-29')
        self.assertEqual(
            len({
                seven.headers['ETag'],
                thirty.headers['ETag'],
                prior.headers['ETag'],
            }),
            3,
        )

    def test_tracker_replacement_invalidates_every_analytics_kind(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            adjust_path = os.path.join(temp_dir, 'model_adjustments.json')
            history_path = os.path.join(temp_dir, 'calibration_history.json')
            self._write_json(tracker_path, {'marker': 'first'})
            self._write_json(adjust_path, {'marker': 'adjust'})
            self._write_json(history_path, [{'marker': 'history'}])

            def payload(kind, date_str, window):
                with open(tracker_path, encoding='utf-8') as handle:
                    marker = json.load(handle)['marker']
                return {
                    'success': True,
                    'kind': kind,
                    'marker': marker,
                }

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                patch.object(mlb_app, 'ADJUST_STORE', adjust_path),
                patch.object(mlb_app, 'CAL_HISTORY_STORE', history_path),
                patch.object(
                    mlb_app,
                    '_tracker_analytics_payload',
                    side_effect=payload,
                ) as builder,
                mlb_app.app.test_client() as client,
            ):
                routes = [
                    '/api/tracker/performance?date=2026-07-30&window=14',
                    '/api/tracker/calibration/dashboard/2026-07-30?window=14',
                    '/api/tracker/value/dashboard/2026-07-30?window=14',
                ]
                first = [client.get(route) for route in routes]
                original = os.stat(tracker_path)
                replacement = f'{tracker_path}.new'
                self._write_json(replacement, {'marker': 'other'})
                os.utime(
                    replacement,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
                os.replace(replacement, tracker_path)
                second = [client.get(route) for route in routes]

            self.assertEqual(builder.call_count, 6)
            self.assertTrue(all(r.get_json()['marker'] == 'first' for r in first))
            self.assertTrue(all(r.get_json()['marker'] == 'other' for r in second))
            for old, new in zip(first, second):
                self.assertNotEqual(old.headers['ETag'], new.headers['ETag'])

    def test_control_file_versions_match_endpoint_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            adjust_path = os.path.join(temp_dir, 'model_adjustments.json')
            history_path = os.path.join(temp_dir, 'calibration_history.json')
            self._write_json(tracker_path, {})
            self._write_json(adjust_path, {'marker': 'first'})
            self._write_json(history_path, [{'marker': 'first'}])

            calls = []

            def payload(kind, date_str, window):
                calls.append(kind)
                with open(adjust_path, encoding='utf-8') as handle:
                    adjustment = json.load(handle)['marker']
                with open(history_path, encoding='utf-8') as handle:
                    history = json.load(handle)[0]['marker']
                return {
                    'success': True,
                    'kind': kind,
                    'adjustment': adjustment,
                    'history': history,
                }

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                patch.object(mlb_app, 'ADJUST_STORE', adjust_path),
                patch.object(mlb_app, 'CAL_HISTORY_STORE', history_path),
                patch.object(
                    mlb_app,
                    '_tracker_analytics_payload',
                    side_effect=payload,
                ),
                mlb_app.app.test_client() as client,
            ):
                performance_route = (
                    '/api/tracker/performance?date=2026-07-30&window=14'
                )
                calibration_route = (
                    '/api/tracker/calibration/dashboard/2026-07-30?window=14'
                )
                value_route = (
                    '/api/tracker/value/dashboard/2026-07-30?window=14'
                )
                first = {
                    'performance': client.get(performance_route),
                    'calibration': client.get(calibration_route),
                    'value': client.get(value_route),
                }

                self._replace_json_preserving_mtime(
                    adjust_path,
                    {'marker': 'second'},
                )
                after_adjustment = {
                    'performance': client.get(performance_route),
                    'calibration': client.get(calibration_route),
                    'value': client.get(value_route),
                }

                self._replace_json_preserving_mtime(
                    history_path,
                    [{'marker': 'second'}],
                )
                after_history = {
                    'performance': client.get(performance_route),
                    'calibration': client.get(calibration_route),
                    'value': client.get(value_route),
                }

            self.assertEqual(calls.count('performance'), 3)
            self.assertEqual(calls.count('calibration'), 3)
            self.assertEqual(calls.count('value'), 2)
            for kind in ('performance', 'calibration', 'value'):
                self.assertEqual(
                    after_adjustment[kind].get_json()['adjustment'],
                    'second',
                )
                self.assertNotEqual(
                    first[kind].headers['ETag'],
                    after_adjustment[kind].headers['ETag'],
                )
            for kind in ('performance', 'calibration'):
                self.assertEqual(
                    after_history[kind].get_json()['history'],
                    'second',
                )
                self.assertNotEqual(
                    after_adjustment[kind].headers['ETag'],
                    after_history[kind].headers['ETag'],
                )
            self.assertEqual(
                after_adjustment['value'].headers['ETag'],
                after_history['value'].headers['ETag'],
            )


if __name__ == '__main__':
    unittest.main()
