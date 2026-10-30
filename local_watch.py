"""Directory events are scan hints. Snapshots and bisync remain authoritative."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time


class WindowsDirectoryEvents:
    """One outstanding recursive Windows read; no thread or unbounded queue.

    Rearm before returning a hint so changes during the ensuing snapshot/sync
    remain pending. A zero-byte completion means overflow and requires a scan.
    """
    def __init__(self, root):
        if os.name != 'nt':
            raise OSError('Directory notifications unavailable on this platform')
        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                        ('hEvent', wintypes.HANDLE)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            'CreateEventW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'ReadDirectoryChangesW': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                       wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
            'GetOverlappedResult': ([wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                     wintypes.BOOL], wintypes.BOOL),
            'CancelIoEx': ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            'ResetEvent': ([wintypes.HANDLE], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        self.handle = None
        self.event = None
        self.pending = False
        self.overlapped = Overlapped()
        # <= 64 KiB also works for network-backed Windows directory handles.
        self.buffer = ctypes.create_string_buffer(65536)
        try:
            self.handle = self.api.CreateFileW(str(Path(root).resolve()), 1, 7, None, 3,
                                               0x02000000 | 0x40000000, None)
            if self.handle == ctypes.c_void_p(-1).value:
                self.handle = None
                raise ctypes.WinError(ctypes.get_last_error())
            self.event = self.api.CreateEventW(None, True, False, None)
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.overlapped.hEvent = self.event
            self._arm()
        except BaseException:
            self.close()
            raise

    def _arm(self):
        self.api.ResetEvent(self.event)
        # Names, directories, attributes, size, last write and creation; exclude
        # last-access events produced merely by reading/hashing a file.
        ok = self.api.ReadDirectoryChangesW(self.handle, self.buffer, len(self.buffer),
                                            True, 0x5F, None, ctypes.byref(self.overlapped), None)
        if not ok and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
            raise ctypes.WinError(ctypes.get_last_error())
        self.pending = True

    def poll(self):
        count = wintypes.DWORD()
        if not self.api.GetOverlappedResult(self.handle, ctypes.byref(self.overlapped),
                                            ctypes.byref(count), False):
            error = ctypes.get_last_error()
            if error == 996:  # ERROR_IO_INCOMPLETE
                return None
            self.pending = False
            raise ctypes.WinError(error)
        self.pending = False
        self._arm()
        return 'event' if count.value else 'overflow'

    def close(self):
        if self.handle:
            if self.pending:
                self.api.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
                count = wintypes.DWORD()
                # Keep the OVERLAPPED and buffer alive until cancellation ends.
                self.api.GetOverlappedResult(self.handle, ctypes.byref(self.overlapped),
                                             ctypes.byref(count), True)
                self.pending = False
            self.api.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            self.api.CloseHandle(self.event)
            self.event = None


class LocalChangeMonitor:
    def __init__(self, root, scan, *, full_scan_seconds=60, poll_seconds=2,
                 enabled=True, notify=None, watcher_factory=None, clock=None):
        self.root = Path(root)
        self.scan = scan
        self.full_scan_seconds = max(1, float(full_scan_seconds))
        self.poll_seconds = max(.1, float(poll_seconds))
        self.enabled = enabled
        self.notify = notify or (lambda data: None)
        self.factory = watcher_factory or WindowsDirectoryEvents
        self.clock = clock or time.monotonic
        self.watcher = None
        self.identity = None
        self.current = None
        self.next_scan = 0
        self.next_retry = 0
        self.metrics = dict(mode='polling', scans=0, event_batches=0, overflows=0,
                            failures=0, starts=0, scan_reasons={}, last_error=None,
                            full_scan_seconds=self.full_scan_seconds, poll_seconds=self.poll_seconds)

    def close(self):
        if self.watcher:
            self.watcher.close()
            self.watcher = None

    def poll(self, *, force=False):
        now = self.clock()
        reason = None
        st = self.root.stat()
        identity = (st.st_dev, st.st_ino)
        if self.watcher and identity != self.identity:
            self.close()
            self.next_retry = 0
            reason = 'root_replaced'
        if self.enabled and not self.watcher and now >= self.next_retry:
            try:
                self.watcher = self.factory(self.root)
                self.identity = identity
                self.metrics['starts'] += 1
                self.metrics['last_error'] = None
                reason = reason or 'watcher_started'
            except OSError as error:
                self._failed(error, now)
                reason = 'watcher_error'
        if self.watcher:
            try:
                event = self.watcher.poll()
                if event:
                    self.metrics['event_batches'] += 1
                    if event == 'overflow':
                        self.metrics['overflows'] += 1
                    reason = event
            except OSError as error:
                self._failed(error, now)
                reason = 'watcher_error'
        if self.current is None:
            reason = 'startup'
        elif force:
            reason = 'forced'
        elif reason is None and now >= self.next_scan:
            reason = 'periodic' if self.watcher else 'polling'
        if reason:
            current = self.scan(self.root)  # Failed scans never advance the baseline.
            self.current = current
            self.next_scan = self.clock() + (self.full_scan_seconds if self.watcher else self.poll_seconds)
            self.metrics['mode'] = 'events' if self.watcher else 'polling'
            self.metrics['scans'] += 1
            counts = self.metrics['scan_reasons']
            counts[reason] = counts.get(reason, 0) + 1
            self.metrics.update(last_scan_reason=reason, last_scan_seconds=round(self.clock()-now, 6))
            self.notify(self.metrics)
        return self.current

    def _failed(self, error, now):
        self.close()
        self.next_retry = now + 30
        self.metrics['failures'] += 1
        self.metrics['last_error'] = str(error)
