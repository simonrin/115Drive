# Third-party components

This distribution contains separate programs. Their licenses and notices remain in effect.

| Component | Version | License / source |
|---|---|---|
| OpenList, modified by TwoDrive | 4.2.6 | AGPL-3.0, `licenses/OpenList-AGPL-3.0.txt`; https://github.com/OpenListTeam/OpenList/tree/v4.2.6 |
| OpenList frontend | 4.2.6 | Original license in the included source archive; https://github.com/OpenListTeam/OpenList-Frontend/tree/v4.2.6 |
| rclone | 1.75.1 | MIT, `licenses/rclone-COPYING.txt`; https://github.com/rclone/rclone/tree/v1.75.1 |
| CPython runtime | 3.11 | PSF and included third-party notices, `licenses/Python-LICENSE.txt`; https://www.python.org/ |
| PyInstaller bootloader | 6.22.3 | GPL with bootloader exception, `licenses/PyInstaller-COPYING.txt`; https://github.com/pyinstaller/pyinstaller |
| 115driver-derived cipher code | See OpenList source | Upstream license in `licenses/115driver-LICENSE.txt`; https://github.com/SheltonZhu/115driver |

The modified OpenList source, including the embedded frontend distribution, is in `third_party/OpenList-4.2.6-twodrive-source.zip`. Its changes cover 115 upload handshake padding, resumable OSS multipart uploads, verified download blocks, per-transfer progress reporting, validated startup cache import, extension-preserving overwrite transactions with persistent crash recovery, and deferring managed scratch cleanup to the TwoDrive supervisor. The preferred frontend source is supplied separately as `third_party/OpenList-Frontend-v4.2.6-source.zip`.

Use `third_party/build-openlist.ps1` to build the supplied backend source. Dependencies are pinned in its `go.mod` and `go.sum`. Additional dependencies retain the notices included in upstream sources.

The read-only 115 file-list and directory-tree protocol is also documented by the p115client project: https://github.com/ChenyangGao/p115client . TwoDrive's implementation is in `cloud_scan.py`.

115 is a trademark of its respective owner. This project is not an official 115 client or a Microsoft product.
