"""Authorization boundary checks; no account or cloud file mutations."""
import json
import threading
import unittest
from unittest.mock import Mock,patch
import auth115 as a
import twodrive as t

SESSION={'uid':'session-private','time':123,'sign':'sign-private'}
COOKIE_DATA={'cookie':{'UID':'user-private','CID':'cid-private','SEID':'seid-private','KID':'kid-private'}}

class LoginTests(unittest.TestCase):
    def test_confirmed_scan_activates_once_with_matching_app_and_private_cookie(self):
        busy=threading.Lock();activate=Mock(return_value=True)
        controller=a.QRController(busy,activate)
        def upstream(url,fields=None,post=False):
            if url==a.TOKEN_URL:return dict(SESSION)
            if url==a.STATUS_URL:
                self.assertEqual(fields['uid'],SESSION['uid'])
                self.assertIn('_',fields)
                return {'status':2}
            self.assertEqual(url,a.LOGIN_URL)
            self.assertEqual(fields,{'account':SESSION['uid'],'app':'wechatmini'})
            self.assertTrue(post)
            return COOKIE_DATA
        with patch.object(a,'fetch_json',side_effect=upstream),patch.object(a,'fetch_image',return_value=a.PNG_HEADER):
            self.assertTrue(controller.start())
            controller.thread.join(2)
        self.assertFalse(controller.thread.is_alive())
        activate.assert_called_once_with('UID=user-private;CID=cid-private;SEID=seid-private;KID=kid-private')
        public=json.dumps(controller.view())
        for secret in ('session-private','sign-private','user-private','cid-private','seid-private'):
            self.assertNotIn(secret,public)
        self.assertEqual(controller.state,'done')
        self.assertFalse(busy.locked())
    def test_expired_qr_never_activates(self):
        busy=threading.Lock();activate=Mock()
        controller=a.QRController(busy,activate)
        with patch.object(a,'fetch_json',side_effect=[SESSION,{'status':-1}]),patch.object(a,'fetch_image',return_value=a.PNG_HEADER):
            self.assertTrue(controller.start());controller.thread.join(2)
        self.assertEqual(controller.state,'expired');activate.assert_not_called()
        self.assertFalse(busy.locked())
    def test_cancel_and_duplicate_start_never_activate(self):
        busy=threading.Lock();activate=Mock();entered=threading.Event();release=threading.Event()
        controller=a.QRController(busy,activate)
        def token(*args,**kwargs):
            entered.set();release.wait(2);return SESSION
        with patch.object(a,'fetch_json',side_effect=token),patch.object(a,'fetch_image',return_value=a.PNG_HEADER):
            self.assertTrue(controller.start());self.assertTrue(entered.wait(1))
            self.assertFalse(controller.start());self.assertTrue(controller.cancel())
            release.set();controller.thread.join(2)
        activate.assert_not_called();self.assertEqual(controller.state,'cancelled')
        self.assertFalse(busy.locked())
    def test_invalid_cookie_and_failed_connect_do_not_report_success(self):
        with self.assertRaises(a.AuthError):a.cookie_from_result({'cookie':{'UID':'x'}})
        with self.assertRaises(a.AuthError):a.cookie_from_result({'cookie':{'UID':'x','CID':'y','SEID':'z;injected=x'}})
        busy=threading.Lock();controller=a.QRController(busy,Mock(return_value=False))
        with patch.object(a,'fetch_json',side_effect=[SESSION,{'status':2},COOKIE_DATA]),patch.object(a,'fetch_image',return_value=a.PNG_HEADER):
            self.assertTrue(controller.start());controller.thread.join(2)
        self.assertEqual(controller.state,'error');self.assertFalse(busy.locked())
    def test_saved_cloud_driver_contains_session_and_keeps_fixed_paths(self):
        cookie='UID=x;CID=y;SEID=z'
        record={'mount_path':'/115','driver':'115 Cloud','addition':json.dumps({'cookie':cookie})}
        with patch.object(t,'read_json',return_value={'recovery_enabled':False}),patch.object(t,'ensure_server'),patch.object(t,'ensure_webdav_permissions'),patch.object(t,'login',return_value='local-auth'),patch.object(t,'save_storage') as save,patch.object(t,'cloud_folder') as folder,patch.object(t,'write_rclone_config'),patch.object(t,'storages',return_value=[record]),patch.object(t,'status'):
            t.configure_cookie(cookie)
        args=save.call_args.args[1]
        self.assertEqual(args['driver'],'115 Cloud')
        self.assertEqual(json.loads(args['addition'])['cookie'],cookie)
        self.assertEqual([call.args[1:] for call in folder.call_args_list],[('/115','115Drive')])
        bad=dict(record,addition='{}')
        with patch.object(t,'ensure_server'),patch.object(t,'ensure_webdav_permissions'),patch.object(t,'login'),patch.object(t,'save_storage'),patch.object(t,'cloud_folder'),patch.object(t,'write_rclone_config'),patch.object(t,'storages',return_value=[bad]),patch.object(t,'status'):
            with self.assertRaises(RuntimeError):t.configure_cookie(cookie)

if __name__=='__main__':unittest.main()
