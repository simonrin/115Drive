from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import desktop_settings as d
import twodrive as t
from tests.test_folder_change import Fixture

class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=t.ROOT/'tests');self.addCleanup(self.temp.cleanup)
        self.f=Fixture(self.temp.name)
        for name,value in [('ROOT',self.f.ROOT),('PRIVATE',self.f.PRIVATE),('CONFIG',self.f.CONFIG)]:
            p=patch.object(t,name,value);p.start();self.addCleanup(p.stop)

    def test_native_info_exposes_no_credentials(self):
        info=d.view()
        self.assertEqual(info['local_path'],str(self.f.source))
        self.assertNotIn('password',info);self.assertNotIn('cookie',info)

    def test_initial_folder_saved_without_creating_or_syncing_data(self):
        (self.f.PRIVATE/'initialized.json').unlink()
        d.initial_folder(str(self.f.target))
        cfg=t.read_json(t.CONFIG)
        self.assertEqual(cfg['local_namespace'],'virtual_v1')
        self.assertEqual(cfg['local_path'],str(self.f.target))
        self.assertFalse(self.f.target.exists())

    def test_existing_sync_cannot_bypass_migration(self):
        with self.assertRaises(ValueError): d.initial_folder(str(self.f.target))
        self.assertEqual(t.read_json(t.CONFIG),self.f.cfg)

    def test_startup_is_applied_before_preference_is_saved(self):
        with patch.object(t,'run',side_effect=RuntimeError('task ownership mismatch')):
            with self.assertRaises(RuntimeError):d.preferences(dict(start_with_windows=False))
        self.assertEqual(t.read_json(t.CONFIG),self.f.cfg)
        with patch.object(t,'run') as command:
            d.preferences(dict(start_with_windows=False))
            self.assertEqual(command.call_args.args[0][-1],'0')
        self.assertFalse(t.read_json(t.CONFIG)['start_with_windows'])

    def test_invalid_startup_value_rejected(self):
        with self.assertRaises(ValueError): d.preferences(dict(start_with_windows='false'))
