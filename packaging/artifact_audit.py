"""Read-only artifact audit; never print matched credential values."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile


class Audit:
    def __init__(self, private_values=()):
        self.violations = []
        self.files = self.archives = self.modules = self.bytes = 0
        self.seen = set()
        values = list(private_values)
        user = os.environ.get('USERNAME') or Path.home().name
        if len(user) >= 4 and user.lower() not in {'user', 'runner', 'administrator', 'public'}:
            values.append(user)
        values += [str(Path.home()), str(Path.cwd().resolve())]
        needles = set()
        for value in values:
            if not isinstance(value, str) or len(value) < 5:
                continue
            for text in {value, value.replace('\\', '/'), value.replace('\\', '\\\\')}:
                for encoding in ('utf-8', 'utf-16-le'):
                    needles.add(text.lower().encode(encoding))
        self.private = re.compile(b'|'.join(re.escape(x) for x in sorted(needles, key=len, reverse=True))) if needles else None
        self.credentials = re.compile(
            rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}'
            rb'|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'
            rb'(?:\r?\n|\\n)(?s:.{64,16384}?)-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----)')

    def issue(self, name, category):
        self.violations.append({'file': name, 'category': category})

    def name(self, name, strict=True):
        normalized = name.replace('\\', '/')
        parts = PurePosixPath(normalized).parts
        if normalized.startswith('/') or any(x in {'.', '..'} or ':' in x for x in parts):
            self.issue(name, 'unsafe archive path')
        if not strict:
            return
        forbidden = {'runtime', 'logs', 'recovery', 'downloads', 'go-cache', 'go-mod',
                     '.git', '__pycache__', 'node_modules'}
        private_names = {'config.json', 'settings.json', 'admin.json', 'rclone.conf',
                         'initialized.json', 'local-snapshot.json', '.env', 'desktop.ini'}
        if any(x.lower() in forbidden for x in parts) or PurePosixPath(normalized).name.lower() in private_names:
            self.issue(name, 'private runtime file')
        if normalized.lower().endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.pdb', '.log')):
            self.issue(name, 'database log or debug symbols')

    def content(self, name, data, depth=0):
        self.files += 1
        self.bytes += len(data)
        if self.bytes > 3 * 1024**3 or depth > 6:
            raise ValueError('Artifact inspection limit exceeded')
        digest = hashlib.sha256(data).digest()
        if digest in self.seen:
            return
        self.seen.add(digest)
        if self.private and self.private.search(data.lower()):
            self.issue(name, 'local identity path or known private value')
        # The official rclone binary includes AWS's public documentation example
        # and PEM parser labels. A label alone is not an encoded private key.
        if any(m.group() != b'AKIAIOSFODNN7EXAMPLE' for m in self.credentials.finditer(data)):
            self.issue(name, 'credential-shaped material requires review')
        if data.startswith(b'PK\x03\x04'):
            self.archives += 1
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set()
                # Upstream examples are permitted; contents are still scanned.
                strict = 'third_party/' not in name.replace('\\', '/')
                for member in archive.infolist():
                    key = member.filename.replace('\\', '/').rstrip('/').casefold()
                    if key in names:
                        self.issue(name+'!'+member.filename, 'duplicate archive path')
                    names.add(key)
                    self.name(member.filename, strict=strict)
                    if stat.S_ISLNK(member.external_attr >> 16):
                        self.issue(name+'!'+member.filename, 'archive symlink')
                    if not member.is_dir():
                        self.content(name+'!'+member.filename, archive.read(member), depth+1)

    def frozen(self, path, name):
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(path))
        for member, record in archive.toc.items():
            if record[-1] == 'z':
                embedded = archive.open_embedded_archive(member)
                for module in embedded.toc:
                    data = embedded.extract(module, raw=True)
                    if data is not None:
                        self.modules += 1
                        self.content(name+'!PYZ!'+module, data)
            else:
                data = archive.extract(member)
                if data is not None:
                    self.content(name+'!'+member, data)

    def directory(self, root):
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                self.issue(path.relative_to(root).as_posix(), 'filesystem symlink')
            if not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            self.name(name)
            self.content(name, path.read_bytes())
            if path.name == 'TwoDrive.Service.exe':
                self.frozen(path, name)

    def report(self):
        return dict(ok=not self.violations, inspected_files=self.files,
                    nested_archives=self.archives, frozen_modules=self.modules,
                    inspected_bytes=self.bytes, violations=self.violations,
                    scope='File names, archive paths/CRC, UTF-8/UTF-16 local values, credential patterns, frozen Python modules. Not a malware scan or a guarantee against all secrets.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--private-values', type=Path, help='Private JSON string list; never included in the report')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    values = json.loads(args.private_values.read_text(encoding='utf-8')) if args.private_values else []
    if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
        raise ValueError('Private values must be a JSON list of strings')
    audit = Audit(values)
    audit.directory(args.root.resolve())
    report = audit.report()
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
