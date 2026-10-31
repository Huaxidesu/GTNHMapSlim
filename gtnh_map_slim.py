#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GTNHMapSlim —— Minecraft Java 版存档区块清理工具。

功能：删除 InhabitedTime（区块累计被加载的时长）低于阈值的区块，默认阈值 15 分钟。
该行为等价于 MCA Selector 中的过滤器组合：InhabitedTime < 15min 后删除所选区块。

判定规则与写入流程参考 MCA Selector（Querz/mcaselector，MIT）的实现：
  filter/filters/InhabitedTimeFilter.java   时长换算为 ticks：parseDuration(raw) * 20
  version/java_1_9/ChunkFilter_15w32a.java  Level.InhabitedTime（1.7.10 ~ 1.17）
  version/java_1_18/...                     根节点 InhabitedTime（1.18 及以后）
  io/mca/MCAFile.java                       deleteChunks()：偏移、扇区数、时间戳清零
                                            deFragment()：重写文件并压实扇区，区块全部删除时移除文件

面向大规模存档的性能设计（数据见 doc/TECHNICAL.md）：
  1. 仅解压每个区块的前 8 KiB。InhabitedTime 位于 NBT 前部，无需解压完整区块数据。
  2. 使用 mmap 读取 region 文件，避免将 GB 级文件重复载入进程堆。
  3. 按 region 文件粒度进行多进程并行处理。
  4. --no-compact：仅修改 8 KiB 文件头，不重写数据，写入开销最低。
  5. --since：仅处理指定天数内改动过的 region 文件，支持增量执行。

运行时仅依赖 Python 3 标准库。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import mmap
import os
import shutil
import struct
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SECTOR = 4096
HEADER_SIZE = 2 * SECTOR
CHUNKS_PER_REGION = 1024
TICKS_PER_MINUTE = 20 * 60

# 解压前 N 字节即可读到 InhabitedTime：该字段位于 1.7.10 Level 标签前部
PREFIX_BYTES = 8192
# 解出上述 8 KiB 所需的压缩数据不超过该长度：16 KiB 输入至少产生 16 KiB 输出
COMPRESSED_PREFIX = 16384

# NBT tag ids
TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG = 0, 1, 2, 3, 4
TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARRAY, TAG_STRING = 5, 6, 7, 8
TAG_LIST, TAG_COMPOUND, TAG_INT_ARRAY, TAG_LONG_ARRAY = 9, 10, 11, 12

_FIXED = {TAG_BYTE: 1, TAG_SHORT: 2, TAG_INT: 4, TAG_LONG: 8, TAG_FLOAT: 4, TAG_DOUBLE: 8}


class TruncatedNBT(Exception):
    """前缀不够长，还没读到 InhabitedTime 就读越界了。"""


# ---------------------------------------------------------------------------
# 精简 NBT 读取器：仅提取 InhabitedTime 与 DataVersion，其余标签按长度跳过，
# 避免将 4096 字节的方块数组解析为 Python 对象。
# ---------------------------------------------------------------------------

def _read_string(buf, pos: int) -> tuple[str, int]:
    if pos + 2 > len(buf):
        raise TruncatedNBT
    n = struct.unpack_from(">H", buf, pos)[0]
    pos += 2
    if pos + n > len(buf):
        raise TruncatedNBT
    return bytes(buf[pos:pos + n]).decode("utf-8", "replace"), pos + n


def _skip_payload(buf, pos: int, tag: int) -> int:
    if tag in _FIXED:
        npos = pos + _FIXED[tag]
        if npos > len(buf):
            raise TruncatedNBT
        return npos
    if tag == TAG_BYTE_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        npos = pos + 4 + n
    elif tag == TAG_STRING:
        n = struct.unpack_from(">H", buf, pos)[0]
        npos = pos + 2 + n
    elif tag == TAG_INT_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        npos = pos + 4 + 4 * n
    elif tag == TAG_LONG_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        npos = pos + 4 + 8 * n
    elif tag == TAG_LIST:
        elem = buf[pos]
        n = struct.unpack_from(">i", buf, pos + 1)[0]
        pos += 5
        if elem == TAG_END or n <= 0:
            return pos
        if elem in _FIXED:
            npos = pos + _FIXED[elem] * n
        else:
            for _ in range(n):
                pos = _skip_payload(buf, pos, elem)
            return pos
    elif tag == TAG_COMPOUND:
        while True:
            t = buf[pos]
            pos += 1
            if t == TAG_END:
                return pos
            _, pos = _read_string(buf, pos)
            pos = _skip_payload(buf, pos, t)
    else:
        raise ValueError(f"未知 NBT tag: {tag}")
    if npos > len(buf):
        raise TruncatedNBT
    return npos


def extract_chunk_info(nbt) -> tuple[int | None, int | None]:
    """返回 (InhabitedTime_ticks, DataVersion)，缺失则为 None。

    nbt 允许是“截断的前缀”，只要在读越界之前拿到了 InhabitedTime 就返回成功；
    读越界且还没拿到时抛 TruncatedNBT，由调用方决定是否完整解压。
    """
    if not nbt:
        return None, None
    if nbt[0] != TAG_COMPOUND:
        raise ValueError("chunk 根节点不是 TAG_Compound")
    pos = 1
    _, pos = _read_string(nbt, pos)

    inhabited = None
    data_version = None

    try:
        while True:
            t = nbt[pos]
            pos += 1
            if t == TAG_END:
                break
            name, pos = _read_string(nbt, pos)

            if t == TAG_LONG and name == "InhabitedTime":          # 1.18+
                if pos + 8 > len(nbt):
                    raise TruncatedNBT
                inhabited = struct.unpack_from(">q", nbt, pos)[0]
                pos += 8
            elif t == TAG_INT and name == "DataVersion":
                if pos + 4 > len(nbt):
                    raise TruncatedNBT
                data_version = struct.unpack_from(">i", nbt, pos)[0]
                pos += 4
            elif t == TAG_COMPOUND and name == "Level":            # 1.7.10 ~ 1.17
                while True:
                    t2 = nbt[pos]
                    pos += 1
                    if t2 == TAG_END:
                        break
                    name2, pos = _read_string(nbt, pos)
                    if t2 == TAG_LONG and name2 == "InhabitedTime":
                        if pos + 8 > len(nbt):
                            raise TruncatedNBT
                        inhabited = struct.unpack_from(">q", nbt, pos)[0]
                        pos += 8
                    elif t2 == TAG_INT and name2 == "InhabitedTime":
                        if pos + 4 > len(nbt):
                            raise TruncatedNBT
                        inhabited = struct.unpack_from(">i", nbt, pos)[0]
                        pos += 4
                    else:
                        pos = _skip_payload(nbt, pos, t2)
            else:
                pos = _skip_payload(nbt, pos, t)
    except (TruncatedNBT, IndexError):
        # 前缀被截断：已读到 InhabitedTime 时直接返回，否则交由调用方完整解压
        if inhabited is not None:
            return inhabited, data_version
        raise TruncatedNBT from None

    return inhabited, data_version


# ---------------------------------------------------------------------------
# 解压：优先只解前 PREFIX_BYTES 字节
# ---------------------------------------------------------------------------

def _inflate(data: bytes, base: int, limit: int | None) -> tuple[bytes, bool]:
    """返回 (解压出的字节, 是否已经到流末尾)。limit=None 表示全部解压。"""
    if base == 3:
        return (data if limit is None else data[:limit]), (limit is None or len(data) <= limit)
    if base == 1:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)     # gzip
    elif base == 2:
        d = zlib.decompressobj()                        # zlib
    elif base == 4:
        try:
            import lz4.block                            # 可选依赖
            out = lz4.block.decompress(data, uncompressed_size=64 * 1024 * 1024)
            return out, True
        except ImportError as exc:
            raise RuntimeError("LZ4 压缩的区块需要 pip install lz4") from exc
    else:
        raise ValueError(f"未知压缩类型 {base}")

    if limit is None:
        return d.decompress(data) + d.flush(), True
    out = d.decompress(data, limit)
    return out, d.eof


def decompress(payload: bytes, ctype: int, mcc: Path | None = None) -> bytes:
    """完整解压（保留给测试与外部调用）。ctype 为有符号字节：负数=外置 .mcc"""
    external = ctype < 0
    base = ctype + 128 if external else ctype
    if external:
        if mcc is None or not mcc.exists():
            raise FileNotFoundError(f"缺少外置区块文件 {mcc}")
        payload = mcc.read_bytes()
    out, _ = _inflate(payload, base, None)
    return out


def _chunk_info(head, ctype: int, mcc: Path | None, mm=None, span=None):
    """先只解压前 8 KB；输入不够时再取完整数据。

    head 为 payload 的前 COMPRESSED_PREFIX 字节，避免为每个区块复制完整的
    60~100 KiB 压缩数据。
    返回 (inhabited, data_version, 是否走了前缀快路径)。
    """
    external = ctype < 0
    base = ctype + 128 if external else ctype
    if external:
        if mcc is None or not mcc.exists():
            raise FileNotFoundError(f"缺少外置区块文件 {mcc}")
        head = mcc.read_bytes()
        mm = None

    def full():
        return head if mm is None else mm[span[0]:span[1]]

    prefix, finished = _inflate(head, base, PREFIX_BYTES)
    if not finished and len(prefix) < PREFIX_BYTES and mm is not None:
        head = full()                                   # 压缩比过高，前 16 KiB 输入不足，取完整数据重试
        prefix, finished = _inflate(head, base, PREFIX_BYTES)

    if finished:
        return (*extract_chunk_info(prefix), True)
    try:
        return (*extract_chunk_info(prefix), True)
    except TruncatedNBT:
        full_out, _ = _inflate(full(), base, None)
        return (*extract_chunk_info(full_out), False)


# ---------------------------------------------------------------------------
# region 文件（.mca）
# ---------------------------------------------------------------------------

def parse_region_name(path: Path) -> tuple[int, int] | None:
    parts = path.stem.split(".")                    # r.<x>.<z>.mca
    if len(parts) != 3 or parts[0] != "r":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def read_header(data) -> tuple[list[int], list[int], list[int]]:
    offsets = [0] * CHUNKS_PER_REGION
    sectors = [0] * CHUNKS_PER_REGION
    timestamps = [0] * CHUNKS_PER_REGION
    for i in range(CHUNKS_PER_REGION):
        base = i * 4
        offsets[i] = (data[base] << 16) | (data[base + 1] << 8) | data[base + 2]
        sectors[i] = data[base + 3]
        timestamps[i] = struct.unpack_from(">i", data, SECTOR + base)[0]
    return offsets, sectors, timestamps


def chunk_mcc_path(region_path: Path, chunk_x: int, chunk_z: int) -> Path:
    return region_path.parent / f"c.{chunk_x}.{chunk_z}.mcc"


class RegionScan:
    __slots__ = ("path", "region_x", "region_z", "present", "deleted",
                 "missing_time", "unreadable", "data_version", "prefix_hits", "compact")

    def __init__(self, path: Path, rx: int, rz: int):
        self.path = path
        self.region_x = rx
        self.region_z = rz
        self.present: list[int] = []
        self.deleted: list[tuple[int, int]] = []       # (index, ticks)
        self.missing_time: list[int] = []
        self.unreadable: list[tuple[int, str]] = []
        self.data_version: int | None = None
        self.prefix_hits = 0
        self.compact = True

    @property
    def kept(self) -> int:
        return len(self.present) - len(self.deleted)

    def chunk_coords(self, index: int) -> tuple[int, int]:
        return self.region_x * 32 + (index % 32), self.region_z * 32 + (index // 32)


def scan_region(path: Path, threshold: int, missing_as_zero: bool,
                full_decompress: bool = False) -> RegionScan:
    loc = parse_region_name(path)
    if loc is None:
        raise ValueError(f"不是合法的 region 文件名: {path}")
    scan = RegionScan(path, loc[0], loc[1])

    if path.stat().st_size < HEADER_SIZE:
        return scan

    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            size = len(mm)
            offsets, sectors, _ = read_header(mm)

            for i in range(CHUNKS_PER_REGION):
                if offsets[i] == 0 or sectors[i] == 0:
                    continue
                scan.present.append(i)
                start = offsets[i] * SECTOR
                if start + 5 > size:
                    scan.unreadable.append((i, "offset 越界"))
                    continue
                try:
                    length = struct.unpack_from(">i", mm, start)[0]
                    if length <= 0:
                        scan.unreadable.append((i, "length 非法"))
                        continue
                    ctype = struct.unpack_from(">b", mm, start + 4)[0]
                    end = start + 4 + length
                    cx, cz = scan.chunk_coords(i)
                    mcc = chunk_mcc_path(path, cx, cz) if ctype < 0 else None
                    if full_decompress:
                        nbt = decompress(mm[start + 5:end], ctype, mcc)
                        inhabited, dv = extract_chunk_info(nbt)
                        hit = False
                    else:
                        head = mm[start + 5:min(end, start + 5 + COMPRESSED_PREFIX)]
                        inhabited, dv, hit = _chunk_info(head, ctype, mcc, mm, (start + 5, end))
                        scan.prefix_hits += 1 if hit else 0
                except Exception as exc:
                    scan.unreadable.append((i, f"{type(exc).__name__}: {exc}"))
                    continue

                if dv is not None and scan.data_version is None:
                    scan.data_version = dv
                if inhabited is None:
                    scan.missing_time.append(i)
                    if missing_as_zero:
                        scan.deleted.append((i, 0))
                elif inhabited < threshold:
                    scan.deleted.append((i, inhabited))

    return scan


def _backup(path: Path, backup_root: Path | None, world_root: Path) -> None:
    if backup_root is None:
        return
    try:
        rel = path.relative_to(world_root)
    except ValueError:
        rel = Path(path.parent.name) / path.name
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)


def _compact_region(path: Path, keep, backup_root: Path | None, world_root: Path,
                    on_remove=None) -> str:
    """把 keep 里的区块按顺序重新写一遍，压掉空洞扇区（等价 MCAFile.deFragment）。"""
    _backup(path, backup_root, world_root)

    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            offsets, sectors, timestamps = read_header(mm)
            keep = [i for i in keep if offsets[i] != 0 and sectors[i] != 0]

            if keep:                                          # 无保留区块时不创建临时文件
                header = bytearray(HEADER_SIZE)
                tmp = path.with_name(path.name + ".tmp")
                global_offset = 2
                with tmp.open("wb") as out:
                    out.write(header)                         # 预留头部，写入完成后回填
                    for i in keep:
                        sec = sectors[i]
                        start = offsets[i] * SECTOR
                        end = start + sec * SECTOR
                        if end <= len(mm):
                            out.write(mm[start:end])
                        else:                                 # 文件被截断，补零
                            chunk = mm[start:]
                            out.write(chunk)
                            out.write(b"\x00" * (sec * SECTOR - len(chunk)))
                        base = i * 4
                        header[base] = (global_offset >> 16) & 0xFF
                        header[base + 1] = (global_offset >> 8) & 0xFF
                        header[base + 2] = global_offset & 0xFF
                        header[base + 3] = sec
                        struct.pack_into(">i", header, SECTOR + base, timestamps[i])
                        global_offset += sec
                    out.seek(0)
                    out.write(header)

    if not keep:
        path.unlink()
        if on_remove:
            on_remove()
        return "removed"

    os.replace(tmp, path)                                     # 必须在 mmap 关闭之后（Windows）
    if on_remove:
        on_remove()
    return "rewritten"


def rewrite_region(scan: RegionScan, backup_root: Path | None, world_root: Path) -> str:
    """按 MCAFile.deFragment 的方式重写 region：只保留未删除的区块，扇区压实。
    返回 'rewritten' / 'removed' / 'patched'。"""
    path = scan.path
    delete_set = {idx for idx, _ in scan.deleted}

    if not scan.compact:
        # 快速模式：仅清零 8 KiB 头部中的偏移、扇区数与时间戳，文件大小不变
        _backup(path, backup_root, world_root)
        with path.open("r+b") as fh:
            for i in delete_set:
                fh.seek(i * 4)
                fh.write(b"\x00\x00\x00\x00")
                fh.seek(SECTOR + i * 4)
                fh.write(b"\x00\x00\x00\x00")
        _drop_mcc(scan, path)
        return "patched"

    keep = [i for i in range(CHUNKS_PER_REGION) if i not in delete_set]
    return _compact_region(path, keep, backup_root, world_root, lambda: _drop_mcc(scan, path))


def compact_region(path: Path, backup_root: Path | None, world_root: Path) -> str:
    """不解压、只按文件头把空洞扇区压实（用于 --no-compact 之后的二次回收）。"""
    return _compact_region(path, range(CHUNKS_PER_REGION), backup_root, world_root)
    return "rewritten"


def _drop_mcc(scan: RegionScan, path: Path) -> None:
    for idx, _ in scan.deleted:
        cx, cz = scan.chunk_coords(idx)
        mcc = chunk_mcc_path(path, cx, cz)
        if mcc.exists():
            try:
                mcc.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 多进程任务
# ---------------------------------------------------------------------------

_WORLD_ROOT: Path | None = None
_BACKUP_ROOT: Path | None = None
_OPTS: dict = {}


def _init_worker(world_root: str, backup_root: str | None, opts: dict) -> None:
    global _WORLD_ROOT, _BACKUP_ROOT, _OPTS
    _WORLD_ROOT = Path(world_root)
    _BACKUP_ROOT = Path(backup_root) if backup_root else None
    _OPTS = opts


def _work(path_str: str) -> dict:
    path = Path(path_str)

    if _OPTS.get("compact_only"):
        # 仅依据文件头压实扇区，不解压任何区块
        try:
            size = path.stat().st_size
            if size < HEADER_SIZE:
                return {"path": path_str, "present": 0, "deleted": 0, "missing": 0,
                        "unreadable": 0, "prefix_hits": 0, "rows": None,
                        "action": None, "error": None, "freed": 0}
            with path.open("rb") as fh:
                offsets, sectors, _ = read_header(fh.read(HEADER_SIZE))
        except Exception as exc:
            return {"path": path_str, "error": f"{type(exc).__name__}: {exc}"}

        present = [i for i in range(CHUNKS_PER_REGION) if offsets[i] and sectors[i]]
        potential = HEADER_SIZE + sum(sectors[i] for i in present) * SECTOR
        action = None
        if _OPTS["apply"] and potential < size:
            try:
                action = compact_region(path, _BACKUP_ROOT, _WORLD_ROOT)
            except Exception as exc:
                return {"path": path_str, "present": len(present), "deleted": 0, "missing": 0,
                        "unreadable": 0, "prefix_hits": 0, "rows": None, "action": None,
                        "error": f"压实失败 {type(exc).__name__}: {exc}", "freed": 0}
        return {"path": path_str, "present": len(present), "deleted": 0, "missing": 0,
                "unreadable": 0, "prefix_hits": 0, "rows": None,
                "action": action, "error": None, "freed": max(0, size - potential)}

    try:
        scan = scan_region(path, _OPTS["threshold"], _OPTS["missing_as_zero"],
                           _OPTS["full_decompress"])
    except Exception as exc:
        return {"path": path_str, "error": f"{type(exc).__name__}: {exc}"}

    scan.compact = _OPTS["compact"]
    result = {
        "path": path_str,
        "present": len(scan.present),
        "deleted": len(scan.deleted),
        "missing": len(scan.missing_time),
        "unreadable": len(scan.unreadable),
        "prefix_hits": scan.prefix_hits,
        "rows": [(path.name, *scan.chunk_coords(i), ticks, round(ticks / TICKS_PER_MINUTE, 3))
                 for i, ticks in scan.deleted] if _OPTS["want_rows"] else None,
        "action": None,
        "error": None,
    }

    if _OPTS["apply"] and scan.deleted:
        try:
            result["action"] = rewrite_region(scan, _BACKUP_ROOT, _WORLD_ROOT)
        except Exception as exc:
            result["error"] = f"写入失败 {type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_region_dirs(world: Path) -> list[Path]:
    """递归找出所有 region 目录（含 DIM-1 / DIM1 / mod 维度目录）。"""
    return [p for p in sorted(world.rglob("region"))
            if p.is_dir() and any(p.glob("*.mca"))]


def _fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:,.1f} GiB"


def _fmt_secs(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="删除 InhabitedTime 低于阈值的区块（等价于 MCA Selector: InhabitedTime < 15min -> delete）")
    ap.add_argument("--world", required=True, help="存档根目录（包含 level.dat 的那一层）")
    ap.add_argument("--minutes", type=float, default=15.0, help="阈值：加载时长下限，单位分钟，默认 15")
    ap.add_argument("--ticks", type=int, default=None, help="直接用 ticks 指定阈值（覆盖 --minutes）")
    ap.add_argument("--apply", action="store_true", help="真正写盘；不加则只预览")
    ap.add_argument("--backup", default=None, help="备份目录；默认在存档旁边自动创建")
    ap.add_argument("--no-backup", action="store_true", help="不备份（不推荐）")
    ap.add_argument("--missing-as-zero", action="store_true",
                    help="没有 InhabitedTime 字段的区块按 0 处理（MCA Selector 的行为）；默认保留")
    ap.add_argument("--csv", default=None, help="把被删除的区块清单写入 CSV")
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数，0=自动（默认）")
    ap.add_argument("--no-compact", action="store_true",
                    help="快速模式：仅修改文件头，不重写数据（开销低，但不回收磁盘空间）")
    ap.add_argument("--compact-only", action="store_true",
                    help="只压实空洞扇区（不解压、不看阈值），用于 --no-compact 之后回收空间")
    ap.add_argument("--since", type=float, default=None,
                    help="增量模式：只处理最近 N 天内改动过的 region 文件")
    ap.add_argument("--full-decompress", action="store_true",
                    help="调试用：完整解压每个区块（老版本行为，用于对比性能）")
    ap.add_argument("--verbose", action="store_true", help="逐个文件打印明细")
    args = ap.parse_args(argv)

    world = Path(args.world).expanduser().resolve()
    if not world.is_dir():
        print(f"[错误] 存档目录不存在: {world}")
        return 2

    threshold = args.ticks if args.ticks is not None else int(args.minutes * TICKS_PER_MINUTE)

    region_dirs = find_region_dirs(world)
    if not region_dirs:
        print(f"[错误] 在 {world} 下没找到任何 region/*.mca")
        return 2

    files: list[Path] = []
    cutoff = time.time() - args.since * 86400 if args.since else None
    for rdir in region_dirs:
        for mca in sorted(rdir.glob("*.mca")):
            if cutoff is not None and mca.stat().st_mtime < cutoff:
                continue
            files.append(mca)

    total_bytes = sum(f.stat().st_size for f in files)

    print(f"存档: {world}")
    print(f"阈值: InhabitedTime < {threshold} ticks  (≈ {threshold / TICKS_PER_MINUTE:.2f} 分钟)")
    print(f"region 目录: {len(region_dirs)} 个，region 文件: {len(files)} 个，共 {_fmt_size(total_bytes)}")
    if args.compact_only:
        mode = "压实回收（不解压，只压掉空洞扇区）"
    elif args.no_compact:
        mode = "快速模式（只改 8 KB 文件头，不回收磁盘空间）"
    else:
        mode = "压实重写（删区块 + 回收磁盘空间）"
    print(f"模式: {'执行' if args.apply else '预览（不改动任何文件）'}，{mode}")
    print("-" * 78)

    backup_root = None
    if args.apply and not args.no_backup:
        if args.backup:
            backup_root = Path(args.backup).expanduser().resolve()
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_root = world.parent / f"{world.name}_chunkcleaner_backup_{stamp}"
        backup_root.mkdir(parents=True, exist_ok=True)

    if args.jobs:
        jobs = args.jobs
    else:
        jobs = min(16, max(1, (os.cpu_count() or 4) - 1))
        if len(files) < 4:                          # 文件数量较少时不启用进程池
            jobs = 1

    opts = {
        "threshold": threshold,
        "missing_as_zero": args.missing_as_zero,
        "compact": not args.no_compact,
        "apply": args.apply,
        "full_decompress": args.full_decompress,
        "want_rows": bool(args.csv),
        "compact_only": args.compact_only,
    }

    total_present = total_deleted = total_missing = total_unreadable = 0
    prefix_hits = 0
    rewritten = removed = patched = 0
    freed_bytes = 0
    rows: list[tuple] = []
    errors = 0
    t0 = time.time()

    def handle(res: dict) -> None:
        nonlocal total_present, total_deleted, total_missing, total_unreadable
        nonlocal rewritten, removed, patched, errors, prefix_hits, freed_bytes
        if res.get("error") and res.get("present") is None:
            errors += 1
            print(f"  [跳过] {Path(res['path']).name}: {res['error']}")
            return
        total_present += res["present"]
        total_deleted += res["deleted"]
        total_missing += res["missing"]
        total_unreadable += res["unreadable"]
        freed_bytes += res.get("freed") or 0
        prefix_hits += res.get("prefix_hits", 0)
        if res.get("error"):
            errors += 1
            print(f"  [警告] {Path(res['path']).name}: {res['error']}")
        if res["action"] == "rewritten":
            rewritten += 1
        elif res["action"] == "removed":
            removed += 1
        elif res["action"] == "patched":
            patched += 1
        if args.verbose and (res["deleted"] or res["missing"] or res["unreadable"]):
            print(f"  {Path(res['path']).name:<22} 区块 {res['present']:>4}  "
                  f"删除 {res['deleted']:>4}  保留 {res['present'] - res['deleted']:>4}")
        if res["rows"]:
            rows.extend(res["rows"])

    if jobs <= 1:
        _init_worker(str(world), str(backup_root) if backup_root else None, opts)
        done = 0
        step = max(1, len(files) // 20)
        for f in files:
            handle(_work(str(f)))
            done += 1
            if done % step == 0 or done == len(files):
                el = time.time() - t0
                print(f"\r  进度 {done}/{len(files)} 文件 ({done / len(files) * 100:5.1f}%)  "
                      f"已扫 {total_present} 区块  用时 {_fmt_secs(el)}", end="", flush=True)
        if len(files) >= 50:
            print()
    else:
        done = 0
        with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker,
                                 initargs=(str(world), str(backup_root) if backup_root else None, opts)) as ex:
            futures = [ex.submit(_work, str(f)) for f in files]
            step = max(1, len(files) // 20)
            for fut in as_completed(futures):
                handle(fut.result())
                done += 1
                if done % step == 0 or done == len(files):
                    el = time.time() - t0
                    rate = total_present / el if el > 0 else 0
                    eta = (len(files) - done) / (done / el) if el > 0 and done else 0
                    print(f"\r  进度 {done}/{len(files)} 文件 ({done / len(files) * 100:5.1f}%)  "
                          f"已扫 {total_present} 区块 ({rate:,.0f}/s)  用时 {_fmt_secs(el)}  剩余 ~{_fmt_secs(eta)}",
                          end="", flush=True)
        print()

    elapsed = time.time() - t0
    print("-" * 78)

    if args.compact_only:
        print(f"合计: {total_present:,} 个区块，压实后可回收 {_fmt_size(freed_bytes)}"
              f"（{_fmt_size(total_bytes)} -> {_fmt_size(total_bytes - freed_bytes)}），"
              f"耗时 {_fmt_secs(elapsed)}")
        if args.apply:
            print(f"已重写 {rewritten} 个、删除 {removed} 个空 region 文件")
            if backup_root is not None:
                print(f"备份目录: {backup_root}")
        else:
            print("这是预览，未改动任何文件。确认无误后加 --apply 执行（会自动备份）。")
        return 0

    pct = (total_deleted / total_present * 100) if total_present else 0.0
    print(f"合计: 扫描 {total_present:,} 个区块，删除 {total_deleted:,} 个（{pct:.1f}%），"
          f"保留 {total_present - total_deleted:,} 个")
    if total_present:
        print(f"      速度 {total_present / elapsed:,.0f} 区块/秒，"
              f"{total_bytes / 2**20 / elapsed:,.1f} MiB/秒，耗时 {_fmt_secs(elapsed)}")
        if not args.full_decompress:
            print(f"      其中 {prefix_hits:,} 个区块走了「只解压前 {PREFIX_BYTES} 字节」的快路径"
                  f"（{prefix_hits / total_present * 100:.1f}%）")
    if total_missing:
        print(f"      {total_missing:,} 个区块没有 InhabitedTime 字段，已{'删除' if args.missing_as_zero else '保留'}")
    if total_unreadable:
        print(f"      {total_unreadable:,} 个区块读取失败，已全部保留（未动）")
    if errors:
        print(f"      {errors} 个文件处理出错（见上方警告）")

    if args.apply:
        print(f"已重写 {rewritten} 个、快速打补丁 {patched} 个、删除 {removed} 个空 region 文件")
        if backup_root is not None:
            print(f"备份目录: {backup_root}")
    else:
        print("这是预览，未改动任何文件。确认无误后加 --apply 执行（会自动备份）。")

    if args.csv:
        csv_path = Path(args.csv).expanduser().resolve()
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["region", "chunk_x", "chunk_z", "inhabited_ticks", "inhabited_minutes"])
            w.writerows(rows)
        print(f"删除清单已写入: {csv_path}  ({len(rows)} 行)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
