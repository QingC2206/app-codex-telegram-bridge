from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import ctypes,os,struct
from ctypes import wintypes
from pathlib import Path
from ...codex.errors import DesktopBridgeSafetyError,DesktopIpcUnavailable

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
        raw = bytearray()
        for _ in range(256):
            chunk = read(environment + len(raw), 1024)
            if not chunk or len(chunk) % 2:
                break
            raw.extend(chunk)
            decoded = raw.decode("utf-16-le", errors="surrogatepass")
            if "\0\0" in decoded:
                values = {}
                for line in decoded.split("\0\0", 1)[0].split("\0"):
                    key, _, value = line.partition("=")
                    if key.upper() in ("CODEX_HOME", "CODEX_APP_TOOLS_PIPE_PATH", "USERPROFILE", "HOME"):
                        values[key.upper()] = value
                return values
        raise DesktopBridgeSafetyError(_t('Desktop 环境不完整，未提交消息。'))
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
    for process in psutil.process_iter(['pid', 'exe', 'cmdline']):
        info = process.info
        if not info['exe']:
            continue
        path = Path(info['exe']).resolve()
        if (path.name.lower() == 'chatgpt.exe' and path.parent.name.lower() == 'app'
                and path.parent.parent.name.lower().startswith('openai.codex_')
                and path.parent.parent.parent.name.lower() == 'windowsapps'
                and not any(arg.startswith('--type=') for arg in (info['cmdline'] or []))):
            records.append({'ProcessId': info['pid'], 'ExecutablePath': str(path)})
    return records
