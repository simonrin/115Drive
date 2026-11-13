"""Engine index integration without native registration or cloud operations."""
import types
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_engine import TaskEngine
from local_change_index import LocalChangeIndex
from tests.test_local_change_index import World


class LocalIndexIntegrationTests(unittest.TestCase):
    def engine(self,index):
        engine=object.__new__(TaskEngine)
        engine.local_index=index;engine._index_attempted=False;engine._journal_checked_at=0
        engine.metrics={};engine._last_local={'same':dict(kind='file',size=1)}
        engine.inventory=Mock(return_value={'full':dict(kind='file',size=2)})
        engine.refresh_inventory=Mock()
        engine.monitor=types.SimpleNamespace(current=engine._last_local,poll=Mock(return_value=engine._last_local))
        return engine

    def test_warm_startup_uses_recovered_snapshot_without_inventory(self):
        snapshot={'cached':dict(kind='file',size=1)}
        index=types.SimpleNamespace(available=True,recover=Mock(return_value=types.SimpleNamespace(snapshot=snapshot,mode='cached')))
        engine=self.engine(index)
        self.assertIs(engine.local_snapshot(),snapshot)
        engine.inventory.assert_not_called();index.recover.assert_called_once_with(engine.inventory,engine.refresh_inventory)
        self.assertTrue(engine.metrics['local_recovery']['journal_available'])

    def test_unavailable_volume_is_probed_once_and_does_not_rescan_each_poll(self):
        snapshot={'same':dict(kind='file',size=1)}
        index=types.SimpleNamespace(available=False,recover=Mock(return_value=types.SimpleNamespace(snapshot=snapshot,mode='full')),update=Mock())
        engine=self.engine(index)
        self.assertEqual(engine.local_snapshot(),snapshot)
        for _ in range(10):self.assertFalse(engine.local_changed())
        self.assertEqual(index.recover.call_count,1);index.update.assert_not_called();engine.inventory.assert_not_called()
        # Explicit monitor audit still obtains fresh inventory after the one
        # startup probe; it does not repeatedly query unavailable USN support.
        self.assertIn('full',engine.local_snapshot())
        self.assertEqual(index.recover.call_count,1);engine.inventory.assert_called_once()

    def test_fresh_watcher_snapshot_is_observed_before_cached_journal_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            world=World(Path(tmp)/'root');world.add('file.txt',2)
            index=LocalChangeIndex(world.root,Path(tmp)/'cache',journal_factory=lambda root:world)
            try:
                old=index.recover(world.scan,world.refresh).snapshot
                engine=self.engine(index);engine.inventory=world.scan;engine.refresh_inventory=world.refresh;engine._last_local=old
                world.rows['file.txt']['size']=99  # Notification before another journal record.
                engine.monitor.poll.return_value=world.rows
                self.assertTrue(engine.local_changed())
                self.assertEqual(engine.monitor.current['file.txt']['size'],99)
                self.assertEqual(world.scan_count,1)
            finally:index.close()

    def test_watcher_observation_is_durable_even_inside_journal_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            world=World(Path(tmp)/'root');world.add('file.txt',2)
            index=LocalChangeIndex(world.root,Path(tmp)/'cache',journal_factory=lambda root:world)
            try:
                old=index.recover(world.scan,world.refresh).snapshot
                engine=self.engine(index);engine.inventory=world.scan;engine.refresh_inventory=world.refresh;engine._last_local=old
                world.rows['file.txt']['size']=88;engine.monitor.poll.return_value=world.rows;engine._journal_checked_at=100
                with patch('task_engine.time.monotonic',return_value=100.1):self.assertTrue(engine.local_changed())
                self.assertEqual(index._db().execute('SELECT path FROM dirty').fetchone()[0],'file.txt')
                index.close()
                self.assertEqual(index.recover(world.scan,world.refresh).snapshot['file.txt']['size'],88)
            finally:index.close()


if __name__=='__main__':unittest.main()
