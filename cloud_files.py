"""Windows x64 Cloud Files API binding. File contents never pass through fake files.

ABI follows Microsoft's cfapi.h. The caller supplies a version-checked download
function returning a complete, verified staging file outside the sync root.
"""
from __future__ import annotations
import contextlib
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import time
import uuid
from sync_tasks import relative_path, placeholder_stat

U32, U16, I32, I64, PTR = c.c_uint32, c.c_uint16, c.c_int32, c.c_int64, c.c_void_p
W = c.c_wchar_p


class Basic(c.Structure):
    _fields_ = [(x, I64) for x in ('creation', 'access', 'write', 'change')] + [('attributes', U32)]


class Metadata(c.Structure):
    _fields_ = [('basic', Basic), ('size', I64)]


class CreateInfo(c.Structure):
    _fields_ = [('name', W), ('metadata', Metadata), ('identity', PTR), ('identity_length', U32),
                ('flags', U32), ('result', I32), ('usn', I64)]


class Policy(c.Structure):
    _fields_ = [('primary', U16), ('modifier', U16)]


class Policies(c.Structure):
    _fields_ = [('size', U32), ('hydration', Policy), ('population', Policy),
                ('insync', U32), ('hardlink', U32), ('management', U32)]


class Registration(c.Structure):
    _fields_ = [('size', U32), ('name', W), ('version', W), ('identity', PTR),
                ('identity_length', U32), ('file_identity', PTR), ('file_identity_length', U32),
                ('provider_id', c.c_ubyte * 16)]


class RootInfo(c.Structure):
    _fields_ = [('file_id', I64), ('hydration', Policy), ('population', Policy),
                ('insync', U32), ('hardlink', U32), ('status', U32),
                ('name', c.c_wchar * 256), ('version', c.c_wchar * 256),
                ('identity_length', U32), ('identity', c.c_ubyte * 4096)]


class CallbackInfo(c.Structure):
    _fields_ = [('size', U32), ('connection', I64), ('context', PTR), ('volume_guid', W),
                ('volume_dos', W), ('serial', U32), ('root_id', I64), ('root_identity', PTR),
                ('root_identity_length', U32), ('file_id', I64), ('file_size', I64),
                ('identity', PTR), ('identity_length', U32), ('path', W), ('transfer', I64),
                ('priority', c.c_ubyte), ('correlation', PTR), ('process', PTR), ('request', I64)]


class Fetch(c.Structure):
    _fields_ = [('flags', U32), ('offset', I64), ('length', I64), ('optional_offset', I64),
                ('optional_length', I64), ('dehydration_time', I64), ('reason', U32)]


class CallbackParameters(c.Structure):
    _fields_ = [('size', U32), ('fetch', Fetch)]


class CallbackRegistration(c.Structure):
    _fields_ = [('kind', U32), ('callback', PTR)]


class Operation(c.Structure):
    _fields_ = [('size', U32), ('kind', U32), ('connection', I64), ('transfer', I64),
                ('correlation', PTR), ('status', PTR), ('request', I64)]


class Transfer(c.Structure):
    _fields_ = [('flags', U32), ('status', I32), ('buffer', PTR), ('offset', I64), ('length', I64)]


class TransferParameters(c.Structure):
    _fields_ = [('size', U32), ('data', Transfer)]


class PlaceholderInfo(c.Structure):
    _fields_ = [(x, I64) for x in ('on_disk', 'validated', 'modified', 'properties')] + [
        ('pin', U32), ('insync', U32), ('file_id', I64), ('root_id', I64),
        ('identity_length', U32), ('identity', c.c_ubyte * 4096)]


class Overlapped(c.Structure):
    _fields_ = [('internal', c.c_size_t), ('internal_high', c.c_size_t),
                ('offset', U32), ('offset_high', U32), ('event', PTR)]


class CloudFilesError(OSError):
    def __init__(self, operation, result):
        self.hresult = result & 0xffffffff
        super().__init__(f'{operation} 失败（0x{self.hresult:08X}）')


class LocalContentChanged(RuntimeError): pass


def check(result, name):
    if result < 0: raise CloudFilesError(name, result)


def identity_bytes(value):
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(result) > 4096: raise ValueError('文件身份信息过长。')
    return result


class CloudFiles:
    def __init__(self, root, task_id, fetch, *, event=None):
        if os.name != 'nt' or c.sizeof(PTR) != 8:
            raise RuntimeError('文件随选需要 Windows x64。')
        self.root = Path(root).absolute()
        self.task_id, self.fetch = task_id, fetch
        self.event = event or (lambda *args: None)
        self.connection = I64()
        self.connected = False
        self.pending = {}
        self.lock = threading.Lock()
        self.threads = set()
        self.dll = c.WinDLL('CldApi.dll')
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self._bind()
        self.owner = identity_bytes(dict(provider='Twodrive', task=task_id, root=str(self.root)))

    def _bind(self):
        P = c.POINTER
        signatures = {
            'CfRegisterSyncRoot': [W, P(Registration), P(Policies), U32],
            'CfUnregisterSyncRoot': [W],
            'CfGetSyncRootInfoByPath': [W, U32, PTR, U32, PTR],
            'CfConnectSyncRoot': [W, P(CallbackRegistration), PTR, U32, P(I64)],
            'CfDisconnectSyncRoot': [I64],
            'CfCreatePlaceholders': [W, P(CreateInfo), U32, U32, P(U32)],
            'CfExecute': [P(Operation), P(TransferParameters)],
            'CfOpenFileWithOplock': [W, U32, P(PTR)],
            'CfCloseHandle': [PTR],
            'CfReferenceProtectedHandle': [PTR],
            'CfReleaseProtectedHandle': [PTR],
            'CfGetWin32HandleFromProtectedHandle': [PTR],
            'CfConvertToPlaceholder': [PTR, PTR, U32, U32, PTR, PTR],
            'CfUpdatePlaceholder': [PTR, P(Metadata), PTR, U32, PTR, U32, U32, PTR, PTR],
            'CfSetPinState': [PTR, U32, U32, PTR],
            'CfSetInSyncState': [PTR, U32, U32, PTR],
            'CfHydratePlaceholder': [PTR, I64, I64, U32, PTR],
            'CfDehydratePlaceholder': [PTR, I64, I64, U32, PTR],
            'CfRevertPlaceholder': [PTR, U32, PTR],
            'CfGetPlaceholderInfo': [PTR, U32, PTR, U32, PTR],
            'CfGetPlaceholderStateFromAttributeTag': [U32, U32],
            'CfReportProviderProgress': [I64, I64, I64, I64],
        }
        for name, args in signatures.items():
            fun = getattr(self.dll, name)
            fun.argtypes, fun.restype = args, I32
        self.dll.CfCloseHandle.restype = None
        self.dll.CfReferenceProtectedHandle.restype = c.c_ubyte
        self.dll.CfReleaseProtectedHandle.restype = None
        self.dll.CfGetWin32HandleFromProtectedHandle.restype = PTR
        self.kernel.CreateFileW.argtypes = [W, U32, U32, PTR, U32, U32, PTR]
        self.kernel.CreateFileW.restype = PTR
        self.kernel.CloseHandle.argtypes = [PTR]
        self.kernel.CloseHandle.restype = I32
        self.kernel.DeviceIoControl.argtypes = [PTR, U32, PTR, U32, PTR, U32, PTR, PTR]
        self.kernel.DeviceIoControl.restype = I32
        self.kernel.SetFileInformationByHandle.argtypes = [PTR, U32, PTR, U32]
        self.kernel.SetFileInformationByHandle.restype = I32
        self.kernel.GetFileSizeEx.argtypes = [PTR, c.POINTER(I64)]
        self.kernel.GetFileSizeEx.restype = I32
        self.kernel.ReadFile.argtypes = [PTR, PTR, U32, c.POINTER(U32), c.POINTER(Overlapped)]
        self.kernel.ReadFile.restype = I32
        self.kernel.GetOverlappedResult.argtypes = [PTR, c.POINTER(Overlapped), c.POINTER(U32), I32]
        self.kernel.GetOverlappedResult.restype = I32

    def register(self):
        self.root.mkdir(parents=True, exist_ok=True)
        info = RootInfo()
        result = self.dll.CfGetSyncRootInfoByPath(str(self.root), 1, c.byref(info), c.sizeof(info), None)
        if result >= 0:
            if info.identity_length > 4096 or bytes(info.identity[:info.identity_length]) != self.owner:
                raise RuntimeError('目录已由其他同步根管理。')
            return
        identity = c.create_string_buffer(self.owner)
        reg = Registration(c.sizeof(Registration), '115Drive', '0.3.0', c.cast(identity, PTR), len(self.owner))
        reg.provider_id[:] = uuid.UUID('6634901e-5b10-4666-a0c4-32a34775d20e').bytes_le
        # FULL hydration; auto dehydration deliberately disabled. The engine
        # checks the authoritative cloud version before releasing any bytes.
        policies = Policies(c.sizeof(Policies), Policy(2, 0), Policy(3, 0), 0x100, 0, 0)
        check(self.dll.CfRegisterSyncRoot(str(self.root), c.byref(reg), c.byref(policies), 0), '注册同步根')

    def registered(self):
        """Inspect ownership without creating a directory or a registration."""
        info = RootInfo()
        result = self.dll.CfGetSyncRootInfoByPath(str(self.root), 1, c.byref(info), c.sizeof(info), None)
        if result & 0xffffffff in (0x80070001, 0x80070186): return False  # Unregistered / no longer under a sync root.
        check(result, '读取同步根注册')
        if info.identity_length > 4096 or bytes(info.identity[:info.identity_length]) != self.owner:
            raise RuntimeError('目录已由其他同步根管理，不能解除。')
        return True

    def connect(self):
        self.register()
        callback = c.WINFUNCTYPE(None, c.POINTER(CallbackInfo), c.POINTER(CallbackParameters))
        self._callbacks = [callback(self._fetch), callback(self._cancel),
                           callback(self._opened), callback(self._closed)]
        self._table = (CallbackRegistration * 5)(
            *[CallbackRegistration(kind, c.cast(fn, PTR)) for kind, fn in zip((0, 2, 5, 6), self._callbacks)],
            CallbackRegistration(0xffffffff, None))
        check(self.dll.CfConnectSyncRoot(str(self.root), self._table, None, 4 | 8, c.byref(self.connection)), '连接同步根')
        self.connected = True

    def close(self):
        if not self.connected: return
        with self.lock:
            for cancel in self.pending.values(): cancel.set()
        check(self.dll.CfDisconnectSyncRoot(self.connection), '断开同步根')
        self.connected = False
        # Callback objects must outlive pending callbacks and their worker threads.
        for thread in list(self.threads): thread.join(timeout=15)

    def unregister(self):
        """Only for an already drained root; never used as implicit cleanup."""
        if self.connected: self.close()
        check(self.dll.CfUnregisterSyncRoot(str(self.root)), '注销同步根')

    def _identity(self, info):
        if not 0 < info.identity_length <= 4096: raise ValueError('缺少云文件身份。')
        value = json.loads(c.string_at(info.identity, info.identity_length))
        if value.get('task') != self.task_id: raise ValueError('云文件不属于当前任务。')
        return value

    def _opened(self, ptr, params):
        try: self.event('open', self._identity(ptr.contents))
        except Exception: pass  # Telemetry cannot escape a native callback.

    def _closed(self, ptr, params):
        try: self.event('close', self._identity(ptr.contents))
        except Exception: pass

    def _cancel(self, ptr, params):
        with self.lock:
            event = self.pending.get((ptr.contents.transfer, ptr.contents.request))
            if event: event.set()

    def _fetch(self, ptr, params):
        info = ptr.contents
        operation = Operation(c.sizeof(Operation), 0, info.connection, info.transfer, None, None, info.request)
        # FULL hydration uses a complete verified staging file. Some Windows 10
        # requests expose only the low 32 bits of a >4 GiB required span; copying
        # that unaligned span is rejected. Supply the complete 64-bit file range
        # in aligned chunks (only the EOF chunk may be unaligned).
        offset, length = 0, info.file_size
        try:
            identity = self._identity(info)
            cancel = threading.Event()
            key = (info.transfer, info.request)
            with self.lock: self.pending[key] = cancel
            thread = threading.Thread(target=self._supply, args=(operation, identity, offset, length, cancel), daemon=True)
            with self.lock: self.threads.add(thread)
            thread.start()
        except Exception:
            self._transfer(operation, offset, length, None, 0xC0000185)

    def _transfer(self, operation, offset, length, data, status=0):
        buf = c.create_string_buffer(data) if data is not None else None
        parameters = TransferParameters(c.sizeof(TransferParameters), Transfer(0, I32(status).value,
            c.addressof(buf) if buf is not None else None, offset, length))
        check(self.dll.CfExecute(c.byref(operation), c.byref(parameters)), '填充文件内容')

    def _supply(self, operation, identity, offset, length, cancel):
        waiting = threading.Event()
        def progress():
            # Whole-file verification may exceed the platform's 60s callback
            # deadline. Report truthful zero committed bytes while staging.
            while not waiting.wait(15) and not cancel.is_set():
                self.dll.CfReportProviderProgress(operation.connection, operation.transfer, identity['size'], 0)
        heartbeat = threading.Thread(target=progress, daemon=True)
        heartbeat.start()
        requested_end = offset + length
        try:
            self.event('fetch', identity)
            # The backend verifies the entire downloaded content before exposing it.
            fetched = self.fetch(identity, cancel)
            with (fetched if hasattr(fetched, '__enter__') else contextlib.nullcontext(fetched)) as path:
                waiting.set()
                if cancel.is_set(): raise InterruptedError()
                size = path.stat().st_size
                if size != identity['size']: raise ValueError('下载大小不匹配。')
                end = min(offset + length, size)
                if not 0 <= offset <= end: raise ValueError('无效的下载范围。')
                with path.open('rb') as stream:
                    stream.seek(offset)
                    while offset < end:
                        if cancel.is_set(): raise InterruptedError()
                        data = stream.read(min(1024 * 1024, end - offset))
                        if not data: raise IOError('下载内容不完整。')
                        self._transfer(operation, offset, len(data), data)
                        offset += len(data)
                        self.dll.CfReportProviderProgress(operation.connection, operation.transfer, size, offset)
            self.event('hydrated', identity)
        except Exception as error:
            try: self._transfer(operation, offset, max(0, requested_end-offset), None, 0xC0000185)
            except Exception: pass
            try: self.event('fetch_error', {'error': type(error).__name__, 'detail':str(error)[:240], 'offset':offset,'length':length,'identity': identity})
            except Exception: pass
        finally:
            waiting.set()
            heartbeat.join(timeout=1)
            with self.lock:
                self.pending.pop((operation.transfer, operation.request), None)
                self.threads.discard(threading.current_thread())

    def path(self, relative):
        relative = relative_path(relative)
        path = self.root.joinpath(*relative.split('/'))
        for parent in path.parents:
            if parent == self.root: break
            if parent.exists() and placeholder_stat(parent).st_file_attributes & 0x400:
                state = self.state(parent)
                if not state or state.get('identity', {}).get('task') != self.task_id:
                    raise ValueError('目录包含不属于此任务的重解析点。')
        return path

    @contextlib.contextmanager
    def handle(self, path, *, exclusive=False, delete=False, write=True):
        handle = PTR()
        check(self.dll.CfOpenFileWithOplock(str(path), (2 if write else 0) | (1 if exclusive else 0) | (4 if delete else 0), c.byref(handle)), '打开云文件')
        try: yield handle
        finally: self.dll.CfCloseHandle(handle)

    def state(self, path):
        path = Path(path)
        attrs = placeholder_stat(path)
        # FILE_FLAG_OPEN_REPARSE_POINT avoids an implicit data fetch.
        handle = self.kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200000 | 0x02000000, None)
        if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        try:
            info = PlaceholderInfo()
            result = self.dll.CfGetPlaceholderInfo(handle, 1, c.byref(info), c.sizeof(info), None)
            if result & 0xffffffff == 0x80070178: return None  # ERROR_NOT_A_CLOUD_FILE
            check(result, '读取云文件状态')
            if info.identity_length > 4096: raise ValueError('云文件身份损坏。')
            identity = json.loads(bytes(info.identity[:info.identity_length])) if info.identity_length else {}
            # Directory allocation/metadata size is not a file data stream.
            return dict(pin=info.pin, insync=bool(info.insync), partial=not (attrs.st_file_attributes & 0x10) and info.on_disk < attrs.st_size,
                        on_disk=info.on_disk, modified=info.modified, identity=identity, file_id=info.file_id)
        finally: self.kernel.CloseHandle(handle)

    def create(self, relative, identity, *, directory=False):
        path = self.path(relative)
        buf = c.create_string_buffer(identity_bytes(identity))
        meta = Metadata(Basic(0, 0, 0, 0, 0x10 if directory else 0x80), identity.get('size', 0))
        info = CreateInfo(path.name, meta, c.cast(buf, PTR), len(buf.raw) - 1, 3)
        processed = U32()
        check(self.dll.CfCreatePlaceholders(str(path.parent), c.byref(info), 1, 1, c.byref(processed)), '创建占位文件')
        check(info.result, '创建占位文件')
        if processed.value != 1: raise RuntimeError('占位文件创建不完整。')

    def usn(self, relative):
        path = self.path(relative)
        handle = self.kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
        if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        try:
            buffer, length = c.create_string_buffer(4096), U32()
            if not self.kernel.DeviceIoControl(handle, 0x900eb, None, 0, buffer, len(buffer), c.byref(length), None):
                raise c.WinError(c.get_last_error())
            if length.value < 32 or struct.unpack_from('<H', buffer.raw, 4)[0] != 2:
                raise RuntimeError('不支持的 NTFS 文件版本记录。')
            return struct.unpack_from('<q', buffer.raw, 24)[0]
        finally: self.kernel.CloseHandle(handle)

    def adopt(self, relative, identity, *, synced=True, expected_usn=None):
        path = self.path(relative)
        buf = c.create_string_buffer(identity_bytes(identity))
        with self.exclusive_content(path) as handle:
            with contextlib.nullcontext(handle) as raw:
                state = self.state(path)
                if synced and identity.get('kind') == 'file':
                    if state and state['partial']: raise RuntimeError('未完整下载的文件不能标记上传完成。')
                    # Nonzero UpdateUsn is rejected by some Windows builds even
                    # for unchanged files. Instead, hold a referenced exclusive
                    # content handle and rehash it immediately before commit.
                    if self.hash_handle(raw) != (identity['size'], identity['sha1']):
                        raise LocalContentChanged('文件在上传期间修改，保留未同步状态。')
                elif expected_usn is not None and self.usn(relative) != expected_usn:
                    raise LocalContentChanged('文件在接管前发生变化。')
                if state:
                    check(self.dll.CfUpdatePlaceholder(handle, None, c.cast(buf, PTR), len(buf.raw)-1, None, 0,
                          2 if synced else 0x40, None, None), '更新云文件身份')
                else:
                    check(self.dll.CfConvertToPlaceholder(handle, c.cast(buf, PTR), len(buf.raw)-1, 1 if synced else 0, None, None), '接管本地文件')
                return self.usn(relative)

    @contextlib.contextmanager
    def exclusive_content(self, path):
        # Held only for local hash/metadata commit, never for network transfers.
        # Sharing violations defer the operation without touching the file.
        # CfConvert/Update accept WRITE_DAC as an alternative to WRITE_DATA.
        # Request existing metadata rights on readonly files; never clear the
        # readonly flag or change the ACL. Share-none still excludes writers.
        access = 0x80040000 if placeholder_stat(path).st_file_attributes & 1 else 0xC0000000
        handle = self.kernel.CreateFileW(str(path), access, 0, None, 3, 0x02000000, None)
        if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        try: yield handle
        finally: self.kernel.CloseHandle(handle)

    @contextlib.contextmanager
    def metadata_handle(self, path):
        if placeholder_stat(path).st_file_attributes & 1:
            with self.exclusive_content(path) as handle: yield handle
        else:
            with self.handle(path, exclusive=True) as protected:
                with self.reference(protected) as handle: yield handle

    @contextlib.contextmanager
    def reference(self, handle):
        if not self.dll.CfReferenceProtectedHandle(handle): raise RuntimeError('文件正在使用，请稍后重试。')
        try: yield self.dll.CfGetWin32HandleFromProtectedHandle(handle)
        finally: self.dll.CfReleaseProtectedHandle(handle)

    def hash_handle(self, handle):
        size = I64()
        if not self.kernel.GetFileSizeEx(handle, c.byref(size)): raise c.WinError(c.get_last_error())
        digest = hashlib.sha1()
        offset = 0
        buffer = c.create_string_buffer(1024 * 1024)
        while offset < size.value:
            overlapped = Overlapped(0, 0, offset & 0xffffffff, offset >> 32, None)
            count = U32()
            if not self.kernel.ReadFile(handle, buffer, min(len(buffer), size.value-offset), c.byref(count), c.byref(overlapped)):
                if c.get_last_error() != 997: raise c.WinError(c.get_last_error())
                if not self.kernel.GetOverlappedResult(handle, c.byref(overlapped), c.byref(count), 1): raise c.WinError(c.get_last_error())
            if count.value == 0: raise RuntimeError('内容核验遇到意外文件结尾。')
            digest.update(buffer.raw[:count.value]); offset += count.value
        return size.value, digest.hexdigest()

    def update_remote(self, relative, identity, expected_usn):
        path = self.path(relative)
        buf = c.create_string_buffer(identity_bytes(identity))
        with self.metadata_handle(path) as handle:
            state = self.state(path)
            if not state or not state['insync']: raise RuntimeError('本地有修改，不能覆盖。')
            if self.usn(relative) != expected_usn: raise RuntimeError('本地文件在更新前发生变化。')
            previous_pin = state['pin']
            check(self.dll.CfSetPinState(handle, 0, 0, None), '准备更新云文件')
            meta = Metadata(Basic(), identity['size'])
            try:
                check(self.dll.CfUpdatePlaceholder(handle, c.byref(meta), c.cast(buf, PTR), len(buf.raw)-1,
                      None, 0, 1 | 2 | 4, None, None), '应用云端版本')
            finally:
                check(self.dll.CfSetPinState(handle, previous_pin, 0, None), '恢复保留状态')

    def reidentify(self, relative, identity):
        """Rename metadata without reading bytes or falsely clearing dirty state."""
        path = self.path(relative)
        buf = c.create_string_buffer(identity_bytes(identity))
        with self.metadata_handle(path) as handle:
            state = self.state(path)
            if not state or state['identity'].get('task') != self.task_id:
                raise RuntimeError('改名文件不属于当前任务。')
            old = state['identity']
            if (old.get('kind'), old.get('sha1'), old.get('size')) != (identity.get('kind'), identity.get('sha1'), identity.get('size')):
                raise RuntimeError('改名期间内容版本发生变化。')
            clean = state['modified'] == 0 and (identity.get('kind') == 'dir' or path.stat().st_size == identity['size'])
            check(self.dll.CfUpdatePlaceholder(handle, None, c.cast(buf, PTR), len(buf.raw)-1,
                  None, 0, 2 if clean else 0, None, None), '更新改名后的文件身份')

    def pin(self, relative, pin):
        with self.handle(self.path(relative), write=False) as handle:
            check(self.dll.CfSetPinState(handle, pin, 0, None), '设置保留状态')

    def hydrate(self, relative):
        with self.handle(self.path(relative), write=False) as handle:
            check(self.dll.CfHydratePlaceholder(handle, 0, -1, 0, None), '下载文件')

    def dehydrate(self, relative):
        path = self.path(relative)
        with self.metadata_handle(path) as handle:
            state = self.state(path)
            if not state or state['pin'] == 1 or not state['insync'] or state['modified']:
                raise RuntimeError('文件未同步或已固定，不能释放空间。')
            check(self.dll.CfDehydratePlaceholder(handle, 0, -1, 0, None), '释放空间')

    def revert(self, relative):
        """Draining a task preserves bytes; partially downloaded files are refused."""
        path = self.path(relative)
        with self.metadata_handle(path) as handle:
            state = self.state(path)
            if state is None: return
            if state['identity'].get('task') != self.task_id: raise RuntimeError('云文件身份发生变化。')
            if state['partial']: raise RuntimeError('文件下载状态发生变化，请重试解除同步。')
            check(self.dll.CfRevertPlaceholder(handle, 0, None), '保留内容并移除占位管理')

    def discard_online(self, relative):
        """Remove only a clean, incomplete local stub; never read or fetch data.

        Return False if hydration completed before the exclusive handle was
        acquired, so the caller can retain it as an ordinary local file.
        """
        with self.handle(self.path(relative), exclusive=True, delete=True, write=False) as protected:
            with self.reference(protected) as handle:
                info = PlaceholderInfo()
                check(self.dll.CfGetPlaceholderInfo(handle, 1, c.byref(info), c.sizeof(info), None), '核验仅联机占位文件')
                if info.identity_length > 4096: raise ValueError('云文件身份损坏。')
                identity = json.loads(bytes(info.identity[:info.identity_length])) if info.identity_length else {}
                if identity.get('task') != self.task_id or identity.get('kind') != 'file':
                    raise RuntimeError('云文件身份发生变化，取消移除本机占位条目。')
                size = I64()
                if not self.kernel.GetFileSizeEx(handle, c.byref(size)): raise c.WinError(c.get_last_error())
                if info.on_disk >= size.value: return False
                if not info.insync or info.modified or identity.get('size') != size.value:
                    raise RuntimeError('未完整下载的文件含未同步修改，已保留本机数据。')
                # FILE_DISPOSITION_INFO_EX: DELETE | IGNORE_READONLY_ATTRIBUTE.
                # This only unlinks the checked local stub, with no cloud client.
                disposition = U32(0x1 | 0x10)
                if not self.kernel.SetFileInformationByHandle(handle, 21, c.byref(disposition), c.sizeof(disposition)):
                    raise c.WinError(c.get_last_error())
        return True

    def remove(self, relative, expected_usn):
        path = self.path(relative)
        with self.handle(path, exclusive=True, delete=True) as handle:
            if self.usn(relative) != expected_usn: raise RuntimeError('文件已变化，取消删除。')
            if not self.dll.CfReferenceProtectedHandle(handle): raise RuntimeError('文件正在使用，取消删除。')
            try:
                native = self.dll.CfGetWin32HandleFromProtectedHandle(handle)
                disposition = I32(1)
                if not self.kernel.SetFileInformationByHandle(native, 4, c.byref(disposition), c.sizeof(disposition)):
                    raise c.WinError(c.get_last_error())
            finally: self.dll.CfReleaseProtectedHandle(handle)
