"""macOS Desktop access: verified local ownership and existing-thread deep links."""
from pathlib import Path
import subprocess
import time

from ..desktop_access import NativeDesktopAccess
from ..discovery import desktop_roots
from ..local_thread import validate_local_desktop_thread
from ...codex.errors import DesktopBridgeSafetyError
from ...i18n import tr as _t


class MacOSDesktopAccess(NativeDesktopAccess):
    # An unloaded renderer can leave discovery unanswered on macOS. Opening
    # the verified existing task is safe before any user-input frame is sent.
    recover_discovery_unavailable = True

    def reader(self, transport_factory, timeout, discover):
        from ...codex.follower import FollowerReader
        return FollowerReader(transport_factory, timeout, discover,
                              validate_thread=validate_local_desktop_thread)

    def validate_reply(self, endpoint, thread_id, reader, *, start_if_inactive,
                       expected_turn_id, check_instance):
        if start_if_inactive and not expected_turn_id:
            validate_local_desktop_thread(endpoint, thread_id)
            check_instance()
            return
        super().validate_reply(endpoint, thread_id, reader,
            start_if_inactive=start_if_inactive, expected_turn_id=expected_turn_id,
            check_instance=check_instance)

    def open_thread(self, endpoint, thread_id, reader, *, deadline, check_instance):
        # Only load the existing task. No prompt or new-task fallback is encoded.
        validate_local_desktop_thread(endpoint, thread_id)
        records = [r for r in desktop_roots() if int(r['ProcessId']) == endpoint.pid]
        if len(records) != 1:
            raise DesktopBridgeSafetyError(_t('Desktop 在提交前已重启，未发送消息；请重新检查任务。'))
        executable = Path(records[0]['ExecutablePath']).resolve()
        if executable.parent.name != 'MacOS' or executable.parent.parent.name != 'Contents':
            raise DesktopBridgeSafetyError(_t('Desktop 的用户目录存在歧义，未提交消息。'))
        check_instance()
        subprocess.run(['/usr/bin/open', '-a', str(executable.parent.parent.parent),
                        'codex://threads/' + thread_id], check=True,
                       timeout=max(0.1, deadline-time.monotonic()),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
