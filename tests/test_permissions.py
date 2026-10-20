import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import twodrive as t

CONFIG={'scheme':{'address':'127.0.0.1','http_port':5244}}
BEFORE={'id':1,'username':'admin','role':2,'permission':29183,'disabled':False,'base_path':'/','sso_id':'','allow_ldap':True}
AFTER=dict(BEFORE,permission=29695)
class PermissionsTests(unittest.TestCase):
    def test_only_approved_bit_changes_and_original_is_backed_up(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(t,'PRIVATE',Path(temp)),patch.object(t,'login',return_value='renewed-token'),patch.object(t,'read_json',return_value=CONFIG),patch.object(t,'write_json') as backup,patch.object(t,'api',side_effect=[{'content':[BEFORE]},None,{'content':[AFTER]}]) as api:
            t.ensure_webdav_permissions('local-token')
        sent=api.call_args_list[1].args[1]
        self.assertEqual(sent['permission']^BEFORE['permission'],512)
        self.assertEqual(sent,dict(AFTER,password=''))
        self.assertEqual(api.call_args_list[1].args[0],'/api/admin/user/update')
        self.assertEqual(api.call_args_list[2].kwargs['token'],'renewed-token')
        backup.assert_called_once()
        self.assertEqual(backup.call_args.args[1]['permission_before'],29183)
    def test_idempotent_and_unexpected_scope_is_rejected_without_writes(self):
        with patch.object(t,'read_json',return_value=CONFIG),patch.object(t,'api',return_value={'content':[AFTER]}) as api:
            t.ensure_webdav_permissions('token')
        self.assertEqual(api.call_count,1)
        for changed in (dict(BEFORE,permission=123),dict(BEFORE,id=2),dict(BEFORE,username='someone')):
            with patch.object(t,'read_json',return_value=CONFIG),patch.object(t,'api',return_value={'content':[changed]}) as api:
                with self.assertRaises(RuntimeError):t.ensure_webdav_permissions('token')
            self.assertEqual(api.call_count,1)
        with patch.object(t,'read_json',return_value={'scheme':{'address':'0.0.0.0','http_port':5244}}),patch.object(t,'api') as api:
            with self.assertRaises(RuntimeError):t.ensure_webdav_permissions('token')
        api.assert_not_called()
if __name__=='__main__':unittest.main()