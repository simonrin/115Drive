import ctypes
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import twodrive as t
from local_watch import LocalChangeMonitor, WindowsDirectoryEvents
from tests import test_worker_retry as worker_tests


class Events:
    def __init__(self):
        self.hint = None
        self.closed = False

    def poll(self):
        hint, self.hint = self.hint, None
        if isinstance(hint, Exception):
            raise hint
        return hint

    def close(self):
        self.closed = True


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 0
        self.events = Events()
        self.monitor = LocalChangeMonitor(self.root, t.snapshot, clock=lambda: self.now,
                                          watcher_factory=lambda root: self.events)
        self.addCleanup(self.monitor.close)

    def test_idle_reuses_snapshot_until_periodic_scan_and_catches_missed_event(self):
        first = self.monitor.poll()
        (self.root/'missed.txt').write_text('missed')
        for self.now in range(1, 60):
            self.assertIs(self.monitor.poll(), first)
        self.now = 60
        self.assertIn('missed.txt', self.monitor.poll())
        self.assertEqual(self.monitor.metrics['scans'], 2)
        self.assertEqual(self.monitor.metrics['last_scan_reason'], 'periodic')

    def test_events_and_overflow_always_reconcile_full_snapshot(self):
        self.monitor.poll()
        nested = self.root/'dir'
        nested.mkdir()
        (nested/'file').write_text('one')
        self.events.hint = 'event'
        self.assertIn(str(Path('dir/file')), self.monitor.poll())
        (nested/'file').rename(nested/'renamed')
        self.events.hint = 'overflow'
        current = self.monitor.poll()
        self.assertNotIn(str(Path('dir/file')), current)
        self.assertIn(str(Path('dir/renamed')), current)
        self.assertEqual(self.monitor.metrics['overflows'], 1)

    def test_read_failure_polls_then_restarts_and_scans_gap(self):
        self.monitor.poll()
        old = self.events
        old.hint = OSError('read failed')
        self.monitor.poll()
        self.assertTrue(old.closed)
        self.assertEqual(self.monitor.metrics['mode'], 'polling')
        (self.root/'during-failure').touch()
        self.now = 2
        self.assertIn('during-failure', self.monitor.poll())
        self.events = Events()
        (self.root/'during-restart').touch()
        self.now = 30
        self.assertIn('during-restart', self.monitor.poll())
        self.assertEqual(self.monitor.metrics['mode'], 'events')
        self.assertEqual(self.monitor.metrics['starts'], 2)

    def test_open_failure_uses_old_scan_cadence_without_retry_storm(self):
        calls = []
        def fail(root):
            calls.append(self.now)
            raise OSError('unsupported filesystem')
        self.monitor.factory = fail
        for self.now in range(31):
            self.monitor.poll()
        self.assertEqual(calls, [0, 30])
        self.assertEqual(self.monitor.metrics['scans'], 16)

    def test_failed_scan_does_not_replace_baseline_or_consume_force(self):
        first = self.monitor.poll()
        with patch.object(self.monitor, 'scan', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.monitor.poll(force=True)
        self.assertIs(self.monitor.current, first)
        self.assertEqual(self.monitor.metrics['scans'], 1)

    def test_root_replacement_rebinds_watcher(self):
        self.monitor.poll()
        first = self.events
        self.events = Events()
        self.monitor.identity = ('different volume', 'different directory')
        self.monitor.poll()
        self.assertTrue(first.closed)
        self.assertEqual(self.monitor.metrics['starts'], 2)
        self.assertEqual(self.monitor.metrics['last_scan_reason'], 'root_replaced')


@unittest.skipUnless(os.name == 'nt', 'Windows native notifications')
class NativeEventsTests(unittest.TestCase):
    def wait(self, watcher):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            hint = watcher.poll()
            if hint:
                return hint
            time.sleep(.01)
        self.fail('Native file notification missing')

    def test_recursive_create_edit_rename_delete_and_pending_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root/'nested'
            nested.mkdir()
            watcher = WindowsDirectoryEvents(root)
            try:
                path = nested/'file'
                path.write_text('one')
                self.assertEqual(self.wait(watcher), 'event')
                path.write_text('two')
                self.wait(watcher)
                path.rename(nested/'renamed')
                self.wait(watcher)
                (nested/'renamed').unlink()
                self.wait(watcher)
            finally:
                watcher.close()
            self.assertIsNone(watcher.handle)

    def test_cancelled_os_read_falls_back_and_rebinds(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = LocalChangeMonitor(directory, t.snapshot)
            try:
                monitor.poll()
                native = monitor.watcher
                self.assertTrue(native.api.CancelIoEx(native.handle, ctypes.byref(native.overlapped)))
                deadline = time.monotonic()+5
                while monitor.watcher and time.monotonic() < deadline:
                    monitor.poll()
                    time.sleep(.01)
                self.assertIsNone(monitor.watcher)
                self.assertEqual(monitor.metrics['failures'], 1)
                (Path(directory)/'pending').touch()
                monitor.next_retry = 0
                self.assertIn('pending', monitor.poll())
                self.assertEqual(monitor.metrics['starts'], 2)
            finally:
                monitor.close()

    def test_real_native_buffer_overflow_requires_full_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = WindowsDirectoryEvents(root)
            try:
                for i in range(700):
                    (root/(str(i)+'x'*180)).touch()
                hints = []
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    hint = watcher.poll()
                    if hint:
                        hints.append(hint)
                    if 'overflow' in hints:
                        break
                    time.sleep(.01)
                self.assertIn('overflow', hints)
                self.assertEqual(len(t.snapshot(root)), 700)
            finally:
                watcher.close()


class WorkerEventTests(unittest.TestCase):
    sandbox = worker_tests.RetryTests.sandbox

    def exercise(self, action):
        with self.sandbox() as (root, private, local, clock):
            cfg = t.read_json(t.CONFIG)
            cfg['local_watch_enabled'] = True
            t.write_json(t.CONFIG, cfg)
            events = Events()
            monitors = []
            def factory(*args, **kwargs):
                monitor = LocalChangeMonitor(*args, **kwargs, watcher_factory=lambda root: events)
                monitors.append(monitor)
                return monitor
            with patch.object(t, 'LocalChangeMonitor', side_effect=factory):
                action(root, private, local, clock, events, monitors)
            self.assertTrue(events.closed)

    def test_events_during_sync_are_processed_afterwards(self):
        def action(root, private, local, clock, events, monitors):
            calls = []
            def sync(cfg, **kwargs):
                calls.append(kwargs['changed_paths'])
                if len(calls) == 1:
                    (local/'concurrent').write_text('pending')
                    events.hint = 'event'
                else:
                    self.assertIn('concurrent', calls[-1])
                    raise worker_tests.Finished()
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(worker_tests.Finished):
                t.worker_loop()
            self.assertEqual(clock.now, 104)
            self.assertEqual(monitors[0].metrics['scans'], 2)
        self.exercise(action)

    def test_retry_refreshes_changes_even_without_notification(self):
        def action(root, private, local, clock, events, monitors):
            calls = []
            def sync(cfg, **kwargs):
                calls.append(kwargs['changed_paths'])
                if len(calls) == 1:
                    (local/'during-failure').touch()
                    raise OSError('network down')
                self.assertIn('during-failure', calls[-1])
                raise worker_tests.Finished()
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(worker_tests.Finished):
                t.worker_loop()
            self.assertEqual(clock.now, 130)
        self.exercise(action)

    def test_resume_forces_scan_without_events(self):
        def action(root, private, local, clock, events, monitors):
            calls = []
            def sync(cfg, **kwargs):
                calls.append(kwargs['changed_paths'])
                if len(calls) == 1:
                    (root/'runtime/stop').touch()
                    (local/'paused-edit').touch()
                else:
                    self.assertIn('paused-edit', calls[-1])
                    raise worker_tests.Finished()
            def tick():
                if clock.now == 103:
                    (root/'runtime/stop').unlink()
            clock.tick = tick
            with patch.object(t, 'sync_once', side_effect=sync), self.assertRaises(worker_tests.Finished):
                t.worker_loop()
            self.assertEqual(clock.now, 103)
        self.exercise(action)

    def test_cached_snapshot_never_bypasses_health_marker(self):
        def action(root, private, local, clock, events, monitors):
            def sync(cfg, **kwargs):
                (local/t.SENTINEL).unlink()
            clock.tick = lambda: (_ for _ in ()).throw(worker_tests.Finished()) if clock.now >= 104 else None
            with patch.object(t, 'sync_once', side_effect=sync) as run, self.assertRaises(worker_tests.Finished):
                t.worker_loop()
            self.assertEqual(run.call_count, 1)
            state = t.read_json(root/'runtime/status.json')
            self.assertTrue(state['requires_action'])
            self.assertIn('健康标记', state['message'])
        self.exercise(action)
