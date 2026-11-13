import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer_upgrade import install, payload_files, Transaction, digest
from installer_process import WindowsController


def write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data if isinstance(data,bytes) else data.encode())


def fixture(base):
    payload=base/'payload';root=base/'app';local=base/'synced'
    write(payload/'115Drive.exe',b'new tray')
    write(payload/'service/TwoDrive.Service.exe',b'new service')
    write(payload/'filters.txt',b'new default rules')
    write(payload/'docs/new.md',b'new documentation')
    files={p.relative_to(payload).as_posix():digest(p) for p in payload.rglob('*') if p.is_file()}
    write(payload/'install-manifest.json',json.dumps(dict(product='115Drive',format=1,version='test',files=files)))
    write(root/'115Drive.exe',b'old tray');write(root/'service/TwoDrive.Service.exe',b'old service')
    write(root/'config.json',json.dumps(dict(local_path=str(local),legacy_enabled=False,start_with_windows=False)))
    tasks=dict(version=1,revision=8,tasks=[dict(id='one',paused=False,local_path=str(local),policy='always_keep'),dict(id='two',paused=True,local_path=str(base/'online'),policy='online_only')])
    write(root/'runtime/private/sync-tasks.json',json.dumps(tasks))
    for name in ('runtime/private/admin.json','runtime/private/initialized.json','runtime/private/bisync/history.lst',
                 'runtime/private/tasks/one/state.json','runtime/private/openlist/data.db-wal','runtime/private/transfer-resume/chunk',
                 'tray/settings.json','filters.txt','filters.txt.md5','unknown-user-file.txt','logs/user.log','recovery/old.txt'):
        write(root/name,('original '+name).encode())
    write(local/'document.txt',b'unchanged local content')
    return payload,root,local


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.payload,self.root,self.local=fixture(self.base)
        self.protected={p:(p.read_bytes(),p.stat().st_ino) for p in self.root.rglob('*') if p.is_file() and p.suffix!='.exe'}
        self.protected[self.local/'document.txt']=(b'unchanged local content',(self.local/'document.txt').stat().st_ino)

    def preserved(self):
        for path,(body,identity) in self.protected.items():
            self.assertEqual(path.read_bytes(),body,str(path));self.assertEqual(path.stat().st_ino,identity,str(path))

    def test_upgrade_preserves_private_state_custom_filters_unknown_files_and_local_identity(self):
        result=install(self.payload,self.root)
        self.assertTrue(result['upgraded']);self.preserved()
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'new tray')
        self.assertEqual((Path(result['backup'])/'old/115Drive.exe').read_bytes(),b'old tray')
        again=install(self.payload,self.root)
        self.assertEqual(again['replaced'],0);self.preserved()

    def test_badge_program_upgrade_preserves_personal_folder_icons(self):
        additions = {'register-folder-badge.ps1': b'badge entry point',
                     'register-account-navigation.ps1': b'account navigation',
                     'tray/FolderBadge.dll': b'badge renderer',
                     'tray/115Drive-folder.ico': b'folder fallback'}
        for name, data in additions.items():
            write(self.payload/name, data)
        manifest = json.loads((self.payload/'install-manifest.json').read_text())
        manifest['files'].update({name: digest(self.payload/name) for name in additions})
        write(self.payload/'install-manifest.json', json.dumps(manifest))
        personal = self.root/'runtime/shell/folder-icons/task.ico'
        backup = self.root/'runtime/shell/folder-icons/task.json'
        write(personal, b'personalized icon'); write(backup, b'original folder appearance')
        install(self.payload, self.root)
        for name, data in additions.items():
            self.assertEqual((self.root/name).read_bytes(), data)
        self.assertEqual(personal.read_bytes(), b'personalized icon')
        self.assertEqual(backup.read_bytes(), b'original folder appearance')
        self.preserved()

    def test_partial_replacement_failure_restores_entire_old_program(self):
        def fail(event,tx):
            if event=='after_replace': raise OSError('simulated file lock')
        with self.assertRaisesRegex(OSError,'simulated'): install(self.payload,self.root,checkpoint=fail)
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'old tray')
        self.assertEqual((self.root/'service/TwoDrive.Service.exe').read_bytes(),b'old service')
        self.assertFalse((self.root/'docs/new.md').exists());self.preserved()
        self.assertIsNone(Transaction.pending(self.root))

    def test_killed_installer_recovers_then_upgrades_without_touching_history(self):
        script='from installer_upgrade import install; import os,sys; install(sys.argv[1],sys.argv[2],checkpoint=lambda e,t: os._exit(77) if e=="after_replace" else None)'
        result=subprocess.run([sys.executable,'-c',script,str(self.payload),str(self.root)],timeout=20)
        self.assertEqual(result.returncode,77);self.assertIsNotNone(Transaction.pending(self.root))
        install(self.payload,self.root);self.preserved()
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'new tray')

    def test_committed_interruption_keeps_verified_new_program(self):
        script='from installer_upgrade import install; import os,sys; install(sys.argv[1],sys.argv[2],checkpoint=lambda e,t: os._exit(78) if e=="after_commit" else None)'
        result=subprocess.run([sys.executable,'-c',script,str(self.payload),str(self.root)],timeout=20)
        self.assertEqual(result.returncode,78)
        install(self.payload,self.root);self.preserved()
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'new tray')

    def test_tampered_payload_rejected_before_any_old_file_changes(self):
        write(self.payload/'115Drive.exe',b'broken')
        with self.assertRaises(ValueError): install(self.payload,self.root)
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'old tray');self.preserved()
        self.assertFalse((self.root/'runtime/private/installer-active.json').exists())

    def test_manifest_cannot_overwrite_config_or_history(self):
        for name in ('config.json','runtime/private/tasks/one/state.json','../escape','tray/settings.json'):
            data=json.loads((self.payload/'install-manifest.json').read_text())
            data['files'][name]='0'*64;write(self.payload/'install-manifest.json',json.dumps(data))
            with self.assertRaises(ValueError): payload_files(self.payload)
        self.preserved()

    def test_unrecognized_directory_and_sync_root_overlap_rejected(self):
        unrelated=self.base/'unrelated';write(unrelated/'my-file',b'user file')
        with self.assertRaises(ValueError): install(self.payload,unrelated)
        self.assertEqual((unrelated/'my-file').read_bytes(),b'user file')
        write(self.root/'config.json',json.dumps(dict(local_path=str(self.root/'personal'))))
        with self.assertRaisesRegex(ValueError,'重叠'): install(self.payload,self.root)

    def test_recovery_preserves_external_program_change_and_backup(self):
        script='from installer_upgrade import install; import os,sys; install(sys.argv[1],sys.argv[2],checkpoint=lambda e,t: os._exit(77) if e=="after_replace" else None)'
        subprocess.run([sys.executable,'-c',script,str(self.payload),str(self.root)],timeout=20)
        write(self.root/'115Drive.exe',b'externally modified')
        with self.assertRaisesRegex(OSError,'手动修改'): install(self.payload,self.root)
        self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'externally modified')
        self.assertIsNotNone(Transaction.pending(self.root));self.preserved()

    def test_fresh_install_and_retry_after_first_install_failure(self):
        root=self.base/'fresh'
        def fail(event,tx):
            if event=='after_replace':raise OSError('interrupted')
        with self.assertRaises(OSError):install(self.payload,root,checkpoint=fail)
        install(self.payload,root)
        self.assertEqual((root/'115Drive.exe').read_bytes(),b'new tray')

    @unittest.skipUnless(os.name=='nt','Windows sharing semantics')
    def test_locked_executable_rolls_back_and_can_be_retried(self):
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        kernel.CreateFileW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.CreateFileW(str(self.root/'service/TwoDrive.Service.exe'),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(OSError):install(self.payload,self.root)
            self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'old tray')
            self.preserved()
        finally:kernel.CloseHandle(handle)
        self.assertTrue(install(self.payload,self.root)['ok']);self.preserved()

    def test_backend_refuses_to_start_during_incomplete_upgrade(self):
        write(self.root/'runtime/private/installer-active.json',b'{}')
        entry=Path(__file__).resolve().parents[1]/'desktop_service.py'
        completed=subprocess.run([sys.executable,str(entry),'server','--root',str(self.root)],timeout=10)
        self.assertEqual(completed.returncode,2);self.preserved()


class FakeController(WindowsController):
    def __init__(self,root):
        super().__init__(root,timeout=.01)
        self.alive=True;self.calls=[];self.foreign=False;self.transfer=False;self.ready=True
    def native(self,action,data=None):
        self.calls.append(action)
        if action=='Inspect':return dict(processes=([dict(role='transfer' if self.transfer else 'service')] if self.alive else []),startup=[])
        if action=='Stop':self.alive=False
        return {}
    def request(self,path,body=None,nonce=None):
        if path=='/desktop':return dict(root=str(self.root/'other' if self.foreign else self.root),nonce='test',legacy_enabled=False)
        if path=='/tasks/pause':
            data=json.loads((self.root/'runtime/private/sync-tasks.json').read_text())
            assert data['revision']==body['revision']
            for task in data['tasks']:
                if task['id']==body['id']:task['paused']=True
            data['revision']+=1;write(self.root/'runtime/private/sync-tasks.json',json.dumps(data))
            return dict(ok=True)
        if path=='/status':return dict(authorization_allowed=self.ready)
        if path=='/qr/status':return dict(state='idle')
        if path=='/pause':return 'paused'
        raise AssertionError(path)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.payload,self.root,self.local=fixture(Path(self.temp.name))
        self.before=(self.root/'runtime/private/sync-tasks.json').read_bytes()
    def test_mixed_pause_states_and_revision_restored_byte_for_byte(self):
        controller=FakeController(self.root)
        install(self.payload,self.root,controller=controller)
        self.assertEqual((self.root/'runtime/private/sync-tasks.json').read_bytes(),self.before)
        self.assertIn('Stop',controller.calls);self.assertIn('Resume',controller.calls)
    def test_foreign_controller_never_stopped(self):
        controller=FakeController(self.root);controller.foreign=True
        with self.assertRaisesRegex(RuntimeError,'另一个'):install(self.payload,self.root,controller=controller)
        self.assertNotIn('Stop',controller.calls)
        self.assertEqual((self.root/'runtime/private/sync-tasks.json').read_bytes(),self.before)
    def test_transfer_and_drain_timeout_leave_old_program_and_pause_choices(self):
        for attribute in ('transfer','ready'):
            controller=FakeController(self.root);setattr(controller,attribute,attribute=='transfer')
            with self.assertRaises((RuntimeError,TimeoutError)):install(self.payload,self.root,controller=controller)
            self.assertNotIn('Stop',controller.calls)
            self.assertEqual((self.root/'runtime/private/sync-tasks.json').read_bytes(),self.before)
            self.assertEqual((self.root/'115Drive.exe').read_bytes(),b'old tray')


if __name__=='__main__':unittest.main()
