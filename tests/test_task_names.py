import unittest
from task_names import local_name,remote_name,remote_relative,excluded

class NamesTests(unittest.TestCase):
    def test_legacy_colon_fullwidth_and_quoted_names_remain_distinct(self):
        samples={'a:b.txt':'a：b.txt','a：b.txt':'a‛：b.txt','a?b.txt':'a？b.txt','a？b.txt':'a‛？b.txt',
                 'x.':'x．','x ':'x␠','x．':'x‛．','x␠':'x‛␠','a‛‛b.txt':'a‛‛b.txt'}
        for remote,local in samples.items():
            self.assertEqual(local_name(remote),local,remote)
            self.assertEqual(remote_name(local),remote,local)
        self.assertEqual(remote_relative('目录：一/a‛：b.txt'),'目录:一/a：b.txt')

    def test_invalid_and_non_roundtrip_paths_fail_closed(self):
        for name in ['..','a/b','a\\b','CON','x\x00','x\n']:
            with self.assertRaises(ValueError,msg=repr(name)):local_name(name)
        with self.assertRaises(ValueError):remote_name('a/b')

    def test_original_windows_metadata_and_temporary_exclusions(self):
        for path in ['desktop.ini','a/Thumbs.db','x/~$draft.xlsx','x/z.part','a/.DS_Store']:
            self.assertTrue(excluded(path),path)
        for path in ['book.pdf','desktop.ini.txt','partial.doc']:
            self.assertFalse(excluded(path),path)
