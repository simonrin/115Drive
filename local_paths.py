"""Stable rclone names keep bisync history valid when the physical root moves."""
import configparser
import io
from pathlib import Path


def configure_mapping(text, cfg, root):
    parser = configparser.RawConfigParser()
    parser.read_string(text)
    if cfg.get('local_namespace') == 'virtual_v1':
        local = Path(cfg['local_path']).as_posix()
        recovery = (Path(root) / 'recovery').as_posix()
        parser['localview'] = dict(type='combine', upstreams=f'"115Drive={local}" "recovery={recovery}"')
        remote = 'localview:'
    else:
        # Preserve legacy volume mappings without embedding a developer drive.
        remote = Path(Path(cfg['local_path']).anchor).as_posix()
    parser['localhash'] = dict(type='hasher', remote=remote, hashes='sha1', max_age='24h')
    output = io.StringIO()
    parser.write(output)
    return output.getvalue()


def cached_paths(local, backup, cfg, root):
    local, backup = Path(local).resolve(), Path(backup).resolve()
    if local != Path(cfg['local_path']).resolve():
        raise RuntimeError('Local hash mapping does not match the configured folder')
    if cfg.get('local_namespace') == 'virtual_v1':
        relative = backup.relative_to((Path(root) / 'recovery').resolve())
        return 'localhash:115Drive', 'localhash:recovery/' + relative.as_posix()
    volume = Path(local.anchor)
    if local != volume / '115Drive' or backup.drive.upper() != local.drive.upper():
        raise RuntimeError('Use folder settings to change the local sync path')
    return 'localhash:115Drive', 'localhash:' + backup.relative_to(volume).as_posix()
