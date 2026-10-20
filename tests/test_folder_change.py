import contextlib
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

import twodrive as t
from folder_change import FolderChanger


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.ROOT = self.base / 'app'
        self.PRIVATE = self.ROOT / 'runtime/private'
        self.PRIVATE.mkdir(parents=True)
        self.CONFIG = self.ROOT / 'config.json'
        self.SENTINEL = t.SENTINEL
        self.read_json, self.write_json, self.is_reparse = t.read_json, t.write_json, t.is_reparse
        self.worker_lock = contextlib.nullcontext
        self.source = self.base / '115Drive'
        self.source.mkdir()
        (self.source / t.SENTINEL).write_text('test marker')
        (self.source / '子目录').mkdir()
        (self.source / '空目录').mkdir()
        (self.source / '子目录/测试.txt').write_text('original content', encoding='utf-8')
        self.target = self.base / '新的 文件夹'
        self.cfg = dict(local_path=str(self.source), remote_path='cloud115:115Drive', local_hash_cache=True)
        self.write_json(self.CONFIG, self.cfg)
        self.write_json(self.PRIVATE / 'initialized.json', dict(local=str(self.source), remote='cloud115:115Drive', filter_sha256='fixture'))
        self.write_json(self.PRIVATE / 'local-snapshot.json', dict(local=str(self.source), remote='cloud115:115Drive', entries={'pending.txt':[10,20]}))
        (self.PRIVATE / 'rclone.conf').write_text('[cloud115]\ntype = alias\nremote = '+self.base.as_posix()+'/remote\n'
                                                 '\n[localhash]\ntype = hasher\nremote = '+self.base.as_posix()+'\nhashes = sha1\nmax_age = 24h\n')
        self.history = self.PRIVATE / 'bisync-cached'
        self.history.mkdir()
        for suffix in ('path1.lst', 'path2.lst'):
            (self.history / ('localhash_115Drive..cloud115_115Drive.' + suffix)).write_text('unchanged history')
        self.changer = FolderChanger(self)


class FolderChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=t.ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def test_preview_is_read_only_and_rejects_overlap_nonempty_relative(self):
        f = self.f
        before = f.CONFIG.read_bytes()
        preview = f.changer.preview(str(f.target))
        self.assertEqual(preview['files'], 2)
        self.assertFalse(f.target.exists())
        self.assertEqual(before, f.CONFIG.read_bytes())
        for target in (f.source, f.source / 'inside', f.source.parent, f.ROOT / 'inside', 'relative', 'C:\\', '\\\\server\\share'):
            with self.subTest(target=target), self.assertRaises(ValueError): f.changer.preview(str(target))
        f.target.mkdir()
        (f.target / 'existing.txt').write_text('never overwrite')
        with self.assertRaises(ValueError): f.changer.preview(str(f.target))

    def test_success_retains_history_source_pending_changes_and_pause(self):
        f = self.f
        (f.ROOT / 'runtime/stop').touch()
        history = {p.name:p.read_bytes() for p in f.history.iterdir()}
        f.changer.change(str(f.target))
        self.assertEqual(f.changer.view()['state'], 'complete')
        cfg = f.read_json(f.CONFIG)
        self.assertEqual(cfg['local_path'], str(f.target))
        self.assertEqual(cfg['remote_path'], 'cloud115:115Drive')
        self.assertEqual(cfg['local_namespace'], 'virtual_v1')
        self.assertEqual(f.read_json(f.PRIVATE / 'initialized.json')['local'], str(f.target))
        self.assertEqual(f.read_json(f.PRIVATE / 'local-snapshot.json')['entries'], {'pending.txt':[10,20]})
        self.assertEqual((f.source / '子目录/测试.txt').read_bytes(), (f.target / '子目录/测试.txt').read_bytes())
        self.assertTrue((f.target / '空目录').is_dir())
        self.assertTrue((f.ROOT / 'runtime/stop').exists())
        self.assertFalse(f.changer.guard.exists())
        self.assertEqual(history, {p.name:p.read_bytes() for p in f.history.iterdir()})
        mapping = (f.PRIVATE / 'rclone.conf').read_text()
        self.assertIn('remote = localview:', mapping)
        self.assertIn('"115Drive=', mapping)

    def test_old_error_listing_does_not_block_recovered_history(self):
        f=self.f
        error=f.history/'localhash_115Drive..cloud115_115Drive.path1.lst-err'
        error.write_text('old failure evidence')
        os.utime(error,(1,1))
        self.assertEqual(f.changer.preview(str(f.target))['files'],2)
        os.utime(error,(time.time()+5,time.time()+5))
        with self.assertRaisesRegex(ValueError,'需要恢复'): f.changer.preview(str(f.target))

    def test_commit_failure_restores_all_config_bytes(self):
        f = self.f
        before = {name:path.read_bytes() for name,path in f.changer.config_files().items()}
        original = f.write_json
        def fail(path, data):
            if path == f.CONFIG: raise OSError('injected config write failure')
            original(path, data)
        f.write_json = fail
        f.changer.change(str(f.target))
        self.assertEqual(f.changer.view()['state'], 'error')
        self.assertFalse(f.changer.guard.exists())
        self.assertEqual(before, {name:path.read_bytes() for name,path in f.changer.config_files().items()})
        self.assertTrue(f.target.is_dir())

    def test_cancel_keeps_original_and_copied_files(self):
        f = self.f
        publish = f.changer.publish
        def cancel(state, message, **extra):
            publish(state, message, **extra)
            if extra.get('step') == 'copying': f.changer.cancel()
        f.changer.publish = cancel
        f.changer.change(str(f.target))
        self.assertEqual(f.changer.view()['state'], 'cancelled')
        self.assertEqual(f.read_json(f.CONFIG), f.cfg)
        self.assertTrue(f.source.exists())
        self.assertTrue(f.target.exists())

    def test_source_edit_during_copy_aborts_before_switch(self):
        f = self.f
        publish = f.changer.publish
        def edit(state, message, **extra):
            publish(state, message, **extra)
            if extra.get('step') == 'verifying':
                (f.source / '子目录/测试.txt').write_text('newer content')
        f.changer.publish = edit
        f.changer.change(str(f.target))
        self.assertEqual(f.changer.view()['state'], 'error')
        self.assertEqual(f.read_json(f.CONFIG), f.cfg)
        self.assertEqual((f.source / '子目录/测试.txt').read_text(), 'newer content')

    def test_crash_during_commit_is_recovered_before_releasing_guard(self):
        f = self.f
        operation = 'a'*32
        backup = f.PRIVATE / 'folder-changes' / operation
        backup.mkdir(parents=True)
        for name,path in f.changer.config_files().items(): (backup/name).write_bytes(path.read_bytes())
        f.write_json(backup/'journal.json', dict(stage='committing',saved=list(f.changer.config_files())))
        f.write_json(f.changer.guard, dict(id=operation))
        f.write_json(f.CONFIG, dict(local_path='partial transaction'))
        f.changer.recover()
        self.assertEqual(f.read_json(f.CONFIG), f.cfg)
        self.assertFalse(f.changer.guard.exists())

    def test_ack_wait_does_not_swallow_errors_from_copy(self):
        f = self.f
        @contextlib.contextmanager
        def busy():
            raise RuntimeError('TwoDrive worker already running')
            yield
        f.worker_lock = busy
        f.write_json(f.PRIVATE/'folder-change-ack.json',dict(id='test'))
        with self.assertRaisesRegex(ValueError, 'fixture failure'):
            with f.changer.quiesce('test'): raise ValueError('fixture failure')

    def test_running_worker_acknowledges_guard_and_reloads_new_folder(self):
        f=self.f
        started=threading.Event()
        finished=threading.Event()
        observed=[]
        class Finished(Exception): pass
        def loop():
            observed.append(f.read_json(f.CONFIG)['local_path'])
            if len(observed)==2: raise Finished()
            started.set()
            deadline=time.monotonic()+5
            while not f.changer.guard.exists() and time.monotonic()<deadline: time.sleep(.01)
        def worker():
            try: t.worker()
            except Finished: finished.set()
        with patch.object(t,'ROOT',f.ROOT),patch.object(t,'PRIVATE',f.PRIVATE),patch.object(t,'CONFIG',f.CONFIG),patch.object(t,'worker_loop',side_effect=loop):
            f.worker_lock=t.worker_lock
            thread=threading.Thread(target=worker,daemon=True)
            thread.start()
            self.assertTrue(started.wait(3))
            f.changer.change(str(f.target))
            self.assertTrue(finished.wait(3))
            thread.join(3)
        self.assertEqual(observed,[str(f.source),str(f.target)])
        self.assertEqual(f.changer.view()['state'],'complete')

    def test_http_preview_and_change_use_fixture_and_require_nonce(self):
        import setup_ui as ui
        f=self.f
        server=ThreadingHTTPServer(('127.0.0.1',0),ui.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        url='http://127.0.0.1:'+str(server.server_port)
        def post(action,nonce):
            request=urllib.request.Request(url+'/folder/'+action,
                data=json.dumps(dict(path=str(f.target))).encode(),
                headers={'Host':'127.0.0.1:5245','Content-Type':'application/json','X-TwoDrive-Nonce':nonce})
            with urllib.request.urlopen(request,timeout=5) as response:
                return response.status,json.load(response)
        try:
            with patch.object(ui,'FOLDERS',f.changer),patch.object(ui,'BUSY',threading.Lock()):
                thread.start()
                with self.assertRaises(urllib.error.HTTPError) as failure: post('change','invalid')
                self.assertEqual(failure.exception.code,403)
                self.assertFalse(f.target.exists())
                code,preview=post('preview',ui.NONCE)
                self.assertEqual(code,200)
                self.assertEqual(preview['target'],str(f.target))
                self.assertFalse(f.target.exists())
                self.assertEqual(post('change',ui.NONCE)[0],202)
                deadline=time.monotonic()+5
                while ui.BUSY.locked() and time.monotonic()<deadline: time.sleep(.02)
                self.assertFalse(ui.BUSY.locked())
                self.assertEqual(f.changer.view()['state'],'complete')
                self.assertEqual(f.read_json(f.CONFIG)['local_path'],str(f.target))
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__ == '__main__': unittest.main()
