import concurrent.futures
import contextlib
import ctypes
from ctypes import wintypes
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import runtime_json as r
from sync_progress import SyncProgress
from sync_errors import TransientSyncError, recovery_policy
import twodrive as t


@contextlib.contextmanager
def deny_delete(path):
    """Real Windows reader that deliberately does not share delete access."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    mutex = threading.Lock()

    def release():
        nonlocal handle
        with mutex:
            if handle is not None:
                kernel.CloseHandle(handle)
                handle = None

    try:
        yield release
    finally:
        release()


class RuntimeJsonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        r._snapshot_failures.clear()

    def test_failed_replace_keeps_old_data_and_cleans_owned_temporary(self):
        target = self.root/'checkpoint.json'
        r.atomic_write_json(target, {'version': 'old'})
        with patch.object(r.os, 'replace', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                t.write_json(target, {'version': 'new'})
        self.assertEqual(json.loads(target.read_text()), {'version': 'old'})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_concurrent_writers_have_independent_temporary_files(self):
        target = self.root/'progress.json'

        def save(number):
            r.atomic_write_json(target, {'number': number, 'payload': str(number)*100})

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(save, range(40)))
        saved = json.loads(target.read_text())
        self.assertEqual(saved['payload'], str(saved['number'])*100)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_snapshot_failure_is_bounded_logged_and_recovers(self):
        target = self.root/'progress.json'
        with patch.object(r, 'atomic_write_json', side_effect=PermissionError('denied')):
            with self.assertLogs('twodrive', level='WARNING') as messages:
                self.assertFalse(r.write_snapshot(target, {'phase': 'checking'}))
                self.assertFalse(r.write_snapshot(target, {'phase': 'checking'}))
            self.assertEqual(len(messages.records), 1)
        self.assertTrue(r.write_snapshot(target, {'phase': 'idle'}))
        self.assertNotIn(target, r._snapshot_failures)

    def test_snapshot_does_not_hide_programming_errors(self):
        with self.assertRaises(TypeError):
            r.write_snapshot(self.root/'progress.json', {'bad': object()})

    def test_activity_write_recovers_on_next_progress_publish(self):
        progress = SyncProgress(self.root)
        real_write = r.atomic_write_json

        def fail_activity(path, data, **kwargs):
            if Path(path).name == 'activity.json':
                raise PermissionError('locked activity')
            return real_write(path, data, **kwargs)

        with patch.object(r, 'atomic_write_json', side_effect=fail_activity):
            progress.activity('example.txt', 'upload')
        self.assertTrue(progress.activities_dirty)
        progress.publish(force=True)
        self.assertFalse(progress.activities_dirty)
        self.assertEqual(json.loads((self.root/'runtime/activity.json').read_text())[0]['name'], 'example.txt')

    def test_progress_failure_does_not_mask_actual_sync_error(self):
        error = TransientSyncError('remote timeout')
        with patch.object(r, 'atomic_write_json', side_effect=PermissionError('progress locked')), \
             patch.object(t, 'ROOT', self.root), patch.object(t, '_sync_once', side_effect=error):
            with self.assertRaises(TransientSyncError) as raised:
                t.sync_once({})
        self.assertIs(raised.exception, error)
        self.assertTrue(recovery_policy(raised.exception).retryable)
        self.assertFalse(recovery_policy(PermissionError('real local file denied')).retryable)

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing semantics')
    def test_short_windows_reader_lock_is_retried(self):
        target = self.root/'progress.json'
        r.atomic_write_json(target, {'phase': 'checking'})
        with deny_delete(target) as release:
            timer = threading.Timer(.06, release)
            timer.start()
            try:
                r.atomic_write_json(target, {'phase': 'idle'})
            finally:
                timer.join()
        self.assertEqual(json.loads(target.read_text()), {'phase': 'idle'})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing semantics')
    def test_long_progress_lock_does_not_abort_scan_finish_or_pause(self):
        progress = SyncProgress(self.root)
        old = progress.path.read_bytes()
        started = time.monotonic()
        with deny_delete(progress.path):
            progress.scan('115Drive', 1, 2, 'manifest')
            progress.finish(None)
            progress.finish(True, synchronized=False)
            self.assertEqual(progress.path.read_bytes(), old)
        self.assertLess(time.monotonic()-started, 3)
        progress.publish(force=True)
        self.assertEqual(json.loads(progress.path.read_text())['phase'], 'idle')
        self.assertEqual(list(progress.path.parent.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing semantics')
    def test_checkpoint_lock_still_raises_after_bounded_retries(self):
        target = self.root/'checkpoint.json'
        t.write_json(target, {'version': 1})
        with deny_delete(target), self.assertRaises(PermissionError):
            t.write_json(target, {'version': 2})
        self.assertEqual(json.loads(target.read_text()), {'version': 1})
        self.assertEqual(list(self.root.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
