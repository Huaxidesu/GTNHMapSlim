#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用 Windows 原生文件夹选择框，把结果打印到标准输出。

由服务进程以独立子进程方式启动：原生对话框需要自己的消息循环，
放在独立的 COM 单线程套间中执行，避免影响 HTTP 处理线程。

成功时输出一行绝对路径；用户取消时无输出且返回码为 1。
仅依赖标准库 ctypes，不使用 tkinter（部分 Python 发行版未附带）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

BIF_RETURNONLYFSDIRS = 0x0001
BIF_EDITBOX = 0x0010
BIF_NEWDIALOGSTYLE = 0x0040
COINIT_APARTMENTTHREADED = 0x2


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


def pick(title: str) -> str | None:
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL

    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

    display = ctypes.create_unicode_buffer(1024)
    info = BROWSEINFOW(None, None, ctypes.cast(display, wintypes.LPWSTR), title,
                       BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE,
                       None, 0, 0)

    pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
    if not pidl:
        return None

    path = ctypes.create_unicode_buffer(1024)
    ok = shell32.SHGetPathFromIDListW(pidl, path)
    ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    return path.value if ok else None


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
