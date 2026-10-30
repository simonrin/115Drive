"""Recovery policy shared by scanning and the worker; no response bodies logged."""
from dataclasses import dataclass
import errno
import http.client
import urllib.error


class ActionRequiredError(RuntimeError):
    """A safety/authorization condition that needs an explicit retry."""


class IndexInvalidError(RuntimeError):
    """Fresh metadata contradicts the index; rebuild before any mutation."""


class ScanChangedError(RuntimeError):
    """A concurrent cloud edit requires another fresh, complete manifest."""


class TransientSyncError(RuntimeError):
    """A failed remote operation that can be retried without discarding hints."""


@dataclass(frozen=True)
class RecoveryPolicy:
    category: str
    retryable: bool
    rebuild_index: bool = False


def recovery_policy(error):
    if isinstance(error, ActionRequiredError):
        return RecoveryPolicy('action_required', False)
    if isinstance(error, IndexInvalidError):
        return RecoveryPolicy('index_invalid', True, True)
    if isinstance(error, ScanChangedError):
        return RecoveryPolicy('cloud_changing', True)
    if isinstance(error, urllib.error.HTTPError):
        return RecoveryPolicy('authorization' if error.code in (401, 403) else 'remote_http',
                              error.code in (408, 425, 429) or error.code >= 500)
    if isinstance(error, (PermissionError, FileNotFoundError)):
        return RecoveryPolicy('local_access', False)
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EROFS, errno.EACCES, errno.ENAMETOOLONG):
        return RecoveryPolicy('local_access', False)
    if isinstance(error, (TransientSyncError, urllib.error.URLError, TimeoutError,
                          ConnectionError, http.client.HTTPException, OSError)):
        return RecoveryPolicy('network_or_io', True)
    # Invalid structured metadata must never be accepted as an empty directory.
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return RecoveryPolicy('invalid_metadata', True, True)
    return RecoveryPolicy('unexpected', False)
