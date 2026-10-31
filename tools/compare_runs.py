#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对两套清理实现的运行结果做决策层比对。

输入三份索引：改动前的基线、A 侧结果、B 侧结果。
对每个文件计算三份区块集合：

  期望值      由基线 InhabitedTime 与阈值直接推导
  A 侧实际    从 A 侧 region 目录读出
  B 侧实际    从 B 侧 region 目录读出

随后分别核对两侧实际结果是否符合期望，并给出两侧之间的差异及成因。

用法：
  python tools/compare_runs.py <基线.pkl> <A结果.pkl> <B结果.pkl> --threshold 18000
"""

from __future__ import annotations

import argparse
import pickle
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load(path: Path) -> dict[str, dict[int, int]]:
    with path.open("rb") as fh:
        records = pickle.load(fh)
    return {name: dict(zip(idx, ticks)) for name, idx, ticks in records}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--threshold", type=int, default=18000)
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    args = ap.parse_args()

    base = load(Path(args.baseline))
    a = load(Path(args.a))
    b = load(Path(args.b))
    th = args.threshold

    total = sum(len(v) for v in base.values())
    miss = sum(1 for v in base.values() for t in v.values() if t < 0)

    # 期望：缺字段按保留（本项目默认策略）
    expect_keep_mine = {n: {i for i, t in v.items() if t < 0 or t >= th} for n, v in base.items()}
    # 期望：缺字段按 0（MCA Selector 策略）
    expect_keep_mca = {n: {i for i, t in v.items() if t >= th} for n, v in base.items()}

    def actual(side: dict[str, dict[int, int]], name: str) -> set[int]:
        return set(side.get(name, {}))

    # 逐侧核对
    def audit(side, expect, label):
        over = under = files_bad = 0
        samples = []
        for name, exp in expect.items():
            act = actual(side, name)
            extra = act - exp
            lost = exp - act
            if extra or lost:
                files_bad += 1
                over += len(extra)
                under += len(lost)
                if len(samples) < 6:
                    samples.append((name, len(extra), len(lost),
                                    sorted(lost)[:5]))
        print(f"[{label}] 应保留 {sum(len(v) for v in expect.values()):,} 个区块，"
              f"实际保留 {sum(len(v) for v in side.values()):,} 个")
        print(f"       结果不符的文件 {files_bad:,}；多保留 {over:,}；少保留 {under:,}")
        for s in samples:
            print(f"         {s[0]}: 多 {s[1]} 少 {s[2]}，缺失下标样例 {s[3]}")
        return over, under, files_bad

    print(f"基线区块总数 {total:,}，其中缺 InhabitedTime 字段 {miss:,}\n")
    audit(b, expect_keep_mine, args.label_b)
    print()
    audit(a, expect_keep_mca, args.label_a)
    print()

    # 两侧差异
    only_a = only_b = 0
    reasons = Counter()
    samples = []
    for name, exp in expect_keep_mine.items():
        sa = actual(a, name)
        sb = actual(b, name)
        for i in sa - sb:
            only_a += 1
            t = base[name].get(i)
            reasons["A 保留 B 删除（原始值 < 阈值）" if (t is not None and 0 <= t < th)
                    else "A 保留 B 删除（缺字段）" if (t is None or t < 0)
                    else "A 保留 B 删除（其他）"] += 1
            if len(samples) < 8:
                samples.append((name, i, t, "A有B无"))
        for i in sb - sa:
            only_b += 1
            t = base[name].get(i)
            reasons["A 删除 B 保留（原始值 >= 阈值）" if (t is not None and t >= th)
                    else "A 删除 B 保留（缺字段）" if (t is None or t < 0)
                    else "A 删除 B 保留（其他）"] += 1
            if len(samples) < 8:
                samples.append((name, i, t, "A无B有"))

    print(f"两侧差异：{args.label_a} 有 {only_a:,} 个 {args.label_b} 没有；"
          f"{args.label_b} 有 {only_b:,} 个 {args.label_a} 没有")
    for k, v in reasons.most_common():
        print(f"  {k}: {v:,}")
    for s in samples:
        print(f"  样例 {s[0]} 区块 {s[1]} 原始值={s[2]}  {s[3]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
