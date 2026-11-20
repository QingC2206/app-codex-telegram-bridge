"""Read the final decision written by the Desktop process that owns the turn."""
from datetime import datetime, timezone
import mmap
import os
from pathlib import Path
import re
import sys
import time


TTL = 47 * 3600
SAFETY = re.compile(rb'^(\S+) info \[cumberbatch\] cumberbatch_safety_decision '
                    rb'sourceThreadId=([0-9a-f-]{36}) sourceTurnId=([0-9a-f-]{36}) suppressed=(true|false)\b',
                    re.MULTILINE)


def auth_stamp(profile):
    try:
        stat = (profile / 'auth.json').stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def log_roots():
    if sys.platform == 'darwin':
        return (Path.home() / 'Library/Logs/com.openai.codex',)
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        if not local or not Path(local).is_absolute():
            return ()
        return (Path(local) / 'Codex/Logs',
                Path(local) / 'Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Codex/Logs')
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return (config / 'Codex/logs',) if config.is_absolute() else ()


def _decisions(log, needle, *, entire_file):
    size = os.fstat(log.fileno()).st_size
    if not size:
        return
    with mmap.mmap(log.fileno(), 0, access=mmap.ACCESS_READ) as data:
        position = 0 if entire_file else max(0, size - 1024 * 1024)
        if position:
            first_newline = data.find(b'\n', position)
            if first_newline < 0:
                return
            position = first_newline + 1
        while (hit := data.find(needle, position)) >= 0:
            start = data.rfind(b'\n', 0, hit) + 1
            row = SAFETY.match(data, start)
            if row is not None and hit < row.end():
                yield row[1], row[4]
            position = hit + len(needle)


def approved_in_log(pid, key, requested, *, callback=False):
    now = time.time()
    if not 0 <= now - requested <= TTL:
        return False
    dates = {dt.strftime('%Y/%m/%d') for ts in (requested, (requested + now)/2, now)
             for dt in (datetime.fromtimestamp(ts), datetime.fromtimestamp(ts, timezone.utc))}
    needle = ('[cumberbatch] cumberbatch_safety_decision sourceThreadId=' + key[0]
              + ' sourceTurnId=' + key[1] + ' suppressed=').encode('ascii')
    found = 0
    for root in log_roots():
        for day in dates:
            for file in (root/day).glob(f'codex-desktop-*-{pid}-t0-i*-*.log'):
                try:
                    with file.open('rb') as log:
                        for raw_time, suppressed in _decisions(log, needle, entire_file=callback):
                            stamp = datetime.fromisoformat(raw_time.decode().replace('Z', '+00:00')).timestamp()
                            if not requested <= stamp <= now or suppressed != b'false' or found:
                                return False
                            found += 1
                except (OSError, ValueError, OverflowError):
                    return False
    return found == 1
