import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import setup_ui as u
import twodrive as t
class ReconnectionTests(unittest.TestCase):
    def test_initialized_sync_must_be_paused_before_authorization(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);private=root/'private';private.mkdir();(root/'runtime').mkdir()
            with patch.object(t,'ROOT',root),patch.object(t,'PRIVATE',private),patch.object(t,'read_json',return_value={'state':'paused'}):
                self.assertTrue(u.authorization_allowed())
                (private/'initialized.json').touch();self.assertFalse(u.authorization_allowed())
                (root/'runtime/stop').touch();self.assertTrue(u.authorization_allowed())
                with patch.object(t,'read_json',return_value={'state':'running'}):self.assertFalse(u.authorization_allowed())
    def test_reconnecting_preserves_history_and_never_resyncs_existing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            private=Path(temp);marker=private/'initialized.json'
            cfg=t.read_json(t.CONFIG)
            t.write_json(marker,{'local':cfg['local_path'],'remote':cfg['remote_path'],'filter_sha256':t.hashlib.sha256((t.ROOT/'filters.txt').read_bytes()).hexdigest()})
            with patch.object(t,'PRIVATE',private),patch('tests.smoke.exercise'),patch.object(t,'status'),patch.object(t,'run'),patch.object(t,'initialize_sync') as initialize:
                u.finish_activation()
            initialize.assert_not_called()
            marker.unlink()
            with patch.object(t,'PRIVATE',private),patch('tests.smoke.exercise'),patch.object(t,'status'),patch.object(t,'run'),patch.object(t,'initialize_sync') as initialize:
                u.finish_activation()
            initialize.assert_called_once()
if __name__=='__main__':unittest.main()