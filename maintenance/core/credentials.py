"""Windows DPAPI primitives shared by independent credential formats."""

import ctypes
import os
from ctypes import wintypes


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _input_blob(value: bytes):
    buffer = ctypes.create_string_buffer(value)
    return _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect(
    value: bytes,
    *,
    entropy_value: bytes = b"7u:model-config:v1",
    description_value: str = "7u model configuration",
) -> bytes:
    if os.name != "nt":
        raise RuntimeError("7u secure model storage currently requires Windows")
    source, source_buffer = _input_blob(value)
    entropy, entropy_buffer = _input_blob(entropy_value)
    output = _Blob()
    crypt = ctypes.windll.crypt32.CryptProtectData
    crypt.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    crypt.restype = wintypes.BOOL
    if not crypt(
        ctypes.byref(source), description_value, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer, entropy_buffer


def unprotect(value: bytes, *, entropy_value: bytes = b"7u:model-config:v1") -> bytes:
    if os.name != "nt":
        raise RuntimeError("7u secure model storage currently requires Windows")
    source, source_buffer = _input_blob(value)
    entropy, entropy_buffer = _input_blob(entropy_value)
    output = _Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    crypt.restype = wintypes.BOOL
    description = wintypes.LPWSTR()
    if not crypt(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        0x1,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            ctypes.windll.kernel32.LocalFree(description)
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buffer, entropy_buffer
