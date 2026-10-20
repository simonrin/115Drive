import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
import twodrive as t


class SyncSkipTests(unittest.TestCase):
    def exercise(self, age):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);private=root/'private';private.mkdir()
            (private/'last-sync-run.json').write_text(json.dumps({'at':time.time()-age}))
            cfg=dict(local_path=directory,remote_path='cloud115:115Drive',remote_backup='cloud115:115Drive_Recovery_AutoSync')
            scanner=Mock();scanner.prepare.return_value=False
            with patch.object(t,'ROOT',root),patch.object(t,'PRIVATE',private),\
                 patch.object(t,'incremental_scanner',return_value=scanner),patch.object(t,'status'),\
                 patch.object(t,'login'),patch.object(t,'cloud_recovery_folder'),patch.object(t,'api'),\
                 patch.object(t,'bisync_args',return_value=[]),\
                 patch.object(t,'execute_sync',return_value=types.SimpleNamespace(returncode=0)) as execute:
                t._sync_once(cfg,False,Mock(),changed_paths=set())
                return execute.call_count

    def test_unchanged_recent_state_skips_expensive_bisync(self):
        self.assertEqual(self.exercise(60),0)

    def test_daily_content_verification_runs_even_without_metadata_changes(self):
        self.assertEqual(self.exercise(86401),1)
