"""Exercise the real worker loop without network calls or real user files."""
import contextlib
import datetime as dt
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import twodrive as t


class Finished(BaseException):
    pass


class Clock:
    def __init__(self):
        self.now = 100.0
        self.tick = lambda: None

    def sleep(self, seconds):
        self.now += seconds
        self.tick()
        if self.now > 2000:
            raise AssertionError('Worker did not stop')


class RetryTests(unittest.TestCase):
    @contextlib.contextmanager
    def sandbox(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            private = root / 'runtime/private'
            private.mkdir(parents=True)
            local = root / 'local'
            local.mkdir()
            (local / t.SENTINEL).touch()
            (root / 'filters.txt').write_text('test filter')
            cfg = dict(local_path=str(local), remote_path='test:115Drive',
                       debounce_seconds=3, local_scan_seconds=1, cloud_poll_seconds=15,
                       local_watch_enabled=False)
            t.write_json(root / 'config.json', cfg)
            t.write_json(private / 'initialized.json', dict(local=str(local),
                         remote=cfg['remote_path'], filter_sha256=hashlib.sha256(
                             (root / 'filters.txt').read_bytes()).hexdigest()))
            clock = Clock()
            for name, value in [('ROOT', root), ('PRIVATE', private), ('CONFIG', root / 'config.json')]:
                stack.enter_context(patch.object(t, name, value))
            stack.enter_context(patch.object(t.time, 'monotonic', side_effect=lambda: clock.now))
            stack.enter_context(patch.object(t.time, 'sleep', side_effect=clock.sleep))
            stack.enter_context(patch.object(t, 'ensure_server'))
            stack.enter_context(patch.object(t, 'incremental_scanner', return_value=None))
            yield root, private, local, clock

    def test_retry_backoff_deadline_and_cap(self):
        with self.sandbox() as (root, private, local, clock):
            for attempt, delay in enumerate([30, 60, 120, 240, 300, 300], 1):
                start = clock.now
                wall = dt.datetime.now().astimezone()
                t.wait_for_retry(RuntimeError('Network unavailable'), attempt)
                state = t.read_json(root / 'runtime/status.json')
                self.assertEqual(clock.now - start, delay)
                self.assertEqual(state['state'], 'retrying')
                self.assertEqual(state['retry_attempt'], attempt)
                self.assertEqual(state['retry_seconds'], delay)
                self.assertAlmostEqual((dt.datetime.fromisoformat(state['retry_at']) - wall).total_seconds(), delay, delta=1)

    def test_retry_interrupt_signals_return_without_consuming_request(self):
        for signal in ['runtime/stop', 'runtime/sync-now', 'runtime/private/folder-change-active.json']:
            with self.subTest(signal=signal), self.sandbox() as (root, private, local, clock):
                clock.tick = lambda: (root / signal).touch() if clock.now >= 103 else None
                t.wait_for_retry(RuntimeError('temporary'), 1)
                self.assertEqual(clock.now, 103)
                self.assertTrue((root / signal).exists())

    def test_worker_automatically_recovers_without_losing_pending_changes(self):
        with self.sandbox() as (root, private, local, clock):
            (local / 'pending.txt').write_text('upload after retry')
            calls = []

            def sync(cfg, **kwargs):
                calls.append((clock.now, kwargs))
                if len(calls) == 1:
                    raise OSError('Network unavailable')
                self.assertFalse((private / 'local-snapshot.json').exists())
                t.status('idle', 'recovered', last_success='verified')

            def tick():
                if len(calls) == 2:
                    raise Finished()

            clock.tick = tick
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(Finished):
                t.worker_loop()
            self.assertEqual([row[0] for row in calls], [100, 130])
            self.assertIn('pending.txt', calls[1][1]['changed_paths'])
            self.assertTrue(calls[1][1]['interruptible'])
            self.assertNotIn('initial', calls[1][1])
            state = t.read_json(root / 'runtime/status.json')
            self.assertEqual(state['state'], 'idle')
            self.assertNotIn('retry_at', state)
            self.assertNotIn('retry_seconds', state)
            self.assertIn('pending.txt', t.read_json(private / 'local-snapshot.json')['entries'])

    def test_manual_sync_skips_remaining_countdown(self):
        with self.sandbox() as (root, private, local, clock):
            calls = []

            def sync(*args, **kwargs):
                calls.append(clock.now)
                if len(calls) == 1:
                    raise OSError('temporary')
                raise Finished()

            clock.tick = lambda: (root / 'runtime/sync-now').touch() if clock.now >= 104 else None
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(Finished):
                t.worker_loop()
            self.assertEqual(calls, [100, 104])
            self.assertFalse((root / 'runtime/sync-now').exists())

    def test_manual_pause_blocks_retry_until_explicit_resume(self):
        with self.sandbox() as (root, private, local, clock):
            calls = []

            def sync(*args, **kwargs):
                calls.append(clock.now)
                if len(calls) == 1:
                    raise OSError('temporary')
                raise Finished()

            def tick():
                if clock.now == 104:
                    (root / 'runtime/stop').touch()
                if clock.now == 150:
                    self.assertEqual(calls, [100])
                    state = t.read_json(root / 'runtime/status.json')
                    self.assertEqual(state['state'], 'paused')
                    self.assertNotIn('retry_at', state)
                    (root / 'runtime/stop').unlink()

            clock.tick = tick
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(Finished):
                t.worker_loop()
            self.assertEqual(calls, [100, 150])

    def test_folder_change_exits_worker_during_countdown(self):
        with self.sandbox() as (root, private, local, clock):
            clock.tick = lambda: t.folder_guard().touch() if clock.now >= 102 else None
            with patch.object(t, 'sync_once', side_effect=OSError('temporary')) as sync:
                t.worker_loop()
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(clock.now, 102)

    def test_retries_do_not_bypass_missing_health_marker(self):
        with self.sandbox() as (root, private, local, clock):
            (local / t.SENTINEL).unlink()

            def tick():
                if clock.now >= 131:
                    raise Finished()

            clock.tick = tick
            with patch.object(t, 'sync_once') as sync, self.assertRaises(Finished):
                t.worker_loop()
            sync.assert_not_called()
            state = t.read_json(root / 'runtime/status.json')
            self.assertEqual(state['state'], 'error')
            self.assertTrue(state['requires_action'])
            self.assertNotIn('retry_attempt', state)
            self.assertIn('健康标记', state['message'])

    def test_success_resets_failure_backoff(self):
        with self.sandbox() as (root, private, local, clock):
            calls = []

            def sync(*args, **kwargs):
                calls.append(clock.now)
                if len(calls) in (1, 3):
                    raise OSError('temporary')
                if len(calls) == 4:
                    raise Finished()
                t.status('idle', 'recovered')

            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(Finished):
                t.worker_loop()
            self.assertEqual(calls, [100, 130, 145, 175])
            self.assertEqual(t.read_json(root / 'runtime/status.json')['retry_attempt'], 1)


if __name__ == '__main__':
    unittest.main()
