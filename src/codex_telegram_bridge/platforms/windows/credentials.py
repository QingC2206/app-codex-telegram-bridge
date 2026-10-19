"""Legacy DPAPI encryption; imported only on Windows."""
from codex_telegram_bridge.i18n import tr as _t
import base64,ctypes,os
from ctypes import wintypes
from typing import Any

class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_function(name: str):
    if os.name != "nt":
        raise RuntimeError(_t('此配置方式仅支持 Windows DPAPI。'))

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = getattr(crypt32, name)
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR if name == "CryptProtectData" else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    return function


def _blob(data: bytes) -> tuple[DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    value = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def _local_free(pointer: Any) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def protect_secret(secret: str) -> str:
    raw = secret.encode("utf-8")
    input_blob, input_buffer = _blob(raw)
    output_blob = DataBlob()
    protect = _dpapi_function("CryptProtectData")
    if not protect(
        ctypes.byref(input_blob),
        "Codex Telegram bot token",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _ = input_buffer  # Keep the backing buffer alive through CryptProtectData.
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        _local_free(output_blob.pbData)


def unprotect_secret(encoded: str) -> str:
    encrypted = base64.b64decode(encoded)
    input_blob, input_buffer = _blob(encrypted)
    output_blob = DataBlob()
    description = wintypes.LPWSTR()
    unprotect = _dpapi_function("CryptUnprotectData")
    if not unprotect(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _ = input_buffer  # Keep the backing buffer alive through CryptUnprotectData.
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        _local_free(output_blob.pbData)
        if description:
            _local_free(description)
