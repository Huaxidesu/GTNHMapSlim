#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GTNHMapSlim 本地图形界面服务。

在回环地址上提供静态页面与 JSON 接口，页面通过接口驱动 gtnh_map_slim.py
的子进程完成扫描与写入。服务仅监听 127.0.0.1，不对外暴露。

设计要点：
  1. 界面层不重复实现扫描与写入逻辑，一律通过命令行调用核心模块，保证单一真相源。
  2. 进度采用轮询而非长连接：http.server 实现 WebSocket 需要自行完成握手，
     轮询在 300 ms 间隔下足以驱动进度条，且无重连问题。
  3. 取消操作终止整个进程树，避免进程池中的工作进程残留并继续写盘。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
TOOL = ROOT / "gtnh_map_slim.py"
PICKER = HERE / "pick_folder.py"

sys.path.insert(0, str(ROOT))
import gtnh_map_slim  # noqa: E402

BACKUP_MARKERS = gtnh_map_slim.BACKUP_MARKERS
DEFAULT_PORT = 8777

# 定位拖入目录时跳过的系统目录，避免遍历整盘
SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "appdata", "recovery",
    "perflogs", "msocache", "node_modules", ".git", ".svn", "config.msi",
}
RESOLVE_VISIT_LIMIT = 40000


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------

def discover(root: Path) -> dict:
    """返回 root 下的存档单元与汇总信息。"""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {root}")
    units = gtnh_map_slim.discover_units(root)
    return {
        "root": str(root),
        "units": units,
        "totals": {
            "dirs": len(units),
            "files": sum(u["files"] for u in units),
            "bytes": sum(u["bytes"] for u in units),
        },
    }


def _drives() -> list[Path]:
    if os.name != "nt":
        return [Path("/")]
    return [Path(f"{letter}:\\") for letter in "CDEFGHIJKL"
            if Path(f"{letter}:\\").exists()]


def resolve_folder(name: str) -> list[str]:
    """按文件夹名在本地磁盘上定位候选路径。

    浏览器出于安全限制不会向页面提供拖入目录的绝对路径，只能提供名称。
    这里在固定驱动器根目录下做受限深度的搜索：系统盘两层，其余盘四层，
    并跳过系统目录与隐藏目录，遍历数量设有上限。
    """
    target = name.casefold()
    hits: list[str] = []
    visited = 0

    for drive in _drives():
        max_depth = 2 if drive.drive.upper() == "C:" else 4
        base_depth = len(drive.parts)
        for dirpath, dirnames, _filenames in os.walk(drive):
            visited += 1
            if visited > RESOLVE_VISIT_LIMIT:
                return hits
            cur = Path(dirpath)
            if cur != drive and cur.name.casefold() == target:
                hits.append(str(cur))
                dirnames[:] = []
                continue
            if len(cur.parts) - base_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if d.casefold() not in SKIP_DIRS
                and not d.startswith(".")
                and not any(marker in d for marker in BACKUP_MARKERS)
            ]
    return hits


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except OSError:
            pass


class Task:
    """一次精简任务，包装核心模块的子进程并汇总结构化进度。"""

    def __init__(self, root: str, units: list[dict], minutes: float,
                 mode: str, backup: bool, jobs: int):
        self.id = uuid.uuid4().hex[:12]
        self.root = root
        self.units = units
        self.minutes = minutes
        self.mode = mode
        self.backup = backup
        self.jobs = jobs

        self.state = "running"          # running / done / cancelled / failed
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = sum(u["bytes"] for u in units)
        self.present = 0
        self.deleted = 0
        self.freed = 0
        self.errors = 0
        self.current = ""
        self.backup_path = ""
        self.message = ""
        self.started = time.time()
        self.ended: float | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False

        self.unit_total = {u["region_dir"]: u["files"] for u in units}
        self.unit_done: dict[str, int] = {u["region_dir"]: 0 for u in units}
        self.unit_deleted: dict[str, int] = {u["region_dir"]: 0 for u in units}
        self.log: deque[str] = deque(maxlen=60)
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self) -> None:
        self.cancel_requested = True
        proc = self.proc
        if proc and proc.poll() is None:
            _kill_tree(proc.pid)

    def _command(self) -> list[str]:
        cmd = [sys.executable, str(TOOL), "--world", self.root, "--apply",
               "--progress-json", "--minutes", f"{self.minutes:g}"]
        if self.mode == "fast":
            cmd.append("--no-compact")
        elif self.mode == "compact-only":
            cmd.append("--compact-only")
        if not self.backup:
            cmd.append("--no-backup")
        if self.jobs:
            cmd += ["--jobs", str(self.jobs)]
        for unit in self.units:
            cmd += ["--only", unit["region_dir"]]
        return cmd

    def _run(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                self._command(), cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags,
            )
        except Exception as exc:
            with self._lock:
                self.state = "failed"
                self.message = f"无法启动子进程: {exc}"
                self.ended = time.time()
            return

        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self._consume(line)
        finally:
            code = self.proc.wait()
            stderr_thread.join(timeout=3)
            for stream in (self.proc.stdout, self.proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

        with self._lock:
            self.ended = time.time()
            if self.cancel_requested:
                self.state = "cancelled"
                self.message = "任务已取消，已完成的文件保持处理结果"
            elif code == 0:
                self.state = "done"
            else:
                self.state = "failed"
                if not self.message:
                    tail = "\n".join(list(self.log)[-4:])
                    self.message = f"核心进程返回 {code}" + (f"\n{tail}" if tail else "")

    def _drain_stderr(self) -> None:
        """读取人类可读输出，仅保留末尾若干行用于报错展示。"""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        buffer = ""
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.replace("\r", "\n")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    with self._lock:
                        self.log.append(line)

    def _consume(self, line: str) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            with self._lock:
                self.log.append(line)
            return

        kind = record.get("t")
        with self._lock:
            if kind == "meta":
                self.files_total = record.get("files", 0)
                self.bytes_total = record.get("bytes", self.bytes_total)
            elif kind == "backup":
                self.backup_path = record.get("path", "")
            elif kind == "file":
                self.files_done += 1
                unit = record.get("unit", "")
                if unit in self.unit_done:
                    self.unit_done[unit] += 1
                    self.unit_deleted[unit] += record.get("deleted", 0)
                self.present += record.get("present") or 0
                self.deleted += record.get("deleted") or 0
                self.freed += record.get("freed") or 0
                if record.get("error"):
                    self.errors += 1
                self.current = record.get("path", "")
            elif kind == "done":
                self.present = record.get("present", self.present)
                self.deleted = record.get("deleted", self.deleted)
                self.freed = record.get("freed", self.freed)
                self.errors = record.get("errors", self.errors)

    # -- 进度快照 ---------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            now = self.ended or time.time()
            elapsed = max(0.0, now - self.started)
            total = self.files_total or sum(self.unit_total.values())
            done = self.files_done
            percent = (done / total * 100.0) if total else 0.0
            speed = done / elapsed if elapsed > 0 and done else 0.0
            eta = ((total - done) / speed) if speed > 0 else 0.0
            units = [{
                "rel": u["rel"],
                "name": u["name"],
                "region_dir": u["region_dir"],
                "files": u["files"],
                "bytes": u["bytes"],
                "done": self.unit_done.get(u["region_dir"], 0),
                "deleted": self.unit_deleted.get(u["region_dir"], 0),
            } for u in self.units]
            return {
                "id": self.id,
                "state": self.state,
                "percent": round(percent, 2),
                "files_total": total,
                "files_done": done,
                "bytes_total": self.bytes_total,
                "present": self.present,
                "deleted": self.deleted,
                "freed": self.freed,
                "errors": self.errors,
                "elapsed": round(elapsed, 2),
                "eta": round(eta, 1),
                "speed": round(speed, 1),
                "current": self.current,
                "backup": self.backup_path,
                "message": self.message,
                "mode": self.mode,
                "minutes": self.minutes,
                "units": units,
                "log": list(self.log)[-12:],
            }


CURRENT: Task | None = None
CURRENT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "GTNHMapSlim"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:      # 静默访问日志
        pass

    # -- 基础 -------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve("index.html")
        if path == "/api/progress":
            task = CURRENT
            if task is None:
                return self._json({"state": "idle"})
            return self._json(task.snapshot())
        if path == "/api/health":
            return self._json({"ok": True, "version": "0.3.0"})
        return self._serve(path.lstrip("/"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/discover":
                return self._discover()
            if path == "/api/resolve":
                return self._resolve()
            if path == "/api/pick":
                return self._pick()
            if path == "/api/start":
                return self._start()
            if path == "/api/cancel":
                return self._cancel()
        except Exception as exc:                    # 接口层兜底，避免页面卡死
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "未知接口"}, 404)

    def _serve(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    # -- 接口实现 ---------------------------------------------------------

    def _discover(self) -> None:
        body = self._body()
        root = (body.get("root") or "").strip().strip('"')
        if not root:
            return self._json({"error": "未指定目录"}, 400)
        try:
            return self._json(discover(Path(root)))
        except FileNotFoundError as exc:
            return self._json({"error": str(exc)}, 400)

    def _resolve(self) -> None:
        name = (self._body().get("name") or "").strip()
        if not name:
            return self._json({"error": "缺少文件夹名"}, 400)
        hits = resolve_folder(name)
        return self._json({"name": name, "candidates": hits})

    def _pick(self) -> None:
        try:
            proc = subprocess.run([sys.executable, str(PICKER)],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
        except subprocess.TimeoutExpired:
            return self._json({"error": "选择窗口超时"}, 500)
        if proc.returncode != 0:
            return self._json({"path": "", "cancelled": True})
        return self._json({"path": proc.stdout.strip()})

    def _start(self) -> None:
        global CURRENT
        body = self._body()
        root = (body.get("root") or "").strip()
        if not root:
            return self._json({"error": "未指定目录"}, 400)
        with CURRENT_LOCK:
            if CURRENT is not None and CURRENT.state == "running":
                return self._json({"error": "已有任务在执行"}, 409)
            try:
                info = discover(Path(root))
            except FileNotFoundError as exc:
                return self._json({"error": str(exc)}, 400)
            if not info["units"]:
                return self._json({"error": "该目录下没有找到任何 region 文件夹"}, 400)

            wanted = set(body.get("units") or [])
            if not wanted:
                return self._json({"error": "未选择任何世界"}, 400)
            units = [u for u in info["units"] if u["region_dir"] in wanted]
            if not units:
                return self._json({"error": "所选世界不属于该目录"}, 400)

            minutes = float(body.get("minutes") or 15)
            mode = body.get("mode") or "compact"
            if mode not in ("compact", "fast", "compact-only"):
                return self._json({"error": "写入模式无效"}, 400)

            task = Task(info["root"], units, minutes, mode,
                        bool(body.get("backup", True)), int(body.get("jobs") or 0))
            CURRENT = task
            task.start()
            return self._json({"id": task.id})

    def _cancel(self) -> None:
        task = CURRENT
        if task is None or task.state != "running":
            return self._json({"ok": False, "message": "没有正在执行的任务"})
        task.cancel()
        return self._json({"ok": True})


def _free_port(preferred: int) -> int:
    import socket
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    no_browser = "--no-browser" in argv
    port = _free_port(DEFAULT_PORT)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"

    print("GTNHMapSlim 精简工具")
    print(f"地址: {url}")
    print("关闭此窗口即可退出。")

    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
