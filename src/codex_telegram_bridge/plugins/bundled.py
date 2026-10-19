"""Install/update explicitly packaged plugins, preserving locally modified copies."""
from codex_telegram_bridge.i18n import tr as _t
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid


def tree_digest(folder):
    digest=hashlib.sha256()
    for path in sorted(folder.rglob('*')):
        if '__pycache__' in path.parts or not path.is_file():continue
        if not path.resolve().is_relative_to(folder.resolve()):raise ValueError(_t('插件文件链接越界'))
        digest.update(path.relative_to(folder).as_posix().encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def install_bundled(package, root, state):
    package,root,state=map(Path,(package,root,state))
    catalog=json.loads((package/'bundled_plugins.json').read_text(encoding='utf-8-sig'))
    root.mkdir(parents=True,exist_ok=True);state.mkdir(parents=True,exist_ok=True)
    receipt=state/'bundled-install.json'
    installed=json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
    notes=[]
    def checked(ident):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',ident):raise ValueError(_t('非法随包插件 ID'))
        target=root/ident
        if target.exists() and target.resolve().parent!=root.resolve():raise ValueError(_t('插件目录越界'))
        return target
    def backup(target):
        destination=state/'bundled-backups'/(target.name+'-'+uuid.uuid4().hex)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if target.resolve().parent!=root.resolve() or not destination.resolve().is_relative_to(state.resolve()):raise ValueError(_t('备份路径越界'))
        shutil.move(str(target),str(destination))
        return destination
    for ident in catalog.get('retired',[]):
        target=checked(ident)
        if target.exists():backup(target);notes.append(ident+_t(' 已移除（代码已备份）'))
        installed.pop(ident,None)
    for ident,files in catalog['plugins'].items():
        target=checked(ident);source=package/'bundled_plugins'/ident
        stage=Path(tempfile.mkdtemp(prefix='.bundle-',dir=root))
        previous=None
        try:
            for name in files:
                relative=Path(name)
                if relative.is_absolute() or '..' in relative.parts:raise ValueError(_t('非法插件文件路径'))
                src=source/relative
                if not src.resolve().is_relative_to(source.resolve()):raise ValueError(_t('随包文件越界'))
                dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
            new=tree_digest(stage)
            if target.exists():
                current=tree_digest(target)
                if current==new:installed[ident]=new;continue
                if installed.get(ident)!=current:
                    notes.append(ident+_t(' 存在本地修改，保留本地版本；随包新版位于程序目录 bundled_plugins'));continue
                previous=backup(target)
            stage.rename(target);installed[ident]=new
        except Exception:
            if previous and not target.exists():shutil.move(str(previous),str(target))
            raise
        finally:
            if stage.exists():
                if stage.resolve().parent!=root.resolve():raise ValueError(_t('暂存目录越界'))
                shutil.rmtree(stage)
    temp=receipt.with_suffix('.tmp');temp.write_text(json.dumps(installed),encoding='utf-8');os.replace(temp,receipt)
    return notes
