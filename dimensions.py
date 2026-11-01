#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""维度名称与分类映射。

名称来源：GTNH 中文维基「维度」页面的「维度 ID 一览」表格
（https://gtnh.huijiwiki.com/wiki/维度），版本标注 2.9.0。

表格中「维度名」为中文名，「相关mod」为来源模组，「ID」为维度编号。
本模块按维度编号建立映射，并在识别时给出分类与默认勾选状态。

默认不勾选的分类：
  personal    私人维度（家园），玩家自行建造的基地
  station     空间站，Galacticraft 在气体恒星轨道上的站点
  mothership  母舰，UHV 阶段可建造的移动空间站
  technical   模组内部使用的技术维度，不对应可探索地形

前三类通常包含玩家自行建造的基地与设施，默认予以排除；技术维度一并排除，避免误删模组数据
（AE2 空间存储与末影存储内可能存有玩家搬运的建筑与容器内容）。
"""

from __future__ import annotations

import re

# 维度编号 -> (中文名, 来源模组)
DIMENSION_INFO: dict[int, tuple[str, str]] = {
    0: ("主世界", "Minecraft"),
    -1: ("下界", "Minecraft"),
    1: ("末地", "Minecraft"),
    2: ("通灵空间", "RandomThings"),
    7: ("暮色森林", "TwilightForest"),
    25: ("鸟神星", "GalaxySpace"),
    28: ("月球", "Galacticraft"),
    29: ("火星", "Galacticraft"),
    30: ("小行星带", "Galacticraft"),
    31: ("半人马Bb", "GalaxySpace"),
    32: ("巴纳德C", "GalaxySpace"),
    33: ("柯伊伯带", "GalaxySpace"),
    35: ("木卫二", "GalaxySpace"),
    36: ("木卫一", "GalaxySpace"),
    37: ("水星", "GalaxySpace"),
    38: ("火卫一", "GalaxySpace"),
    39: ("金星", "GalaxySpace"),
    40: ("火卫二", "GalaxySpace"),
    41: ("土卫二", "GalaxySpace"),
    42: ("谷神星", "GalaxySpace"),
    43: ("木卫三", "GalaxySpace"),
    44: ("土卫六", "GalaxySpace"),
    45: ("木卫四", "GalaxySpace"),
    46: ("天卫四", "GalaxySpace"),
    47: ("海卫八", "GalaxySpace"),
    48: ("海卫一", "GalaxySpace"),
    49: ("冥王星", "GalaxySpace"),
    50: ("外域", "Thaumcraft"),
    55: ("精神世界", "Witchery"),
    56: ("痛苦迷宫", "Witchery"),
    60: ("基岩矿界", "ThaumicTinkerer"),
    63: ("罗斯128ba", "Bartworks"),
    64: ("罗斯128b", "Bartworks"),
    69: ("口袋世界", "ThaumicHorizons"),
    70: ("镜中世界", "Witchery"),
    81: ("巴纳德E", "GalaxySpace"),
    82: ("巴纳德F", "GalaxySpace"),
    83: ("妊神星", "GalaxySpace"),
    84: ("织女B", "GalaxySpace"),
    85: ("鲸鱼座T星E", "GalaxySpace"),
    86: ("天卫五", "GalaxySpace"),
    90: ("奈佩里", "GalaxySpace"),
    91: ("马赫斯", "GalaxySpace"),
    92: ("阿努比斯", "GalaxySpace"),
    93: ("荷鲁斯", "GalaxySpace"),
    94: ("赛特", "GalaxySpace"),
    95: ("迈罕带", "GalaxySpace"),
    100: ("漆黑世界", "ExtraUtilities"),
    112: ("深渊世界", "ExtraUtilities"),
    173: ("虚空漫步者世界", "Gadomancy"),
    227: ("废土世界", "GT++"),
}

# 维基表格记为「[3,24]，不含 7」的维度为 Galacticraft 空间站
STATION_MIN = 3
STATION_MAX = 24
STATION_EXCLUDE = {7}

# 私人维度从 180 开始连续分配
PERSONAL_MIN = 180

DEFAULT_SELECTED = {
    "overworld": True,
    "dimension": True,
    "unknown": True,
    "personal": False,
    "station": False,
    "mothership": False,
    "technical": False,
}

# 列表排序优先级：主世界在前，技术维度在后
CATEGORY_ORDER = {
    "overworld": 0,
    "dimension": 1,
    "unknown": 2,
    "station": 3,
    "mothership": 4,
    "personal": 5,
    "technical": 6,
}

_DIM_RE = re.compile(r"^DIM(-?\d+)$")
_PERSONAL_RE = re.compile(r"^PERSONAL_DIM_(\d+)$", re.IGNORECASE)
_STATION_RE = re.compile(r"^DIM_SPACESTATION(\d+)$", re.IGNORECASE)
_MOTHERSHIP_RE = re.compile(r"^DIM_MOTHERSHIP(\d+)$", re.IGNORECASE)

# 模组内部使用的技术维度目录名
TECHNICAL_DIRS = {
    "ae2": ("AE2 空间存储", "AppliedEnergistics2"),
    "enderstorage": ("末影存储", "EnderStorage"),
    "dummydata": ("占位数据", "Minecraft"),
    "spatialstorage": ("AE2 空间存储", "AppliedEnergistics2"),
}


def describe(folder: str, is_root: bool = False) -> dict:
    """根据世界目录名给出中文名、来源模组、分类与默认勾选状态。

    is_root 为真时该单元是用户所选目录本身，即主世界。
    返回的 dim_id 用于列表排序，无法判定时为 None。
    """
    if is_root:
        return {"name_zh": "主世界", "mod": "Minecraft", "dim_id": 0,
                "category": "overworld", "default_selected": True}

    match = _PERSONAL_RE.match(folder)
    if match:
        return {"name_zh": "私人维度（家园）", "mod": "PersonalSpace",
                "dim_id": int(match.group(1)),
                "category": "personal", "default_selected": False}

    match = _STATION_RE.match(folder)
    if match:
        return {"name_zh": "空间站", "mod": "Galacticraft",
                "dim_id": int(match.group(1)),
                "category": "station", "default_selected": False}

    match = _MOTHERSHIP_RE.match(folder)
    if match:
        return {"name_zh": "母舰", "mod": "Galacticraft",
                "dim_id": int(match.group(1)),
                "category": "mothership", "default_selected": False}

    technical = TECHNICAL_DIRS.get(folder.casefold())
    if technical:
        name_zh, mod = technical
        return {"name_zh": name_zh, "mod": mod, "dim_id": None,
                "category": "technical", "default_selected": False}

    match = _DIM_RE.match(folder)
    if match:
        dim_id = int(match.group(1))
        if dim_id in DIMENSION_INFO:
            name_zh, mod = DIMENSION_INFO[dim_id]
            return {"name_zh": name_zh, "mod": mod, "dim_id": dim_id,
                    "category": "dimension", "default_selected": True}
        if STATION_MIN <= dim_id <= STATION_MAX and dim_id not in STATION_EXCLUDE:
            return {"name_zh": "空间站", "mod": "Galacticraft", "dim_id": dim_id,
                    "category": "station", "default_selected": False}
        if dim_id >= PERSONAL_MIN:
            return {"name_zh": "私人维度（家园）", "mod": "PersonalSpace", "dim_id": dim_id,
                    "category": "personal", "default_selected": False}
        return {"name_zh": "", "mod": "", "dim_id": dim_id,
                "category": "unknown", "default_selected": True}

    return {"name_zh": "", "mod": "", "dim_id": None,
            "category": "unknown", "default_selected": True}


def label(original: str, name_zh: str) -> str:
    """组合显示名：有中文名时输出「中文名（原名）」，否则只输出原名。"""
    if name_zh:
        return f"{name_zh}（{original}）"
    return original
