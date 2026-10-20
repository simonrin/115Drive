from pathlib import Path
import sys
import zipfile
root=Path(sys.argv[1]).resolve()
with zipfile.ZipFile(sys.argv[2],'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for path in sorted(root.rglob('*')):
        if path.is_file(): archive.write(path,path.relative_to(root).as_posix())
