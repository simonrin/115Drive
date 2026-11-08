"""Generate the exact program inventory used by the in-place installer."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer_upgrade import MANIFEST, digest, managed, ordinary

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',type=Path)
    parser.add_argument('--version',required=True)
    args=parser.parse_args()
    files={}
    for path in sorted(args.stage.rglob('*')):
        ordinary(path)
        if path.is_file():
            name=path.relative_to(args.stage).as_posix()
            if name!=MANIFEST: files[managed(name)]=digest(path)
    (args.stage/MANIFEST).write_text(json.dumps(dict(product='115Drive',format=1,version=args.version,files=files),indent=2),encoding='utf-8')
if __name__=='__main__': main()
