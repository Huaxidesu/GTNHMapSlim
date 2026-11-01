#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用 Windows 原生文件夹选择框，把结果打印到标准输出。

优先使用 Vista 之后的 IFileOpenDialog（带地址栏与面包屑，可直接粘贴路径），
失败时回退到旧版 SHBrowseForFolderW。两条路径都只用 ctypes，不依赖 tkinter。

由服务进程以独立子进程方式启动：原生模态对话框需要自己的消息循环，
放在独立的 COM 单线程套间中执行，避免影响 HTTP 处理线程。

成功时输出一行绝对路径；用户取消时无输出且返回码为 1。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, byref, c_int, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p
from ctypes import wintypes

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1

# IFileOpenDialog 选项
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_NOCHANGEDIR = 0x00000008

SIGDN_FILESYSPATH = 0x80058000
ERROR_CANCELLED = 0x800704C7

# 旧版对话框选项
BIF_RETURNONLYFSDIRS = 0x0001
BIF_EDITBOX = 0x0010
BIF_NEWDIALOGSTYLE = 0x0040

ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ole32.CLSIDFromString(c_wchar_p(text), byref(self))


def _call(ptr: c_void_p, index: int, restype, *argtypes):
    """取出 COM 接口指针在虚表中第 index 个方法。"""
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


def _pick_modern(title: str) -> str | None:
    """使用 IFileOpenDialog，返回路径；不支持或出错时返回 None。"""
    clsid = GUID("{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}")
    iid = GUID("{d57c7288-d4ad-4768-be02-9d969532d960}")

    dialog = c_void_p()
    hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER,
                                byref(iid), byref(dialog))
    if hr < 0 or not dialog:
        return None

    set_options = _call(dialog, 9, ctypes.HRESULT, ctypes.c_uint)
    get_options = _call(dialog, 10, ctypes.HRESULT, POINTER(ctypes.c_uint))
    set_title = _call(dialog, 17, ctypes.HRESULT, ctypes.c_wchar_p)
    show = _call(dialog, 3, ctypes.HRESULT, wintypes.HWND)
    get_result = _call(dialog, 20, ctypes.HRESULT, POINTER(c_void_p))

    current = ctypes.c_uint(0)
    get_options(dialog, byref(current))
    set_options(dialog, current.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
                | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR)
    set_title(dialog, title)

    if show(dialog, None) != 0:                      # 用户取消或出错
        _call(dialog, 2, c_ulong)(dialog)            # Release
        return None

    item = c_void_p()
    if get_result(dialog, byref(item)) < 0 or not item:
        _call(dialog, 2, c_ulong)(dialog)
        return None

    buffer = c_wchar_p()
    get_name = _call(item, 5, ctypes.HRESULT, c_int, POINTER(c_wchar_p))
    path = None
    if get_name(item, SIGDN_FILESYSPATH, byref(buffer)) >= 0 and buffer.value:
        path = buffer.value
        ole32.CoTaskMemFree(buffer)

    _call(item, 2, c_ulong)(item)
    _call(dialog, 2, c_ulong)(dialog)
    return path


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", c_int),
    ]


def _pick_legacy(title: str) -> str | None:
    """旧版对话框，作为 IFileOpenDialog 不可用时的回退。"""
    shell32.SHBrowseForFolderW.restype = c_void_p
    shell32.SHBrowseForFolderW.argtypes = [POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.argtypes = [c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL

    display = ctypes.create_unicode_buffer(1024)
    info = BROWSEINFOW(None, None, ctypes.cast(display, wintypes.LPWSTR), title,
                       BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE,
                       None, 0, 0)

    pidl = shell32.SHBrowseForFolderW(byref(info))
    if not pidl:
        return None

    path = ctypes.create_unicode_buffer(1024)
    ok = shell32.SHGetPathFromIDListW(pidl, path)
    ole32.CoTaskMemFree(c_void_p(pidl))
    return path.value if ok else None


def pick(title: str) -> str | None:
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        result = _pick_modern(title)
        if result:
            return result
        return _pick_legacy(title)
    finally:
        ole32.CoUninitialize()


def main() -> int:
    if os.name != "nt":
        print("仅支持 Windows", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    result = pick("选择存档目录（包含 region 文件夹的那一层）")
    if not result:
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
