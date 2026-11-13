"""Read-only NTFS journal access. Never creates, deletes or resizes a journal.

Layout references: Microsoft USN_RECORD_V2/V3, READ_USN_JOURNAL_DATA_V1,
USN_JOURNAL_DATA_V0 and FILE_ID_INFO. Unsupported/incomplete data fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import struct
import time


class JournalUnavailable(OSError):
    pass


@dataclass(frozen=True)
class JournalState:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int = 0

    def covers(self, cursor):
        return max(self.first_usn, self.lowest_valid_usn) <= cursor <= self.next_usn


@dataclass(frozen=True)
class UsnRecord:
    file_id: str
    parent_id: str
    usn: int
    reason: int
    attributes: int
    name: str

    @property
    def directory(self): return bool(self.attributes & 0x10)


def file_id(value):
    if isinstance(value, (bytes, bytearray, memoryview)): value = int.from_bytes(value, 'little')
    if not isinstance(value, int) or value < 0 or value >= 1 << 128:
        raise JournalUnavailable('Invalid file identity')
    return f'{value:032x}'


def parse_records(data, *, start=0):
    """Decode complete V2/V3 records, rejecting unsafe names and unknown layouts."""
    if not isinstance(data,(bytes,bytearray,memoryview)):raise JournalUnavailable('Invalid journal buffer')
    data=bytes(data)
    records, offset = [], start
    while offset < len(data):
        if len(data)-offset < 8: raise JournalUnavailable('Truncated USN record header')
        length, major, minor = struct.unpack_from('<IHH', data, offset)
        base = {2:60, 3:76}.get(major)
        if base is None or minor != 0 or length < base or length % 8 or offset+length > len(data):
            raise JournalUnavailable('Unsupported or incomplete USN record')
        width = 8 if major == 2 else 16
        fid = file_id(data[offset+8:offset+8+width])
        parent = file_id(data[offset+8+width:offset+8+2*width])
        pos = offset+8+2*width
        usn = struct.unpack_from('<q', data, pos)[0]
        reason, _, _, attributes, size, name_offset = struct.unpack_from('<IIIIHH', data, pos+16)
        if usn < 0 or not size or size % 2 or name_offset < base or name_offset % 2 or name_offset+size > length:
            raise JournalUnavailable('Invalid USN file name bounds')
        try: name = data[offset+name_offset:offset+name_offset+size].decode('utf-16-le')
        except UnicodeError as error: raise JournalUnavailable('Invalid USN file name encoding') from error
        if name in ('.', '..') or any(ch in name for ch in ('/', '\\', ':', '\x00')):
            raise JournalUnavailable('Unsafe USN file name')
        records.append(UsnRecord(fid, parent, usn, reason, attributes, name))
        offset += length
    return records


class UsnJournal:
    """A short-lived, metadata-only journal reader for the volume containing root."""
    QUERY = 0x000900F4
    READ = 0x000900BB
    READ_UNPRIVILEGED = 0x000903AB
    READ_FILE = 0x000900EB

    def __init__(self, root):
        if os.name != 'nt': raise JournalUnavailable('NTFS journals require Windows')
        self.root = Path(root).absolute()
        self.api = c.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW':([w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE],w.HANDLE),
            'DeviceIoControl':([w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p],w.BOOL),
            'GetFileInformationByHandleEx':([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'GetVolumePathNameW':([w.LPCWSTR,w.LPWSTR,w.DWORD],w.BOOL),
            'GetVolumeNameForVolumeMountPointW':([w.LPCWSTR,w.LPWSTR,w.DWORD],w.BOOL),
            'CloseHandle':([w.HANDLE],w.BOOL),
        }
        for name,(args,result) in signatures.items():
            method=getattr(self.api,name);method.argtypes=args;method.restype=result
        self.handle = None
        self.read_code = self.READ_UNPRIVILEGED
        volume = c.create_unicode_buffer(32768)
        if not self.api.GetVolumePathNameW(str(self.root),volume,len(volume)): self._error('Find volume')
        guid = c.create_unicode_buffer(128)
        if not self.api.GetVolumeNameForVolumeMountPointW(volume.value,guid,len(guid)): self._error('Find volume identity')
        self.volume = guid.value.rstrip('\\')
        # Use privileges already present on the process token, never request
        # elevation or change volume permissions. Unsupported hosts fall back.
        try:
            self.handle = self._open(self.volume, 0x80000000, 0)
            self.read_code = self.READ
        except JournalUnavailable as error:
            if error.errno != 5: raise
            self.handle = self._open(self.volume, 0, 0)

    def _error(self, operation):
        error = c.get_last_error()
        raise JournalUnavailable(error, f'{operation} failed (Windows error {error})')

    def _open(self, path, access=0, flags=0x02200000):
        result=self.api.CreateFileW(str(path),access,7,None,3,flags,None)
        if result == c.c_void_p(-1).value: self._error('Open metadata handle')
        return result

    def _io(self, handle, code, payload=b'', size=65536):
        output=c.create_string_buffer(size); count=w.DWORD()
        source=c.create_string_buffer(payload) if payload else None
        if not self.api.DeviceIoControl(handle,code,source,len(payload),output,size,c.byref(count),None):
            self._error('Read NTFS journal')
        if count.value > size: raise JournalUnavailable('Journal output overflow')
        return output.raw[:count.value]

    def identity(self, path):
        handle=self._open(path)
        try:
            raw=c.create_string_buffer(24)
            if not self.api.GetFileInformationByHandleEx(handle,18,raw,len(raw)): self._error('Read file identity')
            volume=struct.unpack_from('<Q',raw.raw)[0]
            fid=file_id(raw.raw[8:24])
            attributes=c.create_string_buffer(8)
            if not self.api.GetFileInformationByHandleEx(handle,9,attributes,len(attributes)): self._error('Read file attributes')
            attr,tag=struct.unpack('<II',attributes.raw)
            if attr & 0x400 and tag & 0xFFFF0FFF != 0x9000001A:
                raise JournalUnavailable('Non-cloud reparse point in journal index')
            records=parse_records(self._io(handle,self.READ_FILE,struct.pack('<HH',2,3)))
            if len(records)!=1 or records[0].file_id!=fid:
                raise JournalUnavailable('File identity changed while reading journal metadata')
            return dict(file_id=fid,volume_id=str(volume),usn=records[0].usn,directory=bool(attr & 0x10))
        finally: self.api.CloseHandle(handle)

    def binding(self):
        root=self.identity(self.root)
        if not root['directory']: raise JournalUnavailable('Sync root is not a directory')
        return dict(root=str(self.root).casefold(),root_id=root['file_id'],volume_id=root['volume_id'],volume=self.volume)

    def query(self):
        data=self._io(self.handle,self.QUERY,size=128)
        if len(data)<56: raise JournalUnavailable('Truncated journal state')
        identity,first,next_usn,lowest,maximum,_,_=struct.unpack_from('<QqqqqQQ',data)
        if min(first,next_usn,lowest,maximum)<0 or first>next_usn or lowest>next_usn:
            raise JournalUnavailable('Invalid journal boundaries')
        return JournalState(identity,first,next_usn,lowest)

    def read(self, start, stop, journal_id, *, max_pages=2048, max_seconds=10):
        before=self.query()
        if before.journal_id!=journal_id or not before.covers(start) or stop>before.next_usn or stop<start:
            raise JournalUnavailable('Journal cursor is no longer retained')
        result=[];cursor=start;deadline=time.monotonic()+max_seconds;previous=-1
        for _ in range(max_pages):
            if cursor>=stop: break
            if time.monotonic()>deadline: raise JournalUnavailable('Journal replay exceeded bounded work budget')
            payload=struct.pack('<qIIQQQHH4x',cursor,0xFFFFFFFF,0,0,0,journal_id,2,3)
            try: data=self._io(self.handle,self.read_code,payload)
            except JournalUnavailable as error:
                # Unsupported unprivileged control may use the legacy read with
                # this same zero-access handle. Access denial still falls back.
                if self.read_code!=self.READ_UNPRIVILEGED or error.errno not in (1,50,87): raise
                self.read_code=self.READ;data=self._io(self.handle,self.read_code,payload)
            if len(data)<8: raise JournalUnavailable('Truncated journal page')
            next_usn=struct.unpack_from('<q',data)[0]
            if next_usn<=cursor: raise JournalUnavailable('Incomplete journal replay')
            for record in parse_records(data,start=8):
                if record.usn<cursor or record.usn>=next_usn or record.usn<=previous:
                    raise JournalUnavailable('Unordered journal records')
                previous=record.usn
                if record.usn<stop:result.append(record)
            cursor=min(stop,next_usn)
            if cursor>=stop:break
        else: raise JournalUnavailable('Journal replay exceeded page limit')
        after=self.query()
        if after.journal_id!=journal_id or not after.covers(start):
            raise JournalUnavailable('Journal changed during replay')
        return result

    def close(self):
        if self.handle is not None:self.api.CloseHandle(self.handle);self.handle=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
