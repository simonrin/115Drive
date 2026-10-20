"""Frozen entry point; data paths never depend on PyInstaller's extraction folder."""
import argparse
import json
import os
from pathlib import Path
import sys


def defaults(root):
    import twodrive as t
    (root/'runtime/private').mkdir(parents=True,exist_ok=True)
    (root/'logs').mkdir(exist_ok=True)
    if not t.CONFIG.exists():
        t.write_json(t.CONFIG,dict(local_path=str(Path.home()/'115Drive'),remote_path='cloud115:115Drive',
            remote_backup='cloud115:115Drive_Recovery_AutoSync',recovery_enabled=False,local_scan_seconds=2,debounce_seconds=3,
            cloud_poll_seconds=15,incremental_scan=True,local_hash_cache=True,local_namespace='virtual_v1',start_with_windows=True))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['server','worker','diagnose'])
    parser.add_argument('--root',required=True)
    args=parser.parse_args()
    root=Path(args.root).resolve()
    os.environ['TWODRIVE_HOME']=str(root)
    import twodrive as t
    if args.action=='diagnose':
        result=dict(ok=True,frozen=bool(getattr(sys,'frozen',False)),root=str(t.ROOT),
                    dependencies={name:path.is_file() for name,path in dict(rclone=t.RCLONE,openlist=t.OPENLIST).items()})
        result['ok']=all(result['dependencies'].values())
        (root/'runtime').mkdir(exist_ok=True)
        t.write_json(root/'runtime/package-diagnostics.json',result)
        return 0 if result['ok'] else 1
    defaults(root)
    if args.action=='server':
        import setup_ui
        setup_ui.serve(bootstrap=True)
    else:
        sys.argv=[sys.argv[0],'worker']
        t.main()
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as error:
        # Exception text from our API layer excludes credentials.
        import twodrive as t
        t.status('error',str(error))
        sys.exit(1)
