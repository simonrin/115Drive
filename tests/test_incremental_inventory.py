import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from local_watch import decode_notifications, DirectoryEvent, LocalChangeMonitor
from task_engine import TaskEngine
from task_activity import TaskActivity


class Native:
    def __init__(self, root, *args, **kwargs): self.root = root
    def path(self, name): return self.root/name
    def state(self, path): return None
    def usn(self, name): return (self.root/name).stat().st_mtime_ns


class IncrementalInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'files').mkdir()
        task = dict(id='a'*32, local_path=str(self.root/'files'), cloud_path='/test')
        self.engine = TaskEngine(task, self.root/'state', None, native_factory=Native)
        self.engine.state['local_root_id'] = self.engine.root.stat().st_ino

    def test_scoped_edit_does_not_inspect_unrelated_subtree(self):
        r = self.engine.root; (r/'one').mkdir(); (r/'two').mkdir()
        (r/'one/a').write_text('first'); (r/'two/b').write_text('second')
        before = self.engine.inventory(); (r/'one/a').write_text('edited')
        with patch.object(self.engine, 'inventory_entry', wraps=self.engine.inventory_entry) as read:
            after = self.engine.refresh_inventory(before, {'one/a'})
        self.assertEqual(after['two/b'], before['two/b'])
        self.assertNotEqual(after['one/a'], before['one/a'])
        self.assertNotIn('two/b', [c.args[0] for c in read.call_args_list])

    def test_directory_rename_and_subtree_move_refresh(self):
        r = self.engine.root; (r/'old').mkdir(); (r/'old/child').write_text('data')
        before = self.engine.inventory(); (r/'old').rename(r/'new')
        after = self.engine.refresh_inventory(before, {'old', 'new'})
        self.assertNotIn('old', after); self.assertNotIn('old/child', after)
        self.assertIn('new/child', after)

    def test_notification_parser_unicode_rename_and_invalid_chain(self):
        name = '截图\\图片.png'.encode('utf-16-le')
        data = struct.pack('<III', 0, 5, len(name))+name
        event = decode_notifications(data)
        self.assertEqual(event, 'event'); self.assertEqual(event.changes, ((5, '截图/图片.png'),))
        for broken in (data[:10], struct.pack('<III', 4, 5, len(name))+name,
                       struct.pack('<III', 0, 1, 4)+'..'.encode('utf-16-le')):
            with self.assertRaises(ValueError): decode_notifications(broken)

    def test_failed_incremental_read_retries_without_losing_notification(self):
        class Watcher:
            event = None
            def poll(self):
                value, self.event = self.event, None
                return value
            def close(self): pass
        watcher = Watcher(); calls = []
        def refresh(previous, paths):
            calls.append(paths)
            if len(calls) == 1: raise PermissionError('temporarily locked')
            return {'new': 1}
        monitor = LocalChangeMonitor(self.engine.root, lambda _: {}, watcher_factory=lambda _:watcher,
                                     incremental_scan=refresh)
        self.addCleanup(monitor.close); monitor.poll()
        watcher.event = DirectoryEvent([(1, 'new')])
        with self.assertRaises(PermissionError): monitor.poll()
        self.assertEqual(monitor.poll(), {'new': 1})
        self.assertEqual(calls, [{'new'}, {'new'}])

    def test_activity_is_completed_bounded_and_deduplicated(self):
        history = TaskActivity(self.root, limit=2)
        for n in range(3): history.completed('upload', str(n), operation=str(n))
        before = (history.path.read_bytes(), history.path.stat().st_mtime_ns)
        history.completed('upload', '2', operation='2')
        self.assertEqual(before, (history.path.read_bytes(), history.path.stat().st_mtime_ns))
        self.assertEqual([x['name'] for x in json.loads(before[0])], ['1', '2'])


if __name__ == '__main__': unittest.main()
