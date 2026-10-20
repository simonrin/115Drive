import configparser
from pathlib import Path
import unittest
from local_paths import cached_paths,configure_mapping


class LocalPathsTests(unittest.TestCase):
    def test_legacy_volume_is_derived_without_changing_rclone_pair_name(self):
        volume=Path.cwd().anchor
        local=Path(volume)/'115Drive'
        backup=Path(volume)/'application/recovery/test'
        cfg={'local_path':str(local)}
        parser=configparser.RawConfigParser()
        parser.read_string(configure_mapping('',cfg,Path(volume)/'application'))
        self.assertEqual(parser['localhash']['remote'],Path(volume).as_posix())
        self.assertEqual(cached_paths(local,backup,cfg,Path(volume)/'application'),
                         ('localhash:115Drive','localhash:application/recovery/test'))

    def test_legacy_nested_folder_is_rejected(self):
        volume=Path.cwd().anchor
        local=Path(volume)/'nested/115Drive'
        with self.assertRaises(RuntimeError):
            cached_paths(local,Path(volume)/'recovery',{'local_path':str(local)},Path(volume))
