import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import twodrive as t

class RecoveryPreflight(unittest.TestCase):
    def test_unreadable_backup_aborts_before_file_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg={'local_path':directory,'remote_path':'cloud115:115Drive',
                 'remote_backup':'cloud115:115Drive_Recovery_AutoSync','recovery_enabled':True}
            with patch.object(t,'ROOT',Path(directory)),patch.object(t,'PRIVATE',Path(directory)/'runtime/private'),\
                 patch.object(t,'refresh_cloud_tree'),patch.object(t,'login',return_value='private'),\
                 patch.object(t,'cloud_recovery_folder',return_value='/115/115Drive_Recovery_AutoSync'),\
                 patch.object(t,'api',side_effect=RuntimeError('backup inaccessible')),patch.object(t,'status'),patch.object(t,'SyncProgress'),patch.object(t,'execute_sync') as transfer:
                with self.assertRaisesRegex(RuntimeError,'backup inaccessible'):t.sync_once(cfg)
                transfer.assert_not_called()

    def test_disabled_recovery_never_checks_or_creates_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg={'local_path':directory,'remote_path':'cloud115:115Drive',
                 'remote_backup':'cloud115:115Drive_Recovery_AutoSync','recovery_enabled':False}
            with patch.object(t,'ROOT',Path(directory)),patch.object(t,'PRIVATE',Path(directory)/'runtime/private'),\
                 patch.object(t,'refresh_cloud_tree'),patch.object(t,'login') as login,\
                 patch.object(t,'cloud_recovery_folder') as recovery,patch.object(t,'api') as api,\
                 patch.object(t,'status'),patch.object(t,'write_json'),patch.object(t,'SyncProgress'),\
                 patch.object(t,'execute_sync') as transfer:
                transfer.return_value.returncode=0
                t.sync_once(cfg)
                args=transfer.call_args.args[0]
                self.assertNotIn('--backup-dir1',args)
                self.assertNotIn('--backup-dir2',args)
                self.assertEqual(args[args.index('--conflict-loser')+1],'num')
                self.assertIn('--suffix-keep-extension',args)
                self.assertIn('--check-access',args)
                self.assertIn('--max-delete',args)
                login.assert_not_called()
                recovery.assert_not_called()
                api.assert_not_called()

    def test_disabled_recovery_connect_skips_folder_creation(self):
        with patch.object(t,'cloud_folder') as create:
            self.assertIsNone(t.cloud_recovery_folder('unused',{'recovery_enabled':False}))
            create.assert_not_called()
