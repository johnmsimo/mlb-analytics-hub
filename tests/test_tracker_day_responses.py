import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as mlb_app


class TrackerDayResponseTests(unittest.TestCase):
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
        with mlb_app._TRACKER_RESPONSE_LOCK:
            mlb_app._TRACKER_RESPONSE_CACHE.clear()

    @staticmethod
    def _write_json(path, payload):
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, separators=(',', ':'))

    @staticmethod
    def _store(marker='first', count=80):
        return {
            '2026-07-30': {
                'capturedAt': '2026-07-30T12:00:00',
                'gradedAt': None,
                'closingCapturedAt': None,
                'entries': [
                    {
                        'id': f'pick-{idx}',
                        'player': f'Batter {idx} ' + ('x' * 40),
                        'marketKey': 'batter_hits',
                        'gamePk': 1 if idx % 2 == 0 else 2,
                        'grade': 'pending',
                        'hubRating': 80 - (idx % 10),
                        'edge': 0.08,
                        'marker': marker,
                    }
                    for idx in range(count)
                ],
            },
        }

    def test_today_reuses_json_and_gzip_representation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            adjust_path = os.path.join(temp_dir, 'model_adjustments.json')
            self._write_json(tracker_path, self._store())
            self._write_json(adjust_path, {'bankroll': 1000})
            original_dumps = mlb_app.app.json.dumps
            dumps_calls = 0

            def counting_dumps(*args, **kwargs):
                nonlocal dumps_calls
                dumps_calls += 1
                return original_dumps(*args, **kwargs)

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                patch.object(mlb_app, 'ADJUST_STORE', adjust_path),
                patch.object(
                    mlb_app.app.json,
                    'dumps',
                    side_effect=counting_dumps,
                ),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    '/api/tracker/today?date=2026-07-30',
                    headers={'Accept-Encoding': 'gzip'},
                )
                second = client.get(
                    '/api/tracker/today?date=2026-07-30',
                    headers={'Accept-Encoding': 'gzip'},
                )

            self.assertEqual(dumps_calls, 1)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.headers.get('Content-Encoding'), 'gzip')
            self.assertEqual(first.data, second.data)
            self.assertEqual(first.headers.get('ETag'), second.headers.get('ETag'))

    def test_entries_etag_and_game_filters_are_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            self._write_json(tracker_path, self._store(count=6))

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    '/api/tracker/entries?date=2026-07-30&gamePk=1'
                )
                revalidated = client.get(
                    '/api/tracker/entries?date=2026-07-30&gamePk=1',
                    headers={'If-None-Match': first.headers['ETag']},
                )
                second_game = client.get(
                    '/api/tracker/entries?date=2026-07-30&gamePk=2'
                )

            self.assertEqual(first.get_json()['total'], 3)
            self.assertEqual(second_game.get_json()['total'], 3)
            self.assertEqual(revalidated.status_code, 304)
            self.assertEqual(revalidated.data, b'')
            self.assertNotEqual(
                first.headers.get('ETag'),
                second_game.headers.get('ETag'),
            )

    def test_successful_day_commit_invalidates_cached_representation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            self._write_json(tracker_path, self._store(count=1))

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    '/api/tracker/entries?date=2026-07-30'
                )
                updated_day = self._store(marker='second', count=1)[
                    '2026-07-30'
                ]
                self.assertTrue(
                    mlb_app._tracker_commit_day(
                        '2026-07-30',
                        updated_day,
                    )
                )
                second = client.get(
                    '/api/tracker/entries?date=2026-07-30'
                )

            self.assertEqual(
                first.get_json()['entries'][0]['marker'],
                'first',
            )
            self.assertEqual(
                second.get_json()['entries'][0]['marker'],
                'second',
            )
            self.assertNotEqual(
                first.headers.get('ETag'),
                second.headers.get('ETag'),
            )

    def test_atomic_adjustment_replacement_invalidates_today_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            adjust_path = os.path.join(temp_dir, 'model_adjustments.json')
            self._write_json(tracker_path, self._store(count=1))
            self._write_json(adjust_path, {'bankroll': 1000})

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                patch.object(mlb_app, 'ADJUST_STORE', adjust_path),
                mlb_app.app.test_client() as client,
            ):
                first = client.get(
                    '/api/tracker/today?date=2026-07-30'
                )
                original_stat = os.stat(adjust_path)
                replacement = f'{adjust_path}.new'
                self._write_json(replacement, {'bankroll': 2000})
                os.utime(
                    replacement,
                    ns=(
                        original_stat.st_atime_ns,
                        original_stat.st_mtime_ns,
                    ),
                )
                os.replace(replacement, adjust_path)
                second = client.get(
                    '/api/tracker/today?date=2026-07-30'
                )

            self.assertEqual(first.get_json()['settings']['bankroll'], 1000)
            self.assertEqual(second.get_json()['settings']['bankroll'], 2000)
            self.assertNotEqual(
                first.headers.get('ETag'),
                second.headers.get('ETag'),
            )

    def test_date_route_preserves_decoded_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker_path = os.path.join(temp_dir, 'daily_tracker.json')
            adjust_path = os.path.join(temp_dir, 'model_adjustments.json')
            self._write_json(tracker_path, self._store(count=1))
            self._write_json(adjust_path, {'bankroll': 1000})

            with (
                patch.object(mlb_app, 'TRACKER_STORE', tracker_path),
                patch.object(mlb_app, 'ADJUST_STORE', adjust_path),
                mlb_app.app.test_client() as client,
            ):
                response = client.get('/api/tracker/date/2026-07-30')

            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload['success'])
            self.assertEqual(payload['date'], '2026-07-30')
            self.assertEqual(payload['capturedAt'], '2026-07-30T12:00:00')
            self.assertEqual(payload['adjustments']['bankroll'], 1000)
            self.assertEqual(len(payload['entries']), 1)
            self.assertIn('summary', payload)


if __name__ == '__main__':
    unittest.main()
