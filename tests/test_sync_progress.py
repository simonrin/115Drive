import tempfile
import unittest
import time
import json
import subprocess
import sys
import threading
from unittest.mock import patch
from pathlib import Path
from sync_progress import SyncProgress, SyncPaused, load, execute


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.progress=SyncProgress(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_counters_and_download_direction_survive_missing_fs_fields(self):
        self.progress.feed({'stats':{'bytes':25,'totalBytes':100,'speed':5,
            'transferring':[{'name':'资料.zip','srcFs':'cloud115:115Drive','size':100,'bytes':25}]}})
        self.progress.feed({'stats':{'bytes':50,'totalBytes':100,
            'transferring':[{'name':'资料.zip','size':100,'bytes':50,'speed':10}]}})
        item=load(self.progress.path,{})['active'][0]
        self.assertEqual((item['direction'],item['percentage'],item['speed']),('download',50,10))

    def test_scan_has_no_invented_file_percentage(self):
        self.progress.feed({'stats':{'checks':17,'totalChecks':20,'bytes':0}})
        result=load(self.progress.path,{})
        self.assertEqual(result['phase'],'checking')
        self.assertEqual(result['active'],[])
        self.assertEqual(result['checks'],17)

    def test_pause_at_readonly_directory_boundary_keeps_last_success(self):
        self.progress.finish(True)
        success=self.progress.data['last_success']
        self.progress.cancelled=lambda:True
        with self.assertRaises(SyncPaused):self.progress.scan('115Drive/图片',7,28,'cloud_check')
        self.progress.finish(None)
        result=load(self.progress.path,{})
        self.assertEqual((result['phase'],result['last_success']),('paused',success))

    def test_pause_stops_silent_owned_process_without_waiting_for_file(self):
        children=[]
        signal=threading.Event()
        self.progress.cancelled=signal.is_set
        def started(process):
            children.append(process)
            signal.set()
        self.progress.process_started=started
        started_at=time.monotonic()
        with self.assertRaises(SyncPaused):
            execute([sys.executable,'-c','import time; time.sleep(30)'],
                    self.progress,self.root/'transfer.log')
        self.assertLess(time.monotonic()-started_at,3)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stderr.closed)

    def test_pause_before_launch_never_starts_transfer(self):
        self.progress.cancelled=lambda:True
        with patch('sync_progress.subprocess.Popen') as launch:
            with self.assertRaises(SyncPaused):
                execute(['unused'],self.progress,self.root/'transfer.log')
            launch.assert_not_called()

    def test_pause_keeps_confirmed_percentage_and_last_success(self):
        self.progress.finish(True)
        previous=self.progress.last_success
        self.telemetry(phase='resuming',bytes=16*1024**2,confirmed=True,resumed=True)
        self.feed_local_done()
        self.progress.finish(None)
        row=load(self.progress.path,{})
        self.assertEqual((row['phase'],row['active'][0]['percentage'],row['active'][0]['speed']),('paused',25,0))
        self.assertEqual(row['last_success'],previous)

    def test_paused_view_reads_final_backend_ack_even_after_http_cancel(self):
        row=self.telemetry(phase='resuming',bytes=16*1024**2,confirmed=True,resumed=True)
        self.feed_local_done()
        row.update(phase='error',bytes=24*1024**2,speed=0)
        self.progress.backend_path.write_text(json.dumps(row))
        self.progress.finish(None)
        item=self.progress.data['active'][0]
        self.assertEqual((item['bytes'],item['percentage'],item['speed']),(24*1024**2,37.5,0))
        self.assertEqual(self.progress.data['phase'],'paused')

    def test_finished_file_activity_uses_bisync_copy_direction_and_skips_health(self):
        self.progress.feed({'msg':'- Path2 Do queued copies to - Path1'})
        self.progress.feed({'msg':'Copied (new)','object':'文件夹/资料.txt'})
        self.progress.feed({'msg':'Copied (new)','object':'.twodrive-health'})
        result=load(self.root/'runtime/activity.json',[])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['action'],'download')

    def test_failed_run_retains_last_success_and_clears_stale_transfer(self):
        self.progress.finish(True)
        success=self.progress.data['last_success']
        retry=SyncProgress(self.root)
        retry.feed({'stats':{'transferring':[{'name':'未完成.bin','size':100,'bytes':40}]}})
        retry.finish(False)
        result=load(retry.path,{})
        self.assertEqual(result['phase'],'error')
        self.assertEqual(result['last_success'],success)
        self.assertEqual(result['active'],[])

    def test_internal_backup_events_do_not_replace_user_activity(self):
        self.progress.feed({'msg':'Copied (new)','object':'资料.txt'})
        self.progress.feed({'msg':'Moved into backup dir','object':'资料.txt'})
        result=load(self.root/'runtime/activity.json',[])
        self.assertEqual([x['action'] for x in result],['synced'])
        self.progress.feed({'msg':'Deleted','object':'资料.txt'})
        self.assertEqual(load(self.root/'runtime/activity.json',[])[-1]['action'],'deleted')

    def telemetry(self, **changes):
        cfg=self.root/'runtime/private/rclone.conf'
        self.progress.bind_transfer(['--config',str(cfg),'--user-agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) TwoDrive/0123456789abcdef'])
        row=dict(version=1,run='0123456789abcdef',id='a'*32,name='/115Drive/资料.zip',
            direction='upload',phase='verifying',size=64*1024**2,bytes=0,
            saved_bytes=16*1024**2,speed=0,confirmed=False,resumed=False,updated=int(time.time()),note='')
        row.update(changes)
        self.progress.backend_path.parent.mkdir(parents=True,exist_ok=True)
        self.progress.backend_path.write_text(json.dumps(row),encoding='utf-8')
        return row

    def feed_local_done(self):
        self.progress.feed({'stats':{'speed':999999999,'transferring':[
            {'name':'资料.zip','dstFs':'cloud115:115Drive','size':64*1024**2,'bytes':64*1024**2,'speed':999999999}]}})

    def test_verification_keeps_saved_percentage_without_using_local_put_bytes(self):
        self.telemetry()
        self.feed_local_done()
        item=self.progress.data['active'][0]
        self.assertEqual((item['phase'],item['percentage'],item['speed']),('verifying',25,0))
        self.assertFalse(item['progress_confirmed'])
        self.assertEqual(self.progress.data['speed'],0)

    def test_resume_starts_at_checkpoint_then_advances_and_final_verification_keeps_100(self):
        row=self.telemetry(phase='resuming',bytes=16*1024**2,resumed=True,confirmed=True)
        for phase,count in [('resuming',16),('resuming',24),('verifying',64)]:
            row.update(phase=phase,bytes=count*1024**2)
            self.progress.backend_path.write_text(json.dumps(row))
            self.feed_local_done()
            item=self.progress.data['active'][0]
            self.assertEqual(item['bytes'],count*1024**2)
            self.assertEqual(item['phase'],phase)

    def test_completed_file_cannot_be_resurrected_by_old_backend_report(self):
        self.telemetry(phase='resuming',bytes=16*1024**2,resumed=True,confirmed=True)
        self.feed_local_done()
        self.progress.feed({'msg':'Copied (new)','object':'资料.zip'})
        self.progress.feed({'stats':{'transferring':[]}})
        self.assertEqual(self.progress.data['active'],[])

    def test_new_content_resets_only_after_confirmed_zero_baseline(self):
        self.telemetry(phase='transferring',bytes=0,confirmed=True,note='文件内容已变化')
        self.feed_local_done()
        item=self.progress.data['active'][0]
        self.assertEqual((item['bytes'],item['phase'],item['progress_confirmed']),(0,'transferring',True))

    def test_stale_other_command_wrong_file_and_invalid_counters_are_not_reused(self):
        for changes in [dict(run='f'*16),dict(updated=1),dict(name='different.zip'),dict(bytes=128*1024**2)]:
            with self.subTest(changes=changes):
                self.telemetry(**changes);self.feed_local_done()
                self.assertNotEqual(self.progress.data['active'][0].get('progress_source'),'backend')

    def test_no_percentage_is_invented_during_new_file_verification(self):
        self.telemetry(saved_bytes=0);self.feed_local_done()
        self.assertIsNone(self.progress.data['active'][0]['percentage'])

    def test_explicit_copyto_destination_name_matches_source_progress(self):
        self.telemetry(name='remote.bin',phase='resuming',bytes=16*1024**2,confirmed=True,resumed=True)
        self.progress.bind_transfer(['rclone','copyto','F:/local.bin','cloud115:115Drive/remote.bin',
            '--config',str(self.root/'runtime/private/rclone.conf'),'--user-agent','TwoDrive/0123456789abcdef'])
        self.progress.feed({'stats':{'transferring':[dict(name='local.bin',size=64*1024**2,
            bytes=64*1024**2,dstFs='cloud115:115Drive')]}})
        item=self.progress.data['active'][0]
        self.assertEqual((item['name'],item['phase'],item['percentage']),('local.bin','resuming',25))

    def test_malformed_telemetry_does_not_stop_progress_or_sync(self):
        self.telemetry()
        self.progress.backend_path.write_text('[]')
        self.feed_local_done()
        self.assertNotEqual(self.progress.data['active'][0].get('progress_source'),'backend')
        self.progress.finish(True)
        self.assertEqual(self.progress.data['phase'],'idle')
