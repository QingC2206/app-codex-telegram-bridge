"""Local Windows screenshots. Standard library only; no model or clipboard use."""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
from codex_telegram_bridge.i18n import get_language, using_language
import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import zlib

MAX_PIXELS = 64_000_000


class Placement(c.Structure):
    _fields_ = [('length', w.UINT), ('flags', w.UINT), ('showCmd', w.UINT),
                ('ptMinPosition', w.POINT), ('ptMaxPosition', w.POINT), ('rcNormalPosition', w.RECT)]


class BitmapInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                ('imageSize', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                ('used', w.DWORD), ('important', w.DWORD)]


def api():
    if sys.platform != 'win32':
        raise RuntimeError(_t('截图仅支持 Windows 交互桌面。'))
    u, g, k = c.WinDLL('user32', use_last_error=True), c.WinDLL('gdi32', use_last_error=True), c.WinDLL('kernel32', use_last_error=True)
    signatures = [
        (u, 'GetDC', [w.HWND], w.HDC), (u, 'ReleaseDC', [w.HWND, w.HDC], c.c_int),
        (u, 'PrintWindow', [w.HWND, w.HDC, w.UINT], w.BOOL),
        (u, 'GetWindowRect', [w.HWND, c.POINTER(w.RECT)], w.BOOL),
        (u, 'GetWindowPlacement', [w.HWND, c.POINTER(Placement)], w.BOOL),
        (u, 'SetWindowPlacement', [w.HWND, c.POINTER(Placement)], w.BOOL),
        (u, 'ShowWindow', [w.HWND, c.c_int], w.BOOL),
        (u, 'SetWindowPos', [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT], w.BOOL),
        (u, 'IsWindowVisible', [w.HWND], w.BOOL), (u, 'IsIconic', [w.HWND], w.BOOL),
        (u, 'GetWindowLongPtrW', [w.HWND, c.c_int], c.c_ssize_t),
        (u, 'GetWindowTextW', [w.HWND, w.LPWSTR, c.c_int], c.c_int),
        (u, 'GetClassNameW', [w.HWND, w.LPWSTR, c.c_int], c.c_int),
        (u, 'GetWindowThreadProcessId', [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
        (u, 'SetThreadDpiAwarenessContext', [c.c_void_p], c.c_void_p),
        (u, 'OpenInputDesktop', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (u, 'CloseDesktop', [w.HANDLE], w.BOOL),
        (g, 'CreateCompatibleDC', [w.HDC], w.HDC),
        (g, 'CreateCompatibleBitmap', [w.HDC, c.c_int, c.c_int], w.HBITMAP),
        (g, 'SelectObject', [w.HDC, w.HANDLE], w.HANDLE),
        (g, 'DeleteObject', [w.HANDLE], w.BOOL), (g, 'DeleteDC', [w.HDC], w.BOOL),
        (g, 'BitBlt', [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.HDC, c.c_int, c.c_int, w.DWORD], w.BOOL),
        (g, 'GetDIBits', [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.POINTER(BitmapInfo), w.UINT], c.c_int),
        (k, 'OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (k, 'QueryFullProcessImageNameW', [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL),
        (k, 'CloseHandle', [w.HANDLE], w.BOOL),
    ]
    for dll, name, args, result in signatures:
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return u, g, k


@contextmanager
def physical_pixels(u):
    old = u.SetThreadDpiAwarenessContext(c.c_void_p(-4))
    try:
        yield
    finally:
        if old:
            u.SetThreadDpiAwarenessContext(old)


def monitors():
    u, _, _ = api()
    items = []
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
    @callback_type
    def visit(handle, dc, rect, data):
        r = rect.contents
        items.append({'rect': [r.left, r.top, r.right, r.bottom]})
        return True
    with physical_pixels(u):
        if not u.EnumDisplayMonitors(None, None, visit, 0):
            raise RuntimeError(_t('无法枚举显示器。'))
    items.sort(key=lambda item: (item['rect'][0], item['rect'][1]))
    for index, item in enumerate(items, 1):
        item['id'] = index
    return items


def codex_windows():
    u, _, k = api()
    items = []
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    @callback_type
    def visit(hwnd, data):
        if not u.IsWindowVisible(hwnd) or u.GetWindowLongPtrW(hwnd, -20) & 0x80:
            return True
        title, cls = c.create_unicode_buffer(512), c.create_unicode_buffer(128)
        u.GetWindowTextW(hwnd, title, len(title))
        u.GetClassNameW(hwnd, cls, len(cls))
        if cls.value != 'Chrome_WidgetWin_1':
            return True
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        process = k.OpenProcess(0x1000, False, pid.value)
        if not process:
            return True
        try:
            path, size = c.create_unicode_buffer(32768), w.DWORD(32768)
            if k.QueryFullProcessImageNameW(process, 0, path, c.byref(size)) and is_codex_executable(path.value):
                items.append({'hwnd': int(hwnd), 'pid': pid.value, 'title': title.value or 'Codex', 'minimized': bool(u.IsIconic(hwnd))})
        finally:
            k.CloseHandle(process)
        return True
    u.EnumWindows(visit, 0)
    return items


def is_codex_executable(path):
    # The Store build's UI process is ChatGPT.exe inside OpenAI.Codex_*.
    value = path.replace('\\', '/').lower()
    return value.endswith('/codex.exe') or (value.endswith('/chatgpt.exe') and
        any(part.startswith('openai.codex_') for part in value.split('/')))


def encode_png(width, height, bgra):
    if width <= 0 or height <= 0 or width*height > MAX_PIXELS or len(bgra) != width*height*4:
        raise ValueError(_t('截图尺寸无效或超过 6400 万像素。'))
    rgb = bytearray(width*height*3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    compressor = zlib.compressobj(6)
    compressed = bytearray()
    stride = width*3
    for y in range(height):
        compressed.extend(compressor.compress(b'\0'+rgb[y*stride:(y+1)*stride]))
    compressed.extend(compressor.flush())
    def chunk(kind, data):
        return struct.pack('!I', len(data))+kind+data+struct.pack('!I', zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))+chunk(b'IDAT', bytes(compressed))+chunk(b'IEND', b'')


def bitmap(rect, hwnd=0):
    u, g, _ = api()
    left, top, right, bottom = rect
    width, height = right-left, bottom-top
    if width <= 0 or height <= 0 or width*height > MAX_PIXELS:
        raise ValueError(_t('截图尺寸无效或超过 6400 万像素，请选择单个显示器。'))
    screen = u.GetDC(None)
    dc = g.CreateCompatibleDC(screen)
    bmp = g.CreateCompatibleBitmap(screen, width, height)
    if not screen or not dc or not bmp:
        if bmp: g.DeleteObject(bmp)
        if dc: g.DeleteDC(dc)
        if screen: u.ReleaseDC(None, screen)
        raise RuntimeError(_t('无法分配截图资源。'))
    old = g.SelectObject(dc, bmp)
    try:
        # Window capture NEVER falls back to cropping the desktop.
        ok = u.PrintWindow(hwnd, dc, 2) if hwnd else g.BitBlt(dc, 0, 0, width, height, screen, left, top, 0x40CC0020)
        if not ok:
            raise RuntimeError(_t('窗口未能绘制截图。请确认电脑已解锁，Codex 正常运行。'))
        g.SelectObject(dc, old)
        info = BitmapInfo(c.sizeof(BitmapInfo), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = c.create_string_buffer(width*height*4)
        if g.GetDIBits(dc, bmp, 0, height, pixels, c.byref(info), 0) != height:
            raise RuntimeError(_t('读取截图像素失败。'))
        data = pixels.raw
        if hwnd and len(set(data[0::4])) < 2 and len(set(data[1::4])) < 2 and len(set(data[2::4])) < 2:
            raise RuntimeError(_t('Codex 返回空白截图，未发送；请稍后重试。'))
        return encode_png(width, height, data), width, height
    finally:
        g.SelectObject(dc, old)
        g.DeleteObject(bmp)
        g.DeleteDC(dc)
        u.ReleaseDC(None, screen)


def desktop_rect(items):
    if not items:
        raise RuntimeError(_t('未发现可用显示器。'))
    return [min(x['rect'][0] for x in items), min(x['rect'][1] for x in items),
            max(x['rect'][2] for x in items), max(x['rect'][3] for x in items)]


def capture(target):
    """Bound PrintWindow in a child process; parent always restores minimized state."""
    u, _, _ = api()
    desktop = u.OpenInputDesktop(0, False, 0x100)
    if not desktop:
        raise RuntimeError(_t('电脑已锁屏或交互桌面不可用，无法截图。'))
    u.CloseDesktop(desktop)
    hwnd, placement = 0, None
    with physical_pixels(u):
        if target['kind'] == 'codex':
            match = next((x for x in codex_windows() if x['hwnd'] == target['hwnd'] and x['pid'] == target['pid']), None)
            if not match:
                raise RuntimeError(_t('Codex 窗口已关闭或变化，请刷新截图菜单。'))
            hwnd = match['hwnd']
            if u.IsIconic(hwnd):
                placement = Placement()
                placement.length = c.sizeof(placement)
                if not u.GetWindowPlacement(hwnd, c.byref(placement)):
                    raise RuntimeError(_t('无法保存窗口状态，已取消截图。'))
        try:
            if placement is not None:
                # Restore without activation, behind other windows. Capture uses
                # PrintWindow, so occluding applications never enter the image.
                u.ShowWindow(hwnd, 4)
                u.SetWindowPos(hwnd, w.HWND(1), 0, 0, 0, 0, 0x0013)
                time.sleep(0.6)
            with tempfile.TemporaryDirectory(prefix='codex-screenshot-') as folder:
                path = Path(folder)/'capture.png'
                result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker', json.dumps({**target, '_language':get_language()}), str(path)],
                    capture_output=True, text=True, encoding='utf-8', timeout=18, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise RuntimeError(result.stdout.strip() or _t('截图进程失败。'))
                metadata = json.loads(result.stdout)
                return path.read_bytes(), metadata
        except subprocess.TimeoutExpired:
            raise RuntimeError(_t('截图超时，窗口状态已还原；请稍后重试。')) from None
        finally:
            if placement is not None:
                u.SetWindowPlacement(hwnd, c.byref(placement))


def worker(target, path):
    u, _, _ = api()
    with physical_pixels(u):
        if target['kind'] == 'codex':
            hwnd = target['hwnd']
            if not any(x['hwnd'] == hwnd and x['pid'] == target['pid'] for x in codex_windows()):
                raise RuntimeError(_t('Codex 窗口已关闭。'))
            rect = w.RECT()
            if not u.GetWindowRect(hwnd, c.byref(rect)):
                raise RuntimeError(_t('读取窗口尺寸失败。'))
            box = [rect.left, rect.top, rect.right, rect.bottom]
            data, width, height = bitmap(box, hwnd)
        else:
            items = monitors()
            box = desktop_rect(items) if target['kind'] == 'desktop' else target['rect']
            if target['kind'] == 'monitor' and not any(x['rect'] == box for x in items):
                raise RuntimeError(_t('显示器布局已变化，请刷新菜单。'))
            data, width, height = bitmap(box)
        Path(path).write_bytes(data)
        return {'width': width, 'height': height, 'rect': box}


if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--worker':
            target = json.loads(sys.argv[2])
            with using_language(target.pop('_language', 'zh-CN')):
                print(json.dumps(worker(target, sys.argv[3])))
        else:
            print(json.dumps({'monitors': monitors(), 'codex': codex_windows()}, ensure_ascii=True))
    except Exception as exc:
        print(str(exc))
        sys.exit(1)
