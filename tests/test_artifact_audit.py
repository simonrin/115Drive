import importlib.util
import io
import json
from pathlib import Path
import unittest
import zipfile

spec=importlib.util.spec_from_file_location('artifact_audit',Path(__file__).resolve().parents[1]/'packaging/artifact_audit.py')
audit_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


class ArtifactAuditTests(unittest.TestCase):
    def test_nested_private_value_is_detected_without_disclosing_value(self):
        secret='fixture-private-'+'a'*40
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('source/demo.txt',secret.encode('utf-16-le'))
        audit=audit_module.Audit([secret])
        audit.content('third_party/example.zip',buffer.getvalue())
        report=audit.report()
        self.assertFalse(report['ok'])
        self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(report['nested_archives'],1)

    def test_owned_runtime_files_and_traversal_are_rejected(self):
        audit=audit_module.Audit()
        audit.name('runtime/private/admin.json')
        audit.name('../escape.txt',strict=False)
        audit.name('C:/escape.txt',strict=False)
        self.assertEqual(len(audit.violations),3)

    def test_upstream_example_name_is_allowed_but_content_is_still_scanned(self):
        audit=audit_module.Audit()
        audit.name('example/config.json',strict=False)
        self.assertTrue(audit.report()['ok'])
        audit.content('example/config.json',('gh'+'p_'+'X'*36).encode())
        self.assertFalse(audit.report()['ok'])

    def test_archive_crc_damage_fails_closed(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_STORED) as archive:
            archive.writestr('data.txt',b'known payload')
        damaged=buffer.getvalue().replace(b'known payload',b'other payload')
        with self.assertRaises(zipfile.BadZipFile):
            audit_module.Audit().content('test.zip',damaged)

    def test_public_example_and_pem_label_allowed_but_encoded_key_rejected(self):
        audit=audit_module.Audit()
        header=b'-----BEGIN '+b'RSA PRIVATE KEY-----'
        audit.content('parser.bin',b'AKIA' + b'IOSFODNN7EXAMPLE '+header+b' parser label')
        self.assertTrue(audit.report()['ok'])
        audit.content('key.pem',header+b'\n'+b'A'*80+b'\n-----END '+b'RSA PRIVATE KEY-----')
        self.assertFalse(audit.report()['ok'])
