"""Bounded, credential-isolated HTTP connection reuse for metadata requests.

This is deliberately not a retry policy or a transfer client. Callers retain
their account pacing and transaction recovery. Even a failed request on a stale
keep-alive connection is returned to the caller: a submitted mutation must never
be replayed merely because its response was lost.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import math
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class MetadataResponse(io.BytesIO):
    """urllib-compatible response whose complete body no longer owns a socket."""
    def __init__(self, body, *, status, reason, headers, url):
        super().__init__(body)
        self.status = self.code = status
        self.reason, self.headers, self.url = reason, headers, url

    def getcode(self): return self.code
    def geturl(self): return self.url
    def info(self): return self.headers
    def getheader(self, name, default=None): return self.headers.get(name, default)


class AccountHTTP:
    """Small HTTP/HTTPS pool with urllib Request/.open compatibility.

    Connections are separated by origin and Cookie/Authorization identity. No
    system proxies, implicit credentials, cookies, or automatic retries are used.
    Complete metadata bodies are buffered before returning a connection, so an
    early close or JSON decoding failure cannot poison the next request.
    """
    def __init__(self, *, max_connections=8, idle_seconds=30, max_redirects=5):
        if max_connections < 1 or idle_seconds < 0 or max_redirects < 0:
            raise ValueError('Invalid HTTP pool limits')
        self.max_connections = max_connections
        self.idle_seconds = idle_seconds
        self.max_redirects = max_redirects
        self._condition = threading.Condition()
        self._idle = []  # (key, connection, returned_at); no raw credentials.
        self._allocated = 0
        self._closed = False
        self._tls = ssl.create_default_context()
        self._stats = dict(requests=0, responses=0, failures=0, http_errors=0,
                           redirects=0, connections_opened=0, connections_reused=0,
                           connections_closed=0, bytes_received=0,
                           pool_wait_seconds=0.0, connection_seconds=0.0,
                           header_seconds=0.0, body_seconds=0.0, request_seconds=0.0)

    def _count(self, **values):
        with self._condition:
            for name, value in values.items(): self._stats[name] += value

    def snapshot(self):
        """Aggregate counters only: no URLs, paths, headers or account IDs."""
        with self._condition:
            return dict(self._stats, idle_connections=len(self._idle),
                        active_connections=self._allocated-len(self._idle))

    def _discard(self, connection):
        connection.close()
        self._allocated -= 1
        self._stats['connections_closed'] += 1

    def close(self):
        with self._condition:
            self._closed = True
            for _, connection, _ in self._idle: self._discard(connection)
            self._idle.clear()
            self._condition.notify_all()

    def clear_idle(self):
        """Discard cached sessions after authentication/storage changes."""
        with self._condition:
            for _, connection, _ in self._idle: self._discard(connection)
            self._idle.clear()
            self._condition.notify_all()

    def _acquire(self, key, timeout):
        started = time.monotonic()
        deadline = started + timeout
        with self._condition:
            while True:
                if self._closed: raise urllib.error.URLError('HTTP transport closed')
                now = time.monotonic()
                for index in range(len(self._idle)-1, -1, -1):
                    saved_key, connection, returned = self._idle[index]
                    if now-returned >= self.idle_seconds:
                        self._idle.pop(index)
                        self._discard(connection)
                for index in range(len(self._idle)-1, -1, -1):
                    saved_key, connection, returned = self._idle[index]
                    if saved_key == key:
                        self._idle.pop(index)
                        self._stats['connections_reused'] += 1
                        self._stats['pool_wait_seconds'] += now-started
                        return connection, True
                if self._allocated >= self.max_connections and self._idle:
                    _, connection, _ = self._idle.pop(0)
                    self._discard(connection)
                if self._allocated < self.max_connections:
                    self._allocated += 1
                    self._stats['pool_wait_seconds'] += now-started
                    scheme, host, port, _ = key
                    factory = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
                    arguments = dict(timeout=max(.001, deadline-now))
                    if scheme == 'https': arguments['context'] = self._tls
                    try:
                        return factory(host, port, **arguments), False
                    except Exception:
                        self._allocated -= 1
                        self._condition.notify_all()
                        raise
                remaining = deadline-now
                if remaining <= 0:
                    self._stats['pool_wait_seconds'] += now-started
                    raise urllib.error.URLError(TimeoutError('HTTP pool wait timed out'))
                self._condition.wait(remaining)

    def _release(self, key, connection, reusable):
        with self._condition:
            if reusable and not self._closed and connection.sock is not None:
                self._idle.append((key, connection, time.monotonic()))
            else:
                self._discard(connection)
            self._condition.notify_all()

    @staticmethod
    def _address(url):
        try:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
                raise ValueError('Only HTTP/HTTPS metadata URLs are supported')
            if parts.username is not None or parts.password is not None:
                raise ValueError('URL credentials are not accepted')
            port = parts.port or (443 if parts.scheme.lower() == 'https' else 80)
            host = parts.hostname.encode('idna').decode('ascii').lower()
            target = urllib.parse.urlunsplit(('', '', parts.path or '/', parts.query, ''))
            return (parts.scheme.lower(), host, port), target
        except (ValueError, UnicodeError) as error:
            raise urllib.error.URLError(str(error)) from error

    @staticmethod
    def _key(origin, headers):
        identity = '\0'.join(headers.get(name, '') for name in
                             ('cookie', 'authorization', 'proxy-authorization'))
        return (*origin, hashlib.sha256(identity.encode('utf-8')).digest())

    def _once(self, url, method, body, headers, timeout):
        origin, target = self._address(url)
        key = self._key(origin, headers)
        started = time.monotonic()
        connection = None
        reusable = False
        self._count(requests=1)
        try:
            connection, reused = self._acquire(key, timeout)
            remaining = max(.001, timeout-(time.monotonic()-started))
            connection.timeout = remaining
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            else:
                before = time.monotonic()
                try:
                    connection.connect()
                    self._count(connections_opened=1)
                finally:
                    self._count(connection_seconds=time.monotonic()-before)
            before = time.monotonic()
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            self._count(header_seconds=time.monotonic()-before)
            before = time.monotonic()
            payload = response.read()
            self._count(body_seconds=time.monotonic()-before, bytes_received=len(payload), responses=1)
            reusable = (response.status >= 200 and not response.will_close
                        and headers.get('connection', '').lower() != 'close')
            result = MetadataResponse(payload, status=response.status, reason=response.reason,
                                      headers=response.headers, url=url)
            response.close()
            return result
        except (OSError, http.client.HTTPException) as error:
            self._count(failures=1)
            # No retries, including stale keepalive failures: the outer engine
            # owns recovery and must first establish whether a write committed.
            raise urllib.error.URLError(error) from error
        finally:
            if connection is not None: self._release(key, connection, reusable)
            self._count(request_seconds=time.monotonic()-started)

    def open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None:
            timeout = socket.getdefaulttimeout() or 30
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0: raise ValueError('Invalid HTTP timeout')
        request = fullurl if isinstance(fullurl, urllib.request.Request) else urllib.request.Request(fullurl, data=data)
        if data is not None and isinstance(fullurl, urllib.request.Request):
            request = urllib.request.Request(request.full_url, data=data,
                                             headers=dict(request.header_items()), method=getattr(request, 'method', None))
        url, body, method = request.full_url, request.data, request.get_method()
        headers = {key.lower(): value for key, value in request.header_items()}
        if body is not None and 'content-type' not in headers:
            headers['content-type'] = 'application/x-www-form-urlencoded'
        for redirects in range(self.max_redirects+1):
            result = self._once(url, method, body, headers, timeout)
            location = result.headers.get('Location')
            if result.status in (301, 302, 303, 307, 308) and location:
                # Following a mutation could submit the same intent again. Even
                # a 303 is handed back for an explicit caller decision.
                if method not in ('GET', 'HEAD') or redirects == self.max_redirects:
                    self._count(http_errors=1)
                    raise urllib.error.HTTPError(url, result.status, result.reason, result.headers, result)
                redirected = urllib.parse.urljoin(url, location)
                old_origin, _ = self._address(url)
                new_origin, _ = self._address(redirected)
                if old_origin[0] == 'https' and new_origin[0] != 'https':
                    self._count(http_errors=1)
                    raise urllib.error.HTTPError(url, result.status, 'HTTPS downgrade refused', result.headers, result)
                if new_origin != old_origin:
                    # A positive allowlist also prevents custom API-key headers
                    # from leaking to a redirect target on another origin.
                    headers = {key:value for key,value in headers.items() if key in
                               ('accept', 'accept-language', 'accept-encoding', 'user-agent')}
                result.close()
                self._count(redirects=1)
                url = redirected
                continue
            if not 200 <= result.status < 300:
                self._count(http_errors=1)
                raise urllib.error.HTTPError(url, result.status, result.reason, result.headers, result)
            return result

