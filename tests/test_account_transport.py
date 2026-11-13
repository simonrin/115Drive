import concurrent.futures
import http.server
import json
import socket
import ssl
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from account_transport import AccountHTTP, MetadataResponse


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.records = []
        self.accepted = 0
        self.other = ''
        self.block = threading.Event()
        self.entered = threading.Event()

    def get_request(self):
        result = super().get_request()
        self.accepted += 1
        return result

    @property
    def url(self): return 'http://127.0.0.1:'+str(self.server_port)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass

    def handle(self):
        try: super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    def do_HEAD(self): self.handle_request()
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.records.append(dict(method=self.command, path=self.path,
                                        headers=dict(self.headers), body=body.decode()))
        path = self.path.split('?', 1)[0]
        if path == '/drop':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.close_connection = True
            return
        if path == '/block':
            self.server.entered.set()
            self.server.block.wait(2)
        if path == '/slow': time.sleep(.15)
        status = 503 if path == '/error' else 200
        location = None
        if path in ('/redirect', '/loop', '/redirect-post', '/cross'):
            status = 302
            location = {'/redirect':'/ok', '/loop':'/loop', '/redirect-post':'/ok',
                        '/cross':self.server.other+'/ok'}[path]
        payload = json.dumps(dict(ok=True, method=self.command)).encode()
        self.send_response(status)
        if location: self.send_header('Location', location)
        if path == '/close': self.send_header('Connection', 'close')
        self.send_header('Content-Type', 'application/json')
        if path == '/chunked': self.send_header('Transfer-Encoding', 'chunked')
        else: self.send_header('Content-Length', len(payload)+10 if path == '/incomplete' else len(payload))
        self.end_headers()
        try:
            if self.command != 'HEAD':
                self.wfile.write((hex(len(payload))[2:].encode()+b'\r\n'+payload+b'\r\n0\r\n\r\n')
                                 if path == '/chunked' else payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        if path in ('/incomplete','/stale'):
            # /stale closes without a Connection:close header.
            self.close_connection = True


class AccountTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AccountHTTP(max_connections=2)

    def tearDown(self):
        self.server.block.set()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path='/ok', **kwargs):
        return self.client.open(urllib.request.Request(self.server.url+path, **kwargs), timeout=2)

    def test_complete_responses_reuse_actual_tcp_connection(self):
        for _ in range(3):
            with self.request() as response:
                self.assertEqual(response.getcode(), 200)
                self.assertEqual(response.geturl(), self.server.url+'/ok')
                self.assertEqual(response.getheader('Content-Type'), 'application/json')
                self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.server.accepted, 1)
        self.assertEqual(self.client.snapshot()['connections_reused'], 2)

    def test_caller_partial_read_does_not_poison_reused_connection(self):
        with self.request() as response: self.assertEqual(response.read(1), b'{')
        with self.request() as response: self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.server.accepted, 1)

    def test_chunked_json_is_complete_before_connection_reuse(self):
        with self.request('/chunked') as response: self.assertTrue(json.load(response)['ok'])
        with self.request() as response: self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.server.accepted, 1)

    def test_account_cookie_and_authorization_isolate_connections(self):
        for cookie, token in [('account-a', 'token-a'), ('account-b', 'token-b'), ('account-a','token-a')]:
            with self.request(headers={'Cookie':cookie, 'Authorization':token}): pass
        self.assertEqual(self.server.accepted, 2)
        self.assertEqual(self.client.snapshot()['connections_reused'], 1)
        data = json.dumps(self.client.snapshot())
        self.assertNotIn('account-a', data)
        self.assertNotIn('token-a', data)
        self.assertNotIn(self.server.url, data)

    def test_close_response_is_not_reused(self):
        with self.request('/close'): pass
        with self.request(): pass
        self.assertEqual(self.server.accepted, 2)

    def test_http_error_preserves_body_and_safe_connection_reuse(self):
        with self.assertRaises(urllib.error.HTTPError) as failure: self.request('/error')
        self.assertEqual(failure.exception.code, 503)
        self.assertTrue(json.load(failure.exception)['ok'])
        failure.exception.close()
        with self.request(): pass
        self.assertEqual(self.server.accepted, 1)
        self.assertEqual(self.client.snapshot()['http_errors'], 1)

    def test_truncated_body_discards_socket_without_retry(self):
        with self.assertRaises(urllib.error.URLError): self.request('/incomplete')
        self.assertEqual(len(self.server.records), 1)
        with self.request(): pass
        self.assertEqual(self.server.accepted, 2)

    def test_lost_mutation_response_never_replays(self):
        with self.assertRaises(urllib.error.URLError): self.request('/drop', data=b'write-once')
        self.assertEqual(len(self.server.records), 1)
        self.assertEqual(self.server.records[0]['body'], 'write-once')
        with self.request(): pass
        self.assertEqual(self.server.accepted, 2)

    def test_stale_keepalive_is_not_retried_even_for_get(self):
        with self.request('/stale'): pass
        # Peer closed after a valid response. http.client normally reports a
        # disconnected response; no internal request recovery is permitted.
        with self.assertRaises(urllib.error.URLError): self.request()
        self.assertEqual(len(self.server.records), 1)
        with self.request(): pass
        self.assertEqual(len(self.server.records), 2)

    def test_redirect_same_origin_keeps_cookie(self):
        with self.request('/redirect', headers={'Cookie':'secret'}) as response:
            self.assertEqual(response.url, self.server.url+'/ok')
        self.assertEqual(self.server.records[-1]['headers'].get('cookie'), 'secret')
        self.assertEqual(self.server.accepted, 1)

    def test_cross_origin_redirect_strips_authentication(self):
        other = Server()
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        self.server.other = other.url
        try:
            with self.request('/cross', headers={'Cookie':'secret', 'Authorization':'token',
                                                'Proxy-Authorization':'proxy-token', 'Referer':'private',
                                                'X-Api-Key':'custom-secret'}): pass
            headers = {k.lower():v for k,v in other.records[-1]['headers'].items()}
            for key in ('cookie', 'authorization', 'proxy-authorization', 'referer', 'x-api-key'):
                self.assertNotIn(key, headers)
            self.assertEqual(headers['host'], '127.0.0.1:'+str(other.server_port))
        finally:
            other.shutdown();other.server_close();thread.join(2)

    def test_post_redirect_is_returned_without_submitting_second_request(self):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request('/redirect-post', data=b'write-once')
        self.assertEqual(failure.exception.code, 302)
        failure.exception.close()
        self.assertEqual(len(self.server.records), 1)

    def test_redirect_loop_is_bounded(self):
        with self.assertRaises(urllib.error.HTTPError) as failure: self.request('/loop')
        failure.exception.close()
        self.assertEqual(len(self.server.records), 6)

    def test_idle_expiration_and_explicit_invalidation(self):
        with self.request(): pass
        self.client.idle_seconds = 0
        with self.request(): pass
        self.client.clear_idle()
        with self.request(): pass
        self.assertEqual(self.server.accepted, 3)

    def test_connections_bounded_under_concurrent_load(self):
        def fetch(_):
            with self.request('/slow') as response: return json.load(response)['ok']
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            self.assertEqual(list(executor.map(fetch, range(6))), [True]*6)
        self.assertEqual(self.server.accepted, 2)
        stats = self.client.snapshot()
        self.assertEqual(stats['active_connections'], 0)
        self.assertGreater(stats['pool_wait_seconds'], .1)

    def test_read_timeout_is_not_retried_and_slot_is_released(self):
        with self.assertRaises(urllib.error.URLError):
            self.client.open(self.server.url+'/slow', timeout=.02)
        self.assertEqual(self.client.snapshot()['active_connections'], 0)
        with self.request(): pass
        self.assertEqual(self.server.accepted, 2)

    def test_pool_wait_obeys_timeout(self):
        self.client.max_connections = 1
        def held():
            with self.request('/block'): pass
        worker = threading.Thread(target=held)
        worker.start()
        try:
            self.assertTrue(self.server.entered.wait(1))
            with self.assertRaises(urllib.error.URLError):
                self.client.open(self.server.url+'/ok', timeout=.02)
            self.assertEqual(len(self.server.records), 1)
        finally:
            self.server.block.set();worker.join(2)

    def test_closed_pool_refuses_new_requests(self):
        self.client.close()
        with self.assertRaises(urllib.error.URLError): self.request()
        self.assertEqual(self.server.accepted, 0)

    def test_default_tls_verifies_peer_and_hostname(self):
        self.assertTrue(self.client._tls.check_hostname)
        self.assertEqual(self.client._tls.verify_mode, ssl.CERT_REQUIRED)

    def test_https_redirect_cannot_downgrade(self):
        response = MetadataResponse(b'', status=302, reason='Found',
                                    headers={'Location':self.server.url+'/ok'}, url='https://example.test/')
        with patch.object(self.client, '_once', return_value=response) as once:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.client.open('https://example.test/')
            failure.exception.close()
            self.assertEqual(once.call_count, 1)
        self.assertEqual(self.server.accepted, 0)

    def test_open_data_argument_uses_post_without_mutating_input_request(self):
        request = urllib.request.Request(self.server.url+'/ok')
        with self.client.open(request, data=b'value=1', timeout=2) as response:
            self.assertEqual(json.load(response)['method'], 'POST')
        self.assertEqual(self.server.records[-1]['body'], 'value=1')
        self.assertEqual(request.get_method(), 'GET')

    def test_head_response_can_reuse_connection(self):
        with self.request(method='HEAD') as response: self.assertEqual(response.read(), b'')
        with self.request() as response: self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.server.accepted, 1)

    def test_rejects_unsupported_or_credential_urls(self):
        for url in ['file:///private', 'http://user:secret@example.test/file', 'http://example.test:70000/']:
            with self.assertRaises(urllib.error.URLError): self.client.open(url)
        self.assertEqual(self.server.accepted, 0)

    def test_environment_proxy_is_not_consulted(self):
        with patch.dict('os.environ', {'http_proxy':'http://127.0.0.1:1', 'HTTP_PROXY':'http://127.0.0.1:1'}):
            with self.request() as response: self.assertEqual(response.status, 200)


if __name__ == '__main__': unittest.main()
