import unittest
from unittest.mock import patch
import twodrive as t

class CloudNamesTests(unittest.TestCase):
    def test_conflicting_windows_names_stop_preflight(self):
        with patch.object(t,'login',return_value='private'), patch.object(t,'storages',return_value=[]), patch.object(t,'api',return_value={'content':[{'name':'A.txt','is_dir':False},{'name':'a.txt','is_dir':False}]}):
            with self.assertRaisesRegex(RuntimeError,'重复名称'):
                t.refresh_cloud_tree('cloud115:115Drive')

    def test_nested_folders_are_refreshed(self):
        with patch.object(t,'login',return_value='private'), patch.object(t,'storages',return_value=[]), patch.object(t,'api',side_effect=[{'content':[{'name':'nested','is_dir':True}]},{'content':[]}]) as api:
            t.refresh_cloud_tree('cloud115:115Drive')
        self.assertEqual([call.args[1]['path'] for call in api.call_args_list],['/115/115Drive','/115/115Drive/nested'])
        self.assertTrue(all(call.args[1]['refresh'] for call in api.call_args_list))

    def test_scope_guard(self):
        with self.assertRaisesRegex(RuntimeError,'outside'):
            t.refresh_cloud_tree('cloud115:another-folder')

class NativeNamesTests(unittest.TestCase):
    def test_raw_exact_duplicates_are_detected_before_openlist_merges_them(self):
        with patch.object(t,'native_115_request',return_value={'id':'1'}), patch.object(t,'native_115_list',return_value=[{'n':'same.txt','fid':'2'},{'n':'same.txt','fid':'3'}]):
            with self.assertRaisesRegex(RuntimeError,'重复名称'):
                t.native_115_check_names('private','115Drive')

    def test_raw_pagination_preserves_every_item(self):
        with patch.object(t,'native_115_request',side_effect=[{'cid':'1','count':3,'data':[{'n':'1'},{'n':'2'}]},{'cid':'1','count':3,'data':[{'n':'3'}]}]):
            rows=t.native_115_list('private','1')
        self.assertEqual(len(rows),3)
