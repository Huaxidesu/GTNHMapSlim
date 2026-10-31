#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能对比：基线实现（完整解压、单进程）与优化实现（8 KiB 前缀、多进程）。

构造模拟的 1.7.10 存档（区块体积接近 GTNH 实际水平），分别执行四种配置，
并测量「压实重写」与「快速打补丁」两种写入方式。

运行： python bench/bench.py [区块数]
"""

from __future__ import annotations

import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = ROOT / "gtnh_map_slim.py"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))
from test_map_slim import _compound, _int, _long, _list, _named, _string, _byte_array  # noqa: E402


def build_chunk(chunk_x: int, chunk_z: int, inhabited: int, sections: int = 16) -> bytes:
    """随机方块，压缩后接近 GTNH 区块大小（约 60~70 KB）。"""
    secs = []
    for y in range(sections):
        secs.append(_compound([
            _named(1, "Y", bytes([y])),
            _byte_array("Blocks", os.urandom(4096)),
            _byte_array("Data", bytes(2048)),
            _byte_array("SkyLight", bytes(2048)),
            _byte_array("BlockLight", bytes(2048)),
        ]))
    ents = [_compound([_string("id", "gregtech:gt.blockmachines"), _int("mID", i)]) for i in range(40)]
    level = [_int("xPos", chunk_x), _int("zPos", chunk_z),
             _long("LastUpdate", 1234567890), _long("InhabitedTime", inhabited),
             _int("TerrainPopulated", 1), _int("LightPopulated", 1),
             _list("Sections", 10, secs), _list("Entities", 10, ents),
             _list("TileEntities", 10, [])]
    return _named(10, "", _compound([_named(10, "Level", _compound(level)), _int("DataVersion", 100)]))


def build_world(root: Path, total_chunks: int, per_region: int = 300) -> tuple[int, int]:
    (root / "region").mkdir(parents=True, exist_ok=True)
    random.seed(7)
    n_regions = max(1, total_chunks // per_region)
    count = 0
    for rx in range(n_regions):
        header = bytearray(8192)
        body = bytearray()
        offset = 2
        for i in range(min(per_region, 1024)):
            nbt = build_chunk(rx * 32 + i % 32, i // 32, random.randint(0, 60000))
            payload = zlib.compress(nbt, 6)
            blob = struct.pack(">i", len(payload) + 1) + b"\x02" + payload
            sectors = (len(blob) + 4095) // 4096
            base = i * 4
            header[base] = (offset >> 16) & 0xFF
            header[base + 1] = (offset >> 8) & 0xFF
            header[base + 2] = offset & 0xFF
            header[base + 3] = sectors
            struct.pack_into(">i", header, 4096 + base, 1700000000)
            body += blob + b"\x00" * (sectors * 4096 - len(blob))
            offset += sectors
            count += 1
        (root / "region" / f"r.{rx}.0.mca").write_bytes(bytes(header) + bytes(body))
    size = sum(p.stat().st_size for p in root.rglob("*.mca"))
    return count, size


def run(world: Path, *extra: str) -> tuple[float, str]:
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(TOOL), "--world", str(world), *extra],
                       capture_output=True, text=True, encoding="utf-8")
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"命令失败: {' '.join(extra)}")
    return dt, r.stdout


def line(label: str, dt: float, chunks: int, size: int) -> str:
    return (f"{label:<34} {dt:6.2f}s   {chunks / dt:8,.0f} 区块/s   "
            f"{size / 2**20 / dt:7.1f} MiB/s")


def main() -> int:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    tmp = Path(tempfile.mkdtemp(prefix="cc_bench_"))
    try:
        src = tmp / "world"
        t0 = time.perf_counter()
        chunks, size = build_world(src, total)
        print(f"测试存档: {chunks} 区块, {size / 2**20:.1f} MiB, 生成耗时 {time.perf_counter() - t0:.1f}s")
        print(f"平均每区块 {size / chunks / 1024:.0f} KiB，CPU {os.cpu_count()} 核\n")

        results = []
        for label, extra in [
            ("旧逻辑: 完整解压, 单进程", ("--full-decompress", "--jobs", "1")),
            ("旧逻辑: 完整解压, 8 进程", ("--full-decompress", "--jobs", "8")),
            ("新逻辑: 8KB 前缀, 单进程", ("--jobs", "1")),
            ("新逻辑: 8KB 前缀, 8 进程", ("--jobs", "8")),
        ]:
            best = min(run(src, *extra)[0] for _ in range(2))
            results.append((label, best))
            print(line(label, best, chunks, size))

        base = results[0][1]
        print()
        for label, dt in results[1:]:
            print(f"  {label} 相对旧逻辑提速 {base / dt:.1f}x")

        # 写盘：压实 vs 快速打补丁
        print()
        for label, extra in [("压实重写 (默认)", ("--apply", "--jobs", "8")),
                             ("快速打补丁 (--no-compact)", ("--apply", "--no-compact", "--jobs", "8"))]:
            work = tmp / ("w_" + label.split()[0])
            shutil.copytree(src, work)
            dt, out = run(work, *extra)
            size_after = sum(p.stat().st_size for p in work.rglob("*.mca"))
            print(f"{label:<34} {dt:6.2f}s   写入后 {size_after / 2**20:6.1f} MiB "
                  f"(原 {size / 2**20:.1f} MiB)")
            shutil.rmtree(work, ignore_errors=True)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
