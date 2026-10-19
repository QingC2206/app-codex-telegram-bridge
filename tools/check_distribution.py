"""Audit built distributions: packaged plugins present, no runtime data."""
import argparse
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile

FORBIDDEN = re.compile(r'(^|/)(?:\.env(?:\..*)?|auth\.json|hooks\.json|config\.toml|.*credentials.*\.json|.*\.sqlite(?:3)?(?:-.*)?|.*\.log(?:\..*)?|.*\.validation\.json|bridge-install\.json|telegram_language\.json|telegram_(?:replies|attachments|message_routes|image_paths))(?:/|$)', re.I)
TOKEN = re.compile(rb'(?<![\w-])\d{6,}:[A-Za-z0-9_-]{20,}(?![\w-])|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}')


def audit(entries):
    names=[]
    for name,data in entries:
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or FORBIDDEN.search(name):
            raise ValueError('Forbidden distribution path: '+name)
        if TOKEN.search(data): raise ValueError('Potential secret in distribution: '+name)
        names.append(name)
    if not any(n.endswith('resources/bundled_plugins/screenshot/main.py') for n in names):
        raise ValueError('Missing bundled screenshot plugin.')
    return len(names)


def check(path):
    if path.suffix=='.whl' or path.suffix=='.zip':
        with zipfile.ZipFile(path) as archive:
            return audit((n,archive.read(n)) for n in archive.namelist() if not n.endswith('/'))
    with tarfile.open(path) as archive:
        return audit((n.name,archive.extractfile(n).read()) for n in archive if n.isfile())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('paths',nargs='*',type=Path);args=parser.parse_args()
    paths=args.paths or [*Path('dist').glob('*.whl'),*Path('dist').glob('*.tar.gz')]
    if not paths: raise SystemExit('No distributions found.')
    for path in paths: print(f'{path.name}: {check(path)} files checked')
