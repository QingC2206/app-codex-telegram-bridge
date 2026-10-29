"""Desktop access policy selection; protocol orchestration stays OS independent."""
import sys

from ..codex.errors import DesktopBridgeSafetyError, DesktopIpcInactive
from ..i18n import tr as _t


class NativeDesktopAccess:
    """Live App Tools checks shared by platforms that expose that interface."""

    recover_discovery_unavailable = False

    def reader(self, transport_factory, timeout, discover):
        from ..codex.desktop import DesktopAppTools
        return DesktopAppTools(transport_factory, timeout, discover)

    def validate_reply(self, endpoint, thread_id, reader, *, start_if_inactive,
                       expected_turn_id, check_instance):
        state = reader.read_thread(thread_id)
        thread = state.get('thread')
        if not isinstance(thread, dict) or thread.get('id') != thread_id:
            raise DesktopBridgeSafetyError(_t('Desktop 返回的任务与目标不一致，未提交消息。'))
        if not start_if_inactive or expected_turn_id:
            status = thread.get('status')
            if not isinstance(status, dict) or status.get('type') != 'active':
                raise DesktopIpcInactive(_t('该 Desktop 任务当前没有运行中的 Turn。'))
        if expected_turn_id:
            turns = state.get('turns')
            if (not isinstance(turns, list) or not turns or not isinstance(turns[0], dict)
                    or turns[0].get('id') != expected_turn_id):
                raise DesktopIpcInactive(_t('Desktop 当前 Turn 与原问题不一致，未提交回答。'))

    def open_thread(self, endpoint, thread_id, reader, *, deadline, check_instance):
        reader.open_thread(thread_id, deadline=deadline, before_open=check_instance)


def desktop_access(platform=None):
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        from .macos.desktop_access import MacOSDesktopAccess
        return MacOSDesktopAccess()
    if platform == 'win32':
        from .windows.desktop_access import WindowsDesktopAccess
        return WindowsDesktopAccess()
    return NativeDesktopAccess()
