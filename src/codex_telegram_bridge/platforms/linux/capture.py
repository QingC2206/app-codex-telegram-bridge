"""X11 visible window regions; Wayland uses the user-consent desktop portal."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, unquote
import uuid
from ..posix.capture import monitors as x11_monitors, capture_rect, image_result


def wayland():
    return bool(os.environ.get('WAYLAND_DISPLAY'))


def monitors():
    return [{'id':1,'portal':True}] if wayland() else x11_monitors()


def codex_windows():
    if wayland():
        return []  # The compositor, not a background client, chooses the view.
    from Xlib import display
    from ...codex.desktop import discover_desktop
    pid = discover_desktop().pid
    connection = display.Display()
    try:
        root = connection.screen().root
        windows = root.get_full_property(connection.intern_atom('_NET_CLIENT_LIST'), 0)
        result = []
        for ident in (windows.value if windows else []):
            window = connection.create_resource_object('window', int(ident))
            owner = window.get_full_property(connection.intern_atom('_NET_WM_PID'), 0)
            if owner is None or int(owner.value[0]) != pid:
                continue
            geometry = window.get_geometry()
            position = root.translate_coords(window, 0, 0)
            result.append({'id':int(ident), 'pid':pid, 'visible_region':True,
                           'rect':[position.x,position.y,position.x+geometry.width,position.y+geometry.height]})
        return result
    finally:
        connection.close()


async def _portal():
    from dbus_next.aio import MessageBus
    from dbus_next import Message, MessageType, Variant
    bus = await MessageBus().connect()
    token = 'codex_bridge_'+uuid.uuid4().hex
    sender = bus.unique_name[1:].replace('.','_')
    request_path = '/org/freedesktop/portal/desktop/request/'+sender+'/'+token
    future = asyncio.get_running_loop().create_future()
    def received(message):
        if (message.message_type == MessageType.SIGNAL and message.path == request_path
                and message.interface == 'org.freedesktop.portal.Request' and message.member == 'Response'
                and not future.done()):
            future.set_result(message.body)
    bus.add_message_handler(received)
    try:
        await bus.call(Message(destination='org.freedesktop.DBus',path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus',member='AddMatch',signature='s',
            body=["type='signal',interface='org.freedesktop.portal.Request',path='"+request_path+"'"]))
        reply = await bus.call(Message(destination='org.freedesktop.portal.Desktop',path='/org/freedesktop/portal/desktop',
            interface='org.freedesktop.portal.Screenshot',member='Screenshot',signature='sa{sv}',
            body=['',{'handle_token':Variant('s',token),'interactive':Variant('b',True)}]))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError('Screenshot portal unavailable. Use an active desktop session with xdg-desktop-portal.')
        status, results = await asyncio.wait_for(future, 50)
        if status != 0:
            raise RuntimeError('Screenshot was cancelled or denied on the Linux desktop.')
        uri = urlsplit(results['uri'].value)
        if uri.scheme != 'file' or uri.netloc not in ('','localhost'):
            raise RuntimeError('Screenshot portal returned a non-local image.')
        path = Path(unquote(uri.path))
        if not path.is_absolute() or path.stat().st_size > 49*1024*1024:
            raise RuntimeError('Invalid screenshot portal output.')
        return image_result(path.read_bytes())
    finally:
        bus.disconnect()


def capture(target):
    if wayland():
        return asyncio.run(_portal())
    if target['kind'] == 'codex':
        current = next((x for x in codex_windows() if x['id']==target['id'] and x['pid']==target['pid']),None)
        if current is None:
            raise RuntimeError('Desktop window changed; refresh screenshots.')
        return capture_rect(current['rect'])
    if target['rect'] not in [x['rect'] for x in monitors()]:
        raise RuntimeError('Display layout changed; refresh screenshots.')
    return capture_rect(target['rect'])


def failure_hint():
    from ...i18n import tr as _t
    return _t('截图失败。请检查当前桌面会话的屏幕共享权限和截图服务。')
