#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gtnh_map_slim.py 的自测：构造模拟的 1.7.10 存档，执行清理并校验结果。

运行： python tests/test_map_slim.py
"""

from __future__ import annotations

import gzip
import hashlib
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "gtnh_map_slim.py"
sys.path.insert(0, str(TOOL.parent))
import gtnh_map_slim as cc  # noqa: E402


# --------------------------- 极简 NBT 写入器 ---------------------------

def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _named(tag_id: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_id]) + _s(name) + payload


def _compound(entries: list[bytes]) -> bytes:
    return b"".join(entries) + b"\x00"


def _int(name: str, v: int) -> bytes:
    return _named(3, name, struct.pack(">i", v))


def _long(name: str, v: int) -> bytes:
    return _named(4, name, struct.pack(">q", v))


def chunk_nbt(chunk_x: int, chunk_z: int, inhabited: int | None, data_version: int | None = 100) -> bytes:
    level = [_int("xPos", chunk_x), _int("zPos", chunk_z)]
    if inhabited is not None:
        level.append(_long("InhabitedTime", inhabited))
    root = [_named(10, "Level", _compound(level))]
    if data_version is not None:
        root.append(_int("DataVersion", data_version))
    return _named(10, "", _compound(root))


def _list(name: str, elem_id: int, payloads: list[bytes]) -> bytes:
    return _named(9, name, bytes([elem_id]) + struct.pack(">i", len(payloads)) + b"".join(payloads))


def _byte_array(name: str, data: bytes) -> bytes:
    return _named(7, name, struct.pack(">i", len(data)) + data)


def _string(name: str, v: str) -> bytes:
    return _named(8, name, _s(v))


def chunk_nbt_full(chunk_x: int, chunk_z: int, inhabited: int | None) -> bytes:
    """带 Sections / Entities / TileEntities 的“真实形状”区块，用来验证跳过逻辑。"""
    sections = []
    for y in range(16):
        sections.append(_compound([
            _named(1, "Y", bytes([y])),
            _byte_array("Blocks", bytes((i * 7 + y) & 0xFF for i in range(4096))),
            _byte_array("Data", bytes(2048)),
            _byte_array("SkyLight", bytes(2048)),
            _byte_array("BlockLight", bytes(2048)),
        ]))
    entities = [
        _compound([_string("id", "minecraft:item"), _list("Pos", 6, [struct.pack(">d", 1.0)] * 3)]),
        _compound([_string("id", "gregtech:gt.blockmachines"), _string("mID", "basicmachine.centrifuge.tier.01")]),
    ]
    tile_entities = [
        _compound([_string("id", "gregtech:gt.blockmachines"), _int("x", 3), _int("y", 64), _int("z", 9)]),
    ]
    level = [_int("xPos", chunk_x), _int("zPos", chunk_z),
             _int("TerrainPopulated", 1), _int("LightPopulated", 1)]
    if inhabited is not None:
        level.append(_long("InhabitedTime", inhabited))
    level += [_list("Sections", 10, sections), _list("Entities", 10, entities),
              _list("TileEntities", 10, tile_entities)]

    root = [_named(10, "Level", _compound(level)), _int("DataVersion", 100)]
    return _named(10, "", _compound(root))


def chunk_nbt_late_time(chunk_x: int, chunk_z: int, inhabited: int) -> bytes:
    """把 InhabitedTime 放在 Sections 之后，强制走「前缀不够 -> 完整解压」的回退路径。"""
    sections = []
    for y in range(16):
        sections.append(_compound([
            _named(1, "Y", bytes([y])),
            _byte_array("Blocks", bytes((i * 13 + y) & 0xFF for i in range(4096))),
            _byte_array("Data", bytes(2048)),
        ]))
    level = [_int("xPos", chunk_x), _int("zPos", chunk_z),
             _list("Sections", 10, sections),
             _long("InhabitedTime", inhabited)]
    root = [_named(10, "Level", _compound(level)), _int("DataVersion", 100)]
    return _named(10, "", _compound(root))


def chunk_blob(nbt: bytes, compression: str = "zlib") -> bytes:
    if compression == "zlib":
        payload, ctype = zlib.compress(nbt), 2
    elif compression == "gzip":
        payload, ctype = gzip.compress(nbt), 1
    else:
        payload, ctype = nbt, 3
    return struct.pack(">i", len(payload) + 1) + bytes([ctype]) + payload


def write_region(path: Path, chunks: dict[int, bytes]) -> None:
    """chunks: {index: blob}，按扇区顺序写入，写不下的按实际长度补齐。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(8192)
    body = bytearray()
    offset = 2
    for index, blob in sorted(chunks.items()):
        sectors = (len(blob) + 4095) // 4096
        base = index * 4
        header[base] = (offset >> 16) & 0xFF
        header[base + 1] = (offset >> 8) & 0xFF
        header[base + 2] = offset & 0xFF
        header[base + 3] = sectors
        struct.pack_into(">i", header, 4096 + base, 1234567890)
        body += blob + b"\x00" * (sectors * 4096 - len(blob))
        offset += sectors
    path.write_bytes(bytes(header) + bytes(body))


def read_region_chunks(path: Path) -> dict[int, int | None]:
    """返回 {index: InhabitedTime}（None 表示字段缺失）"""
    data = path.read_bytes()
    offsets, sectors, _ = cc.read_header(data)
    out = {}
    for i in range(1024):
        if offsets[i] == 0 or sectors[i] == 0:
            continue
        start = offsets[i] * 4096
        length = struct.unpack_from(">i", data, start)[0]
        ctype = struct.unpack_from(">b", data, start + 4)[0]
        nbt = cc.decompress(data[start + 5:start + 4 + length], ctype, None)
        inhabited, _ = cc.extract_chunk_info(nbt)
        out[i] = inhabited
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_chunk_bytes(path: Path, index: int) -> bytes | None:
    """取出某个区块在文件中的原始字节（含长度+压缩类型头），用于校验未被改写。"""
    data = path.read_bytes()
    offsets, sectors, _ = cc.read_header(data)
    if offsets[index] == 0:
        return None
    start = offsets[index] * 4096
    return data[start:start + sectors[index] * 4096]


# ------------------------------- 测试 -------------------------------

class MapSlimTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = Path(self.tmp.name) / "GTNH_Test"
        self.world.mkdir()
        (self.world / "level.dat").write_bytes(b"dummy")

        # 主世界：0,1,5 预期删除；2,3,33 预期保留；4 缺失字段默认保留
        write_region(self.world / "region" / "r.0.0.mca", {
            0: chunk_blob(chunk_nbt(0, 0, 0)),
            1: chunk_blob(chunk_nbt(1, 0, 6000)),
            2: chunk_blob(chunk_nbt(2, 0, 18000)),          # 达到 15 分钟阈值，予以保留
            3: chunk_blob(chunk_nbt(3, 0, 36000)),
            4: chunk_blob(chunk_nbt(4, 0, None)),
            5: chunk_blob(chunk_nbt(5, 0, 1200), "gzip"),
            33: chunk_blob(chunk_nbt(1, 1, 20000)),
        })

        # 下界：全部低于阈值，预期清空/删除整个 region 文件
        write_region(self.world / "DIM-1" / "region" / "r.0.0.mca", {
            0: chunk_blob(chunk_nbt(0, 0, 100)),
            1: chunk_blob(chunk_nbt(1, 0, 200)),
        })

        # 末地：高于阈值，保持原样
        write_region(self.world / "DIM1" / "region" / "r.-1.0.mca", {
            0: chunk_blob(chunk_nbt(-32, 0, 99999)),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra):
        cmd = [sys.executable, str(TOOL), "--world", str(self.world), *extra]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

    def test_preview_does_not_touch_files(self):
        before = {p: digest(p) for p in self.world.rglob("*.mca")}
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = {p: digest(p) for p in self.world.rglob("*.mca")}
        self.assertEqual(before, after, "预览模式不应该修改文件")
        self.assertIn("15.00 分钟", r.stdout)

    def test_apply_deletes_only_below_threshold(self):
        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        main = read_region_chunks(self.world / "region" / "r.0.0.mca")
        self.assertEqual(sorted(main), [2, 3, 4, 33], f"保留结果不对: {main}")
        self.assertEqual(main[2], 18000)
        self.assertEqual(main[3], 36000)
        self.assertIsNone(main[4], "缺 InhabitedTime 的区块默认应保留")

        emptied = self.world / "DIM-1" / "region" / "r.0.0.mca"
        self.assertTrue(emptied.exists(), "默认不删除文件，应改写为空 region")
        self.assertEqual(emptied.stat().st_size, 8192, "空 region 文件应为 8 KiB 头部")
        self.assertEqual(read_region_chunks(emptied), {}, "空 region 不应残留任何区块")

        end = read_region_chunks(self.world / "DIM1" / "region" / "r.-1.0.mca")
        self.assertEqual(end, {0: 99999})

        # 自动备份应包含被改动的 region 文件
        backups = list(self.world.parent.glob("GTNH_Test_chunkcleaner_backup_*/**/*.mca"))
        self.assertGreaterEqual(len(backups), 2, f"备份文件缺失: {backups}")

    def test_missing_as_zero(self):
        r = self.run_tool("--apply", "--missing-as-zero")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        main = read_region_chunks(self.world / "region" / "r.0.0.mca")
        self.assertEqual(sorted(main), [2, 3, 33], f"缺字段的区块应被删除: {main}")

    def test_csv_report(self):
        csv_path = Path(self.tmp.name) / "del.csv"
        r = self.run_tool("--apply", "--csv", str(csv_path))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = csv_path.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines), 1 + 5)  # 表头 + 主世界3 + 下界2
        self.assertIn("chunk_x", lines[0])

    def test_ticks_override(self):
        # 阈值 30 分钟：36000 ticks 予以保留，20000 ticks 予以删除
        r = self.run_tool("--apply", "--ticks", "36000")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        main = read_region_chunks(self.world / "region" / "r.0.0.mca")
        self.assertEqual(sorted(main), [3, 4], f"阈值 36000 时应只剩 3 和 4: {main}")

    def test_realistic_chunk_with_sections(self):
        """包含 16 个 Sections、实体与方块实体的区块：正确读取 InhabitedTime，且保留区块字节不变。"""
        mca = self.world / "region" / "r.2.0.mca"
        write_region(mca, {
            0: chunk_blob(chunk_nbt_full(64, 0, 9000)),    # 7.5 分钟：预期删除
            1: chunk_blob(chunk_nbt_full(65, 0, 54000)),   # 45 分钟：预期保留
            2: chunk_blob(chunk_nbt_full(66, 0, None)),    # 缺失字段：默认保留
        })
        before = raw_chunk_bytes(mca, 1)
        self.assertIsNotNone(before)

        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        remaining = read_region_chunks(mca)
        self.assertEqual(sorted(remaining), [1, 2], f"只应保留 1、2: {remaining}")
        self.assertEqual(remaining[1], 54000)
        self.assertEqual(raw_chunk_bytes(mca, 1), before, "保留的区块必须原样字节拷贝，不能重新编码")

    def test_no_compact_only_patches_header(self):
        mca = self.world / "region" / "r.0.0.mca"
        size_before = mca.stat().st_size
        r = self.run_tool("--apply", "--no-compact")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(mca.stat().st_size, size_before, "快速模式不应改变文件大小")
        self.assertEqual(sorted(read_region_chunks(mca)), [2, 3, 4, 33])
        # 快速打补丁模式（--no-compact）下，区块全部删除的 region 文件仅清零头部扇区表，不修改文件大小
        self.assertTrue((self.world / "DIM-1" / "region" / "r.0.0.mca").exists())
        self.assertEqual(read_region_chunks(self.world / "DIM-1" / "region" / "r.0.0.mca"), {})

    def test_late_inhabited_time_falls_back(self):
        """InhabitedTime 位于 Sections 之后：8 KiB 前缀无法读到，必须回退到完整解压。"""
        mca = self.world / "region" / "r.3.0.mca"
        write_region(mca, {
            0: chunk_blob(chunk_nbt_late_time(96, 0, 5000)),
            1: chunk_blob(chunk_nbt_late_time(97, 0, 60000)),
        })
        scan = cc.scan_region(mca, 18000, False)
        self.assertEqual(sorted(scan.present), [0, 1])
        self.assertEqual([i for i, _ in scan.deleted], [0])
        self.assertEqual(scan.prefix_hits, 0, "该布局必须走完整解压回退")

    def test_compact_only_reclaims_space(self):
        """--no-compact 之后通过 --compact-only 回收空间：不解压，仅压实扇区。"""
        mca = self.world / "region" / "r.0.0.mca"
        self.run_tool("--apply", "--no-compact")
        size_patched = mca.stat().st_size

        r = self.run_tool("--compact-only", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(mca.stat().st_size, size_patched, "压实后文件应变小")
        self.assertEqual(sorted(read_region_chunks(mca)), [2, 3, 4, 33])
        emptied = self.world / "DIM-1" / "region" / "r.0.0.mca"
        self.assertTrue(emptied.exists(), "默认不删除文件，应改写为空 region")
        self.assertEqual(read_region_chunks(emptied), {}, "空 region 不应残留任何区块")

    def test_delete_empty_files_flag(self):
        """--delete-empty-files 时才真正移除空 region 文件。"""
        r = self.run_tool("--apply", "--delete-empty-files")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.world / "DIM-1" / "region" / "r.0.0.mca").exists(),
                         "指定该参数后应删除空 region 文件")
        self.assertEqual(sorted(read_region_chunks(self.world / "region" / "r.0.0.mca")),
                         [2, 3, 4, 33])

    def test_parallel_workers(self):
        for rx in range(2, 8):
            write_region(self.world / "region" / f"r.{rx}.0.mca", {
                0: chunk_blob(chunk_nbt(rx * 32, 0, 1000)),
                1: chunk_blob(chunk_nbt(rx * 32 + 1, 0, 30000)),
                2: chunk_blob(chunk_nbt(rx * 32 + 2, 0, 17000)),
            })
        r = self.run_tool("--apply", "--jobs", "4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rx in range(2, 8):
            left = read_region_chunks(self.world / "region" / f"r.{rx}.0.mca")
            self.assertEqual(sorted(left), [1], f"r.{rx} 只应保留 index 1: {left}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
