from pathlib import Path
import subprocess
import tempfile
from ..posix.capture import monitors, capture_rect, image_result


def codex_windows():
    import Quartz
    from ...codex.desktop import discover_desktop
    pid = discover_desktop().pid
    items = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    return [{'id':int(x['kCGWindowNumber']), 'pid':pid, 'title':x.get('kCGWindowName','Codex')}
            for x in items if x.get('kCGWindowOwnerPID') == pid and x.get('kCGWindowLayer') == 0]


def capture(target):
    import Quartz
    if hasattr(Quartz, 'CGPreflightScreenCaptureAccess') and not Quartz.CGPreflightScreenCaptureAccess():
        raise RuntimeError('Allow Screen Recording for the Robot Python interpreter in macOS System Settings, then restart the service.')
    if target['kind'] != 'codex':
        if target['rect'] not in [x['rect'] for x in monitors()]:
            raise RuntimeError('Display layout changed; refresh screenshots.')
        return capture_rect(target['rect'])
    if not any(x['id']==target['id'] and x['pid']==target['pid'] for x in codex_windows()):
        raise RuntimeError('Desktop window changed; refresh screenshots.')
    with tempfile.TemporaryDirectory(prefix='codex-capture-') as folder:
        output = Path(folder)/'capture.png'
        subprocess.run(['/usr/sbin/screencapture','-x','-l',str(target['id']),str(output)],
                       check=True, timeout=18, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return image_result(output.read_bytes())
