import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import twodrive as t

class SnapshotTests(unittest.TestCase):
    def test_deletion_during_scan_is_not_a_connection_failure(self):
        with tempfile.TemporaryDirectory(dir=t.ROOT/'tests') as tmp:
            root=Path(tmp); (root/'stable.txt').write_text('stable')
            doomed=root/'removed.txt';doomed.write_text('remove')
            original=t.is_reparse
            def concurrent_delete(path):
                if Path(path)==doomed:
                    doomed.unlink(missing_ok=True)
                return original(path)
            with patch.object(t,'is_reparse',side_effect=concurrent_delete):
                result=t.snapshot(root)
            self.assertIn('stable.txt',result)
            self.assertNotIn('removed.txt',result)

    def test_permission_failure_is_not_treated_as_deletion(self):
        with tempfile.TemporaryDirectory(dir=t.ROOT/'tests') as tmp:
            root=Path(tmp); (root/'protected.txt').write_text('protected')
            with patch.object(t,'is_reparse',side_effect=PermissionError('Denied')):
                with self.assertRaises(PermissionError): t.snapshot(root)
