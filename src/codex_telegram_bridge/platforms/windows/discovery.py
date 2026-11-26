from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import ctypes,os,struct
from ctypes import wintypes
import mmap
from pathlib import Path
from ...codex.errors import DesktopBridgeSafetyError,DesktopIpcUnavailable


def _desktop_environment_values(start: int, read) -> dict[str, str]:
    raw = bytearray()
    limit = 256 * 1024
    while len(raw) < limit:
        offset = len(raw)
        address = start + offset
        # The terminator can be in the last readable page. Windows can reject
        # an entire read that extends even one byte into the following page.
        page_remaining = mmap.PAGESIZE - address % mmap.PAGESIZE
        chunk = read(address, min(1024, page_remaining, limit - offset))
        if not chunk or len(chunk) % 2:
            break
        raw.extend(chunk)
        # Include the preceding UTF-16 code unit for a split double NUL.
        tail = raw[max(0, offset - 2):].decode("utf-16-le", errors="surrogatepass")
        if "\0\0" in tail:
            block = raw.decode("utf-16-le", errors="surrogatepass").partition("\0\0")[0]
            values = {}
            for line in block.split("\0"):
                key, _, value = line.partition("=")
                if key.upper() in ("CODEX_HOME", "CODEX_APP_TOOLS_PIPE_PATH", "USERPROFILE", "HOME", "CODEX_SQLITE_HOME"):
                    values[key.upper()] = value
            return values
    raise DesktopBridgeSafetyError(_t('Desktop 环境不完整，未提交消息。'))


def _process_environment(pid: int) -> dict[str, str]:
    """Read a 64-bit Desktop's environment; return only profile/pipe values.

    Keep the Windows PEB layout isolated here. A changed layout, denied read,
    or truncated environment must fail closed instead of guessing a profile.
    """
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise DesktopBridgeSafetyError(_t('无法验证 Desktop 环境，请使用 64 位 Windows Python。'))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t),
                    ("reserved3", ctypes.c_void_p)]

    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = wintypes.LONG
    handle = kernel32.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        raise DesktopBridgeSafetyError(_t('无法验证 Desktop 使用的环境目录。'))
    try:
        def read(address: int, size: int) -> bytes:
            buffer = ctypes.create_string_buffer(size)
            received = ctypes.c_size_t()
            ok = kernel32.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(received))
            if not ok and not received.value:
                raise DesktopBridgeSafetyError(_t('读取 Desktop 环境失败，未提交消息。'))
            return buffer.raw[:received.value]

        def pointer(address: int) -> int:
            raw = read(address, 8)
            if len(raw) != 8:
                raise DesktopBridgeSafetyError(_t('Desktop 环境指针不完整，未提交消息。'))
            value = struct.unpack("<Q", raw)[0]
            if value < 0x10000:
                raise DesktopBridgeSafetyError(_t('Desktop 环境指针无效，未提交消息。'))
            return value

        info = BasicInformation()
        size = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size))
        if status != 0 or not info.peb:
            raise DesktopBridgeSafetyError(_t('无法读取 Desktop 环境信息，未提交消息。'))
        parameters = pointer(int(info.peb) + 0x20)
        environment = pointer(parameters + 0x80)
        return _desktop_environment_values(environment, read)
    finally:
        kernel32.CloseHandle(handle)


def validate_pipe_server(handle: int, expected_pid: int) -> None:
    if os.name != "nt":
        raise DesktopBridgeSafetyError(_t('无法验证 Desktop 连接进程。'))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or pid.value != expected_pid:
        raise DesktopBridgeSafetyError(_t('Desktop 连接进程已改变或不属于当前环境，未提交消息。'))


def desktop_roots():
    import psutil
    records = []
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() != 'chatgpt.exe':
            continue
        try:
            executable = process.exe()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise DesktopBridgeSafetyError(_t('无法验证 Desktop 连接进程。')) from exc
        if not executable:
            raise DesktopBridgeSafetyError(_t('无法验证 Desktop 连接进程。'))
        path = Path(executable).resolve()
        if (path.name.lower() != 'chatgpt.exe' or path.parent.name.lower() != 'app'
                or not path.parent.parent.name.lower().startswith('openai.codex_')
                or path.parent.parent.parent.name.lower() != 'windowsapps'):
            continue
        try:
            args = process.cmdline()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise DesktopBridgeSafetyError(_t('无法验证 Desktop 连接进程。')) from exc
        if not args:
            raise DesktopBridgeSafetyError(_t('无法验证 Desktop 连接进程。'))
        if not any(arg == '--type' or arg.startswith('--type=') for arg in args):
            records.append({'ProcessId': process.pid, 'ExecutablePath': str(path)})
    return records
