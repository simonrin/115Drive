import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
import transfer_recovery as r
import twodrive as t


class TransferRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root/'local'; self.local.mkdir()
        self.work = self.root/'runtime/private/bisync'; self.work.mkdir(parents=True)
        (self.work/'pair.path1.lst').write_text('history')
        self.cfg = dict(local_path=str(self.local),remote_path='cloud115:115Drive')
        self.manager = r.TransferRecovery(self.root,self.cfg,self.work,alive=lambda:False)

    def interrupt(self):
        args = self.manager.begin(['bisync'])
        suffix = args[-1]
        partial = self.local/('file.abc123'+suffix)
        partial.write_bytes(b'partial bytes')
        return partial

    def test_restart_cleans_only_owned_partials_and_lock_retains_history(self):
        own = self.interrupt()
        other = self.local/'user.partial';other.write_text('keep')
        unknown = self.local/'file.twodrive-ffffffffffffffffffffffffffffffff.partial'
        unknown.write_text('keep')
        (self.work/'pair.lck').write_text('stale lock')
        restarted = r.TransferRecovery(self.root,self.cfg,self.work,alive=lambda:False)
        self.assertTrue(restarted.prepare())
        self.assertFalse(own.exists())
        self.assertFalse((self.work/'pair.lck').exists())
        self.assertEqual((self.work/'pair.path1.lst').read_text(),'history')
        self.assertTrue(other.exists());self.assertTrue(unknown.exists())
        self.assertTrue(restarted.path.exists())  # Must retry until successful.
        restarted.begin(['bisync'])
        restarted.complete()
        self.assertFalse(restarted.path.exists())

    def test_active_orphan_is_not_killed_or_cleaned(self):
        own = self.interrupt()
        self.manager.alive=lambda:True
        with self.assertRaisesRegex(RuntimeError,'仍在运行'):self.manager.prepare()
        self.assertTrue(own.exists())

    def test_replaced_root_and_corrupt_record_block_cleanup(self):
        own = self.interrupt()
        data = json.loads(self.manager.path.read_text())
        data['local_identity'][1] += 1
        r.save(self.manager.path,data)
        with self.assertRaises(RuntimeError):self.manager.prepare()
        self.assertTrue(own.exists())
        self.manager.path.write_text('{')
        with self.assertRaises(RuntimeError):self.manager.prepare()
        self.assertTrue(own.exists())

    def test_unknown_lock_is_retained(self):
        self.interrupt()
        (self.work/'unrelated.lck').write_text('keep')
        with self.assertRaisesRegex(RuntimeError,'不属于'):self.manager.prepare()
        self.assertTrue((self.work/'unrelated.lck').exists())

    def test_first_run_lock_requires_recorded_process_and_exact_session(self):
        (self.work/'pair.path1.lst').unlink()
        self.interrupt()
        self.manager.started(Mock(pid=12345))
        lock=self.work/'initial-pair.lck'
        lock.write_text(json.dumps({'PID':'12345','Session':str(lock.with_suffix(''))}))
        self.assertTrue(self.manager.prepare())
        self.assertFalse(lock.exists())

    def test_changed_history_directory_blocks_recovery(self):
        own=self.interrupt()
        data=json.loads(self.manager.path.read_text())
        data['work_identity'][1]+=1
        r.save(self.manager.path,data)
        with self.assertRaises(RuntimeError):self.manager.prepare()
        self.assertTrue(own.exists())

    def test_interrupted_run_cannot_be_skipped_by_clean_index(self):
        self.interrupt()
        scanner=Mock();scanner.prepare.return_value=False
        (self.root/'runtime/private/last-sync-run.json').write_text(json.dumps({'at':r.time.time()}))
        cfg={**self.cfg,'remote_backup':'unused'}
        with patch.object(t,'ROOT',self.root),patch.object(t,'PRIVATE',self.root/'runtime/private'),\
             patch.object(t,'incremental_scanner',return_value=scanner),\
             patch.object(r,'rclone_running',return_value=False),\
             patch.object(t,'status'),patch.object(t,'execute_sync',return_value=Mock(returncode=0)) as execute:
            t._sync_once(cfg,False,Mock(),changed_paths=set())
        self.assertEqual(execute.call_count,1)
        args=execute.call_args.args[0]
        self.assertIn('--recover',args)
        self.assertNotIn('--resync',args)
        self.assertNotIn('--backup-dir1',args)
        self.assertFalse(self.manager.path.exists())

    def test_requested_pause_keeps_pending_run_and_does_not_commit_snapshot(self):
        scanner=Mock();scanner.prepare.return_value=True
        cfg={**self.cfg,'remote_backup':'unused'}
        with patch.object(t,'ROOT',self.root),patch.object(t,'PRIVATE',self.root/'runtime/private'),\
             patch.object(t,'incremental_scanner',return_value=scanner),patch.object(t,'status'),\
             patch.object(t,'execute_sync',side_effect=t.SyncPaused):
            with self.assertRaises(t.SyncPaused):
                t.sync_once(cfg,interruptible=True,changed_paths={'large.bin'})
        scanner.synchronized.assert_not_called()
        self.assertTrue(self.manager.path.exists())
        self.assertFalse((self.root/'runtime/private/last-sync-run.json').exists())
        self.assertEqual(json.loads((self.root/'runtime/progress.json').read_text())['phase'],'paused')

    def test_scratch_cleanup_requires_owner_and_ignores_unknown_files(self):
        directory,_=r.prepare_openlist_scratch(self.root)
        (directory/'file-123').write_bytes(b'bytes')
        (directory/'important.txt').write_text('keep')
        (directory/'folder').mkdir()
        _,result=r.prepare_openlist_scratch(self.root)
        self.assertEqual(result,dict(files=1,bytes=5))
        self.assertTrue((directory/'important.txt').exists())
        (directory.parent/'openlist-owner.json').write_text('{}')
        (directory/'file-123').touch()
        with self.assertRaises(RuntimeError):r.prepare_openlist_scratch(self.root)
        self.assertTrue((directory/'file-123').exists())

    def test_no_pending_journal_does_no_tree_walk(self):
        with patch.object(r.os,'walk') as walk:
            self.assertFalse(self.manager.prepare())
            walk.assert_not_called()

    def test_closed_port_does_not_allow_cleanup_of_live_server(self):
        socket=Mock()
        socket.connect_ex.return_value=1
        with patch.object(t,'login',side_effect=RuntimeError('not listening')),\
             patch.object(t.socket,'socket',return_value=socket),\
             patch.object(r,'server_process_running',return_value=True),\
             patch.object(r,'prepare_openlist_scratch') as clean:
            with self.assertRaisesRegex(RuntimeError,'尚未退出'):t._ensure_server()
            clean.assert_not_called()

    @unittest.skipUnless(sys.platform=='win32','Windows service startup lock')
    def test_simultaneous_server_start_waits_for_existing_launch(self):
        lock=self.root/'server.lock'
        code=("import msvcrt,sys,time; f=open(sys.argv[1],'a+b'); "
              "f.write(b'0'); f.flush(); f.seek(0); "
              "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); "
              "print('locked',flush=True); time.sleep(.4); f.close()")
        child=subprocess.Popen([sys.executable,'-c',code,str(lock)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               text=True,creationflags=t.NO_WINDOW)
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            start=time.monotonic()
            with t.worker_lock(lock):pass
            self.assertGreater(time.monotonic()-start,.2)
        finally:
            child.communicate(timeout=5)

    def test_cloud_pending_recovery_precedes_any_scanner(self):
        directory=self.root/'runtime/private/upload-transactions';directory.mkdir()
        (directory/'pending.json').write_text('{}')
        with patch.object(t,'ROOT',self.root),patch.object(t,'PRIVATE',self.root/'runtime/private'),\
             patch.object(t,'login',return_value='local-only'),patch.object(t,'status'),\
             patch.object(t,'api',side_effect=RuntimeError('pending conflict')),\
             patch.object(t,'incremental_scanner') as scan:
            with self.assertRaisesRegex(RuntimeError,'pending conflict'):
                t._sync_once(self.cfg,False,Mock())
            scan.assert_not_called()
