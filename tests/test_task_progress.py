import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from task_service import TaskManager, task_activity, task_progress
from runtime_json import atomic_write_json, write_snapshot


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = TaskManager(types.SimpleNamespace(ROOT=self.root), shell=False)
        self.a = dict(id='a'*32, name='图片', local_path=str(self.root/'pictures'), paused=False)
        self.b = dict(id='b'*32, name='文件', local_path=str(self.root/'files'), paused=False)
        self.tasks = {x['id']:x for x in (self.a,self.b)}
        self.manager.workers = {x['id']:dict(ready=True,thread=types.SimpleNamespace(is_alive=lambda:True)) for x in (self.a,self.b)}

    def status(self, task, value):
        atomic_write_json(self.manager.store.directory(task['id'])/'status.json', value)

    def activity(self, task, value):
        atomic_write_json(self.manager.store.directory(task['id'])/'activity.json', value)

    def read(self, name):
        return json.loads((self.root/'runtime'/name).read_text(encoding='utf-8'))

    def test_real_phase_counts_and_idle_task_are_not_conflated(self):
        self.status(self.a,dict(state='checking',message='删除中',progress=dict(phase='deleting',name='截图/a.png',completed=18,total=75)))
        self.status(self.b,dict(state='idle',last_check_at=1700000000))
        self.manager.publish_summary(self.tasks)
        progress=self.read('tasks-progress.json')
        self.assertEqual(len(progress['active']),1)
        self.assertEqual(progress['active'][0]['phase'],'deleting')
        self.assertEqual(progress['active'][0]['completed'],18)
        self.assertEqual(progress['active'][0]['total'],75)
        self.assertEqual(self.read('tasks-status.json')['state'],'syncing')

    def test_network_and_queue_wait_are_preserved_without_fake_progress(self):
        self.status(self.a,dict(state='checking',progress=dict(phase='waiting_network',wait_reason='云端响应中')))
        self.status(self.b,dict(state='checking',progress=dict(phase='queued')))
        self.manager.publish_summary(self.tasks)
        rows=self.read('tasks-progress.json')['active']
        self.assertEqual([x['phase'] for x in rows],['waiting_network','queued'])
        self.assertEqual(rows[0]['wait_reason'],'云端响应中')
        self.assertNotIn('bytes',rows[0])

    def test_idle_summary_progress_and_history_do_not_rewrite(self):
        for task in self.tasks.values(): self.status(task,dict(state='idle',last_check_at=1700000000))
        self.manager.publish_summary(self.tasks)
        paths=[self.root/'runtime'/name for name in ('tasks-status.json','tasks-progress.json','tasks-activity.json')]
        before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths]
        with patch('task_service.write_snapshot', wraps=write_snapshot) as writer:
            for _ in range(20): self.manager.publish_summary(self.tasks)
            writer.assert_not_called()
        self.assertEqual(before,[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths])

    def test_activity_binds_root_and_id_to_configuration_sorts_and_deduplicates(self):
        a=dict(id='first',name='same.txt',action='upload',time='2026-11-12T21:00:00+08:00',task_id=self.b['id'],local_root='C:/wrong')
        b=dict(id='second',name='same.txt',action='download',time='2026-11-12T21:01:00+08:00')
        self.activity(self.a,[a,a]);self.activity(self.b,[b])
        for task in self.tasks.values(): self.status(task,dict(state='idle'))
        self.manager.publish_summary(self.tasks)
        rows=self.read('tasks-activity.json')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['task_id'],self.a['id'])
        self.assertEqual(rows[0]['local_root'],self.a['local_path'])
        self.assertEqual(rows[1]['task_id'],self.b['id'])
        self.assertEqual(self.read('tasks-status.json')['last_transfer_at'],rows[1]['time'])
        self.manager.publish_summary({self.b['id']:self.b})
        self.assertEqual([x['id'] for x in self.read('tasks-activity.json')],['second'])

    def test_paused_and_not_running_tasks_do_not_publish_stale_operation(self):
        self.status(self.a,dict(state='uploading',progress=dict(phase='uploading',name='a.txt')))
        self.a['paused']=True
        self.status(self.b,dict(state='checking',progress=dict(phase='deleting',name='b.txt')))
        self.manager.workers={}
        self.manager.publish_summary(self.tasks)
        progress=self.read('tasks-progress.json')
        self.assertEqual([x['phase'] for x in progress['active']],['preparing'])
        self.assertEqual(progress['active'][0]['name'],'')

    def test_malformed_event_and_status_cannot_escape_root_or_break_publishing(self):
        events=[dict(id='x',name='../out',action='upload',time='2026-11-12T00:00:00Z'),
                dict(id='y',name='ok.txt',action='download',time='invalid'),
                dict(id='z',name='ok.txt',action='backup',time='2026-11-12T00:00:00Z')]
        self.assertEqual(task_activity(self.a,events),[])
        self.assertEqual(task_activity(self.a,{}),[])
        row=task_progress(self.a,dict(state='checking',progress=dict(phase='deleting',name='../outside',completed=99,total=75,size=float('nan'))))
        self.assertEqual(row['name'],'')
        self.assertEqual(row['completed'],75)
        self.assertEqual(row['size'],0)
        self.status(self.a,[]);self.status(self.b,{})
        self.manager.publish_summary(self.tasks)

    def test_failed_snapshot_write_is_retried_without_losing_activity(self):
        for task in self.tasks.values(): self.status(task,dict(state='idle'))
        self.activity(self.a,[dict(id='x',name='a.txt',action='deleted',time='2026-11-12T00:00:00Z')])
        with patch('task_service.write_snapshot',return_value=False):self.manager.publish_summary(self.tasks)
        self.assertIsNone(self.manager.last_activity)
        self.manager.publish_summary(self.tasks)
        self.assertEqual(self.read('tasks-activity.json')[0]['action'],'deleted')

    def test_transport_diagnostics_only_refresh_after_completed_checks(self):
        class Counters:
            calls=0
            requests=10
            def snapshot(self):
                self.calls+=1
                return dict(requests=self.requests,connections_reused=6,request_seconds=2.25,Cookie='must not be published')
        http=Counters();self.manager.host.HTTP=http
        for task in self.tasks.values():self.status(task,dict(state='idle',last_check_at=1700000000,metrics=dict(cycles=1)))
        self.manager.publish_summary(self.tasks)
        diag=self.read('tasks-status.json')['diagnostics']
        self.assertEqual(diag['http']['requests'],10)
        self.assertEqual(diag['http']['connections_reused'],6)
        self.assertNotIn('Cookie',diag['http'])
        http.requests=20
        with patch('task_service.write_snapshot',wraps=write_snapshot) as writer:
            for _ in range(5):self.manager.publish_summary(self.tasks)
            writer.assert_not_called()
        self.assertEqual(http.calls,1)
        self.status(self.a,dict(state='idle',last_check_at=1700000100,metrics=dict(cycles=2)))
        self.manager.publish_summary(self.tasks)
        self.assertEqual(self.read('tasks-status.json')['diagnostics']['http']['requests'],20)
        self.assertEqual(http.calls,2)


if __name__ == '__main__': unittest.main()
