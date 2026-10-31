#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""region 索引与比对工具。

用于核对两套清理实现的结果是否一致：

  python tools/region_index.py build <region目录> <索引文件>
  python tools/region_index.py diff  <索引A> <索引B> [--baseline <索引>]

build 为每个 region 文件记录区块存在性与 InhabitedTime；diff 比对两份索引，
输出两侧区块集合的差异，并可用改动前的基线索引判断哪一侧处理正确。

索引文件使用 pickle 保存，每项为 (文件名, 区块下标数组, InhabitedTime 数组)，
缺失字段记为 -1。单个 region 目录约产生 60 MB 索引。
"""

from __future__ import annotations

import argparse
import mmap
import pickle
import struct
import sys
from array import array
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gtnh_map_slim as core  # noqa: E402


def _scan_file(path_str: str) -> tuple[str, array, array]:
    """读取单个 region 文件，返回存在的区块下标与对应 InhabitedTime。"""
    path = Path(path_str)
    indices = array("H")
    ticks = array("q")

    if path.stat().st_size < core.HEADER_SIZE:
        return path.name, indices, ticks

    coords = core.parse_region_name(path)
    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            size = len(mm)
            offsets, sectors, _ = core.read_header(mm)
            for i in range(core.CHUNKS_PER_REGION):
                if not offsets[i] or not sectors[i]:
                    continue
                start = offsets[i] * core.SECTOR
                if start + 5 > size:
                    continue
                length = struct.unpack_from(">i", mm, start)[0]
                if length <= 0 or start + 4 + length > size:
                    continue
                ctype = struct.unpack_from(">b", mm, start + 4)[0]
                payload = mm[start + 5:start + 4 + length]
                mcc = None
                if ctype < 0 and coords is not None:
                    cx = coords[0] * 32 + (i % 32)
                    cz = coords[1] * 32 + (i // 32)
                    mcc = core.chunk_mcc_path(path, cx, cz)
                try:
                    inhabited, _dv, _hit = core._chunk_info(payload, ctype, mcc)
                except Exception:
                    inhabited = None
                indices.append(i)
                ticks.append(-1 if inhabited is None else inhabited)
    return path.name, indices, ticks


def build(region_dir: Path, out_path: Path, jobs: int) -> None:
    files = sorted(region_dir.glob("*.mca"))
    if not files:
        raise SystemExit(f"没有找到 region 文件: {region_dir}")

    records = []
    done = 0
    if jobs <= 1:
        for f in files:
            records.append(_scan_file(str(f)))
            done += 1
            _progress(done, len(files))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for result in pool.map(_scan_file, [str(f) for f in files], chunksize=8):
                records.append(result)
                done += 1
                _progress(done, len(files))
    print()

    with out_path.open("wb") as fh:
        pickle.dump(records, fh, protocol=pickle.HIGHEST_PROTOCOL)

    total = sum(len(r[1]) for r in records)
    print(f"索引完成: {len(records)} 个文件，{total:,} 个区块 -> {out_path}")


def _progress(done: int, total: int) -> None:
    if done % 200 == 0 or done == total:
        print(f"\r  扫描 {done}/{total} 文件 ({done / total * 100:5.1f}%)", end="", flush=True)


def load(path: Path) -> dict[str, tuple[array, array]]:
    with path.open("rb") as fh:
        records = pickle.load(fh)
    return {name: (idx, ticks) for name, idx, ticks in records}


def diff(path_a: Path, path_b: Path, baseline: Path | None) -> int:
    a = load(path_a)
    b = load(path_b)
    base = load(baseline) if baseline else None

    names = sorted(set(a) | set(b))

    # 只在一侧存在的文件：区分「仍有区块」与「已清空」
    def split_missing(side: dict, other: dict) -> tuple[list[str], list[str]]:
        with_chunks, empty = [], []
        for n in names:
            if n in side and n not in other:
                (with_chunks if len(side[n][0]) else empty).append(n)
        return with_chunks, empty

    a_only_chunks, a_only_empty = split_missing(a, b)
    b_only_chunks, b_only_empty = split_missing(b, a)

    chunk_only_a = sum(len(a[n][0]) for n in a_only_chunks)   # A 有、B 无
    chunk_only_b = sum(len(b[n][0]) for n in b_only_chunks)   # B 有、A 无
    details: list[tuple[str, int, int | None, int | None]] = []

    for name in names:
        if name not in a or name not in b:
            continue
        set_a = dict(zip(*a[name]))
        set_b = dict(zip(*b[name]))
        for i in set_a.keys() - set_b.keys():
            chunk_only_a += 1
            details.append((name, i, set_a[i], None))
        for i in set_b.keys() - set_a.keys():
            chunk_only_b += 1
            details.append((name, i, None, set_b[i]))

    total_a = sum(len(v[0]) for v in a.values())
    total_b = sum(len(v[0]) for v in b.values())

    print(f"A = {path_a.name}")
    print(f"    文件 {len(a):,}   区块 {total_a:,}")
    print(f"B = {path_b.name}")
    print(f"    文件 {len(b):,}   区块 {total_b:,}")
    print(f"仅在 A 的文件: {len(a_only_chunks) + len(a_only_empty):,}"
          f"（其中仍有区块 {len(a_only_chunks):,}，已清空 {len(a_only_empty):,}）")
    print(f"仅在 B 的文件: {len(b_only_chunks) + len(b_only_empty):,}"
          f"（其中仍有区块 {len(b_only_chunks):,}，已清空 {len(b_only_empty):,}）")
    print(f"区块差异: A 有 B 无 {chunk_only_a:,}   A 无 B 有 {chunk_only_b:,}")

    if not details and not a_only_chunks and not b_only_chunks:
        print("\n两侧区块集合完全一致，无差异。")
        return 0

    wrong_b = 0
    wrong_a = 0
    samples: list[str] = []

    if base:
        for name in a_only_chunks:
            for i, t in zip(*a[name]):
                if t >= 18000 or t < 0:
                    wrong_a += 1
                    if len(samples) < 10:
                        samples.append(f"  {name} 区块 {i}: A 保留、B 删除，原始值 {t}")
        for name in b_only_chunks:
            for i, t in zip(*b[name]):
                if 0 <= t < 18000:
                    wrong_b += 1
                    if len(samples) < 20:
                        samples.append(f"  {name} 区块 {i}: B 保留、A 删除，原始值 {t}")

    print(f"\n差异明细（最多 40 条）:")
    for line in samples:
        print(line)
    shown = 0
    for name, i, va, vb in details[:40]:
        base_ticks = None
        if base and name in base:
            for bi, bt in zip(*base[name]):
                if bi == i:
                    base_ticks = bt
                    break
        verdict = ""
        if base_ticks is not None:
            if base_ticks >= 18000 and vb is None:
                verdict = f"  <- B 误删（原始值 {base_ticks} >= 18000）"
                wrong_b += 1
            elif base_ticks < 18000 and va is None:
                verdict = f"  <- A 误删（原始值 {base_ticks} < 18000）"
                wrong_a += 1
        print(f"  {name} 区块 {i}: A={'保留' if va is not None else '删除'} "
              f"B={'保留' if vb is not None else '删除'}  原始值={base_ticks}{verdict}")
        shown += 1
    if len(details) > shown:
        print(f"  ... 另有 {len(details) - shown} 条")

    if base:
        print(f"\n按基线判定：B 侧误删 {wrong_b} 个区块，A 侧误删 {wrong_a} 个区块")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="region 索引与比对")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build")
    p_build.add_argument("region_dir")
    p_build.add_argument("out")
    p_build.add_argument("--jobs", type=int, default=0)

    p_diff = sub.add_parser("diff")
    p_diff.add_argument("a")
    p_diff.add_argument("b")
    p_diff.add_argument("--baseline", default=None)

    args = ap.parse_args()

    if args.cmd == "build":
        import os
        jobs = args.jobs or min(16, max(1, (os.cpu_count() or 4) - 1))
        build(Path(args.region_dir), Path(args.out), jobs)
        return 0

    return diff(Path(args.a), Path(args.b),
                Path(args.baseline) if args.baseline else None)


if __name__ == "__main__":
    raise SystemExit(main())
