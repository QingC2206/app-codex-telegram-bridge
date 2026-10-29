"""One reversible installation transaction for both execution backends."""
from datetime import datetime, timezone
import os
import uuid

from ..paths import ensure_data_dir
from . import services


def atomic_write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        temporary.write_bytes(data);temporary.chmod(0o600);os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()


def install_files(changes, *, service=True, originals=None):
    """Validate service ownership, back up files, then install or restore all."""
    originals = originals if originals is not None else {
        path: path.read_bytes() if path.exists() else None for path in changes}
    service_before = services.snapshot() if service else None
    backup = ensure_data_dir()/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True)
    for path, content in originals.items():
        if content is not None:
            atomic_write(backup/path.name, content)
    service_changed = False
    try:
        for path, content in changes.items():
            if content == originals.get(path):
                continue
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content)
        if service:
            service_changed = True
            services.install_and_start()
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content)
        if service_changed:
            try:
                services.restore(service_before)
            except Exception as restore_error:
                raise RuntimeError(f'Installation failed and service rollback failed; backups: {backup}') from restore_error
        raise
    return backup
