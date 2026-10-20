"""Download the pinned official archive, verify SHA-256, then extract safely."""
import hashlib
from pathlib import Path
import urllib.request
import zipfile
root=Path(__file__).resolve().parents[1]
url='https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-windows-amd64.zip'
digest='200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e'
archive=root/'build/rclone-v1.75.1.zip'
archive.parent.mkdir(exist_ok=True)
if not archive.exists():
    with urllib.request.urlopen(url,timeout=120) as response: archive.write_bytes(response.read())
if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest:raise RuntimeError('rclone archive SHA-256 mismatch')
destination=root/'bin/rclone-v1.75.1'
with zipfile.ZipFile(archive) as zipped:
    for member in zipped.infolist():
        if not (destination/member.filename).resolve().is_relative_to(destination.resolve()):raise RuntimeError('Unsafe archive path')
    zipped.extractall(destination)
print('rclone verified and extracted')
