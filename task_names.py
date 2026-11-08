"""Unicode name mapping compatible with rclone's Windows and Standard encoders.

Reference: rclone lib/encoder (OS Windows, Standard, QuoteRune). Reject names
that cannot round-trip through the configured WebDAV Standard namespace.
"""
from fnmatch import fnmatchcase
from sync_tasks import relative_path

QUOTE = '\u201b'
WINDOWS = dict(zip('<>:"\\|?*/', '＜＞：＂＼｜？＊／'))
STANDARD = {'/':'／', '\x7f':'␡'}
for number in range(32):
    WINDOWS[chr(number)] = STANDARD[chr(number)] = chr(0x2400+number)
EXCLUDED = ('desktop.ini','thumbs.db','~$*','*.crdownload','*.part','*.partial','.ds_store')


def _encode(value, windows):
    mapping = WINDOWS if windows else STANDARD
    if value in ('.','..'): return value.replace('.', '．')
    if value in ('．','．．'): return ''.join(QUOTE+c for c in value)
    suffix = ''
    if windows and value:
        if value[-1] in (' ','.'):
            suffix = {' ':'␠','.':'．'}[value[-1]]; value=value[:-1]
        elif value[-1] in ('␠','．'):
            suffix=QUOTE+value[-1];value=value[:-1]
    reserved=set(mapping.values())|{QUOTE}
    return ''.join(mapping[c] if c in mapping else QUOTE+c if c in reserved else c for c in value)+suffix


def _decode(value, windows):
    reverse = {v:k for k,v in (WINDOWS if windows else STANDARD).items()}
    if value in ('．','．．'):return value.replace('．','.')
    if value in (QUOTE+'．',QUOTE+'．'+QUOTE+'．'):return value.replace(QUOTE,'')
    suffix=''
    if windows and value and value[-1] in ('␠','．'):
        last=value[-1];value=value[:-1]
        if value.endswith(QUOTE):suffix=last;value=value[:-1]
        else:suffix={'␠':' ','．':'.'}[last]
    result=[];quoted=False
    for c in value:
        if quoted:
            result.append(c if c in reverse or c==QUOTE else QUOTE+c);quoted=False
        elif c==QUOTE:quoted=True
        else:result.append(reverse.get(c,c))
    return ''.join(result)+suffix


def local_name(remote):
    if not isinstance(remote,str) or not remote or '/' in remote or '\\' in remote or remote in ('.','..'):
        raise ValueError('云端文件名无法安全映射。')
    value=_encode(_decode(remote,False),True)
    relative_path(value)
    if _encode(_decode(value,True),False)!=remote:
        raise ValueError('云端文件名不能无损映射，已保留原文件。')
    return value


def remote_name(local):
    relative_path(local)
    if '/' in local:raise ValueError('只允许单个文件名。')
    value=_encode(_decode(local,True),False)
    if local_name(value)!=local:
        raise ValueError('本地文件名不能无损映射，已保留原文件。')
    return value


def remote_relative(local):
    return '/'.join(remote_name(part) for part in relative_path(local).split('/'))


def excluded(path):
    return any(any(fnmatchcase(part.casefold(),pattern) for pattern in EXCLUDED) for part in path.split('/'))
