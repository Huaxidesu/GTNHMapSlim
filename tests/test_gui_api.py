#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui/server.py 的接口测试。

构造模拟的多世界存档，验证存档单元发现、备份目录排除、任务执行与进度汇总，
以及通过 HTTP 接口完成的完整流程。
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_map_slim import chunk_blob, chunk_nbt_full, write_region  # noqa: E402
import gtnh_map_slim  # noqa: E402
from gui import server as srv  # noqa: E402


def build_world(root: Path, subs: list[str], per_region: int = 90,
                seed: int = 1) -> None:
    """按 subs 中的相对路径创建 region 目录并写入模拟区块。"""
    for index, sub in enumerate(subs):
        region = root / sub / "region" if sub else root / "region"
        region.mkdir(parents=True, exist_ok=True)
        rng = random.Random(seed + index)
        chunks = {}
        for i in range(per_region):
            chunks[i] = chunk_blob(chunk_nbt_full(i % 32, i // 32, rng.randint(0, 60000)))
        write_region(region / "r.0.0.mca", chunks)


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.world = self.tmp / "World"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_multi_world_units(self):
        build_world(self.world, ["", "DIM-1", "DIM100"])
        units = gtnh_map_slim.discover_units(self.world)
        self.assertEqual([u["rel"] for u in units], [".", "DIM-1", "DIM100"])
        self.assertEqual(units[0]["name"], "World")
        self.assertEqual(units[1]["name"], "DIM-1")
        for unit in units:
            self.assertEqual(unit["files"], 1)
            self.assertGreater(unit["bytes"], 0)

    def test_upper_level_directory(self):
        """选择服务器目录时，应向下找出 World 及其全部维度。"""
        build_world(self.world, ["", "DIM-1"])
        units = gtnh_map_slim.discover_units(self.tmp)
        self.assertEqual(len(units), 2)
        self.assertTrue(all(u["rel"].startswith("World") for u in units))

    def test_backup_directory_excluded(self):
        build_world(self.world, ["", "DIM-1"])
        build_world(self.tmp / "World_chunkcleaner_backup_20260101_000000", ["DIM-1"])
        units = gtnh_map_slim.discover_units(self.tmp)
        self.assertEqual(len(units), 2, "备份目录不应被识别为存档单元")

    def test_depth_limit(self):
        build_world(self.world, ["DIM-1"])
        units = gtnh_map_slim.discover_units(self.world, max_depth=1)
        self.assertEqual(units, [], "超出深度上限的目录不应被发现")


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.world = cls.tmp / "World"
        build_world(cls.world, ["", "DIM-1"], per_region=70)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        srv.CURRENT = None

    def call(self, path: str, payload: dict | None = None) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_static_page(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=20) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("GTNHMapSlim", body)
        self.assertIn("/app.js", body)

    def test_discover_endpoint(self):
        data = self.call("/api/discover", {"root": str(self.world)})
        self.assertEqual(data["totals"]["dirs"], 2)
        self.assertEqual(len(data["units"]), 2)
        self.assertEqual(data["totals"]["files"], 2)

    def test_discover_missing_directory(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/discover", {"root": str(self.tmp / "nope")})
        self.assertEqual(ctx.exception.code, 400)

    def test_start_rejects_empty_selection(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/start", {"root": str(self.world), "units": ["/no/such/dir"]})
        self.assertEqual(ctx.exception.code, 400)

    def test_full_run(self):
        data = self.call("/api/discover", {"root": str(self.world)})
        units = [u["region_dir"] for u in data["units"]]

        started = self.call("/api/start", {
            "root": str(self.world), "units": units, "minutes": 15,
            "mode": "compact", "backup": False,
        })
        self.assertIn("id", started)

        snapshot = {}
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = self.call("/api/progress")
            if snapshot.get("state") != "running":
                break
            time.sleep(0.2)

        self.assertEqual(snapshot["state"], "done", snapshot.get("message"))
        self.assertEqual(snapshot["files_done"], 2)
        self.assertEqual(snapshot["files_total"], 2)
        self.assertGreater(snapshot["deleted"], 0)
        self.assertEqual(snapshot["percent"], 100.0)
        self.assertEqual(len(snapshot["units"]), 2)
        self.assertTrue(all(u["done"] == 1 for u in snapshot["units"]))

    def test_cancel_without_task(self):
        data = self.call("/api/cancel", {})
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
