"""Release source ZIP only after an exact real delivery/receipt acceptance."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from check_distribution import audit
from packaging.version import Version

ROOT=Path(__file__).resolve().parents[1]


def source_files():
    names=['README.md','README.en.md','LICENSE','SECURITY.md','CHANGELOG.md','pyproject.toml','MANIFEST.in','install.py']
    # Explicit source roots and suffixes; no developer config, state or logs.
    for folder, suffixes in [('src',{'.py','.json'}),('tests',{'.py'}),('tools',{'.py'}),('docs',{'.md'})]:
        names.extend(p.relative_to(ROOT).as_posix() for p in (ROOT/folder).rglob('*')
                     if p.is_file() and p.suffix in suffixes and '__pycache__' not in p.parts
                     and '.egg-info' not in str(p))
    return sorted(set(names))


def source_version(content):
    """Read the captured package literal without importing an installed copy."""
    tree = ast.parse(content['src/codex_telegram_bridge/__init__.py'])
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
    if len(values) != 1:
        raise ValueError('Expected one package __version__ literal.')
    version = ast.literal_eval(values[0])
    if not isinstance(version, str) or str(Version(version)) != version:
        raise ValueError('Package __version__ must be a canonical version string.')
    return version


def write_source_archive(content, dist):
    """Write a previously accepted snapshot and its derived compatibility file."""
    version = source_version(content)
    name = 'codex-telegram-bridge-v' + version
    dist.mkdir(parents=True, exist_ok=True)
    output = dist/(name+'.zip')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, data in content.items():
            if path != 'VERSION':
                archive.writestr(name+'/'+path, data)
        archive.writestr(name+'/VERSION', version+'\n')
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread-id',required=True)
    parser.add_argument('--credentials',type=Path)
    parser.add_argument('--codex-home',type=Path)
    args=parser.parse_args()
    files=source_files();content={n:(ROOT/n).read_bytes() for n in files};audit(content.items())
    source_version(content)
    hashes={n:hashlib.sha256(data).hexdigest() for n,data in content.items()}
    validation=ROOT/'.validation';validation.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='live-',suffix='.json',dir=validation,delete=False) as f:report_path=Path(f.name)
    command=[sys.executable,str(ROOT/'tools/verify_connection.py'),'--thread-id',args.thread_id,'--send-probe','--report',str(report_path)]
    if args.credentials:command+=['--credentials',str(args.credentials)]
    if args.codex_home:command+=['--codex-home',str(args.codex_home)]
    subprocess.run(command,check=True,cwd=ROOT)
    report=json.loads(report_path.read_text(encoding='utf-8'))
    if not (report.get('success') and report.get('desktop_attempts')==1
            and report.get('desktop_route')=='desktop-user-input' and report.get('receipt_origin')=='user'
            and report.get('thread_id')==args.thread_id
            and report.get('checks',{}).get('desktop_delivery')=='passed'
            and report.get('checks',{}).get('telegram_delivery')=='passed'):
        raise RuntimeError('Incomplete delivery evidence; release stopped.')
    subprocess.run([sys.executable,str(ROOT/'tools/run_tests.py'),'--suite','Full'],check=True,cwd=ROOT)
    if source_files()!=files or any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=hashes[n] for n in files):
        raise RuntimeError('Sources changed during acceptance; run again.')
    output=write_source_archive(content, ROOT/'dist')
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    output.with_suffix('.validation.json').write_text(json.dumps({'live':report,'sourceHashes':hashes,'sha256':digest},indent=2),encoding='utf-8')
    print(str(output)+'\nSHA256: '+digest)


if __name__=='__main__':raise SystemExit(main())
