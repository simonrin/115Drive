"""Atomic local JSON writes; UI snapshots are not synchronization checkpoints."""
import json
import logging
import os
from pathlib import Path
import tempfile
import time


_REPLACE_DELAYS = (.01, .03, .06, .1, .2)
_snapshot_failures = {}
log = logging.getLogger('twodrive')


class AtomicReplaceBusyError(PermissionError):
    """A prepared runtime document could not replace a Windows-locked target."""
    def __init__(self, path, error):
        super().__init__(error.errno, 'Runtime file replacement blocked', str(path))
        self.winerror = error.winerror


def atomic_write_json(path, value, *, indent=None, durable=False):
    payload = json.dumps(value, ensure_ascii=False, indent=indent).encode('utf-8')
    atomic_write_bytes(path, payload, durable=durable)


def atomic_write_bytes(path, payload, *, durable=False):
    """Keep the previous document intact and retry short Windows read locks.

    Permanent errors still propagate for credentials, sync history and other
    control data. Each writer owns a distinct temporary file, including cleanup.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        for attempt in range(len(_REPLACE_DELAYS)+1):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33):
                    raise
                if attempt == len(_REPLACE_DELAYS):
                    raise AtomicReplaceBusyError(path, error) from error
                time.sleep(_REPLACE_DELAYS[attempt])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup must not mask the original failure or remove another
            # writer's temporary file. Never unlink the destination to retry.
            pass


def write_snapshot(path, value):
    """Best-effort display data only; never use for durable sync/control state."""
    path = Path(path)
    try:
        atomic_write_json(path, value)
    except OSError as error:
        stamp = time.monotonic()
        previous = _snapshot_failures.get(path)
        if previous is None or stamp-previous >= 60:
            log.warning('UI snapshot write deferred: %s (winerror=%s, errno=%s); sync continues',
                        path.name, getattr(error, 'winerror', None), error.errno)
            _snapshot_failures[path] = stamp
        return False
    if path in _snapshot_failures:
        del _snapshot_failures[path]
        log.info('UI snapshot writes recovered: %s', path.name)
    return True
