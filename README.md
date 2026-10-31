# GTNHMapSlim

面向 Minecraft Java 版大型存档（GregTech: New Horizons 等 1.7.10 整合包）的区块清理工具。

核心功能：删除 `InhabitedTime`（区块累计被加载的时长）低于阈值的区块，默认阈值为 15 分钟。
该行为等价于 MCA Selector 中「InhabitedTime < 15min」过滤后执行「删除所选区块」。
用于回收玩家大范围探索产生的大量低价值区块，缩减存档体积并降低服务端区块加载压力。

- 运行时仅依赖 Python 3 标准库，无需 Java 运行环境。
- 默认以预览模式运行，不修改任何文件。
- 写入前自动备份被改动的 region 文件。

## 环境要求

- Python 3.10 及以上（使用 `int | None` 等类型标注语法）
- Windows / Linux / macOS

## 使用方式

```bash
# 预览：统计将被删除的区块，不修改文件
python gtnh_map_slim.py --world "/path/to/saves/GTNH"

# 执行：删除区块并压实 region 文件，回收磁盘空间
python gtnh_map_slim.py --world "/path/to/saves/GTNH" --apply

# 指定阈值并导出被删除区块清单
python gtnh_map_slim.py --world "/path/to/saves/GTNH" --minutes 30 --apply --csv deleted.csv
```

### 参数说明

| 参数 | 说明 |
| --- | --- |
| `--world` | 存档根目录，即包含 `level.dat` 的目录 |
| `--minutes` | 阈值，单位为分钟，默认 `15`；15 分钟对应 18000 ticks |
| `--ticks` | 以 ticks 指定阈值，优先级高于 `--minutes` |
| `--apply` | 实际写入磁盘；未指定时仅预览 |
| `--backup DIR` | 指定备份目录，默认在存档同级目录创建 `存档名_chunkcleaner_backup_时间戳` |
| `--no-backup` | 跳过备份 |
| `--missing-as-zero` | 将缺少 `InhabitedTime` 字段的区块按 0 处理（与 MCA Selector 行为一致），默认保留 |
| `--csv FILE` | 导出被删除区块的坐标与时长 |
| `--jobs N` | 并行进程数，默认按 CPU 核心数自动取值 |
| `--no-compact` | 快速模式：仅修改 region 文件头，不重写数据 |
| `--compact-only` | 仅压实空洞扇区，不解压任何区块 |
| `--since N` | 增量模式：仅处理最近 N 天内改动过的 region 文件 |
| `--full-decompress` | 完整解压每个区块，用于性能对比 |
| `--verbose` | 输出逐文件明细 |
| `--only DIR` | 仅处理指定的 region 目录，可重复指定 |
| `--progress-json` | 以 NDJSON 输出结构化进度，人类可读信息改写到标准错误 |

程序会递归扫描 `region/`、`DIM-1/region/`、`DIM1/region/` 以及模组维度目录下的 `*.mca`。

### 执行模式

| 模式 | 参数 | 效果 | 磁盘空间 |
| --- | --- | --- | --- |
| 预览 | 默认 | 只读扫描，输出统计与清单 | 不变 |
| 快速打补丁 | `--apply --no-compact` | 仅清零文件头中的区块索引，写入开销极低 | 不回收 |
| 压实重写 | `--apply` | 重写 region 文件并压实扇区 | 回收 |
| 压实回收 | `--compact-only --apply` | 不解压，仅按文件头压实扇区 | 回收 |
| 增量 | `--since N` | 仅处理近期变动的 region 文件 | 视模式而定 |

大存档推荐流程：

1. 预览并导出清单，核对将被删除的区域。
2. `--apply --no-compact` 快速生效，进入游戏后对应区块会重新生成。
3. 在停机窗口执行 `--compact-only --apply` 回收磁盘空间。

## 图形界面

双击 `run_gui.bat`，或在命令行执行 `python gui/server.py`。程序会启动本地服务并打开页面。

界面流程：拖入或选择目录 → 自动列出该目录下的全部世界 → 勾选、设定阈值与写入方式 → 点击「精简」。
服务仅监听 `127.0.0.1`，不对外暴露。

存档单元以 `region/` 目录为判定依据。1.7.10 的服务器存档常把每个维度存成独立世界目录
（`DIM-1`、`DIM100`、`PERSONAL_DIM_180` 等，各自带有 `level.dat` 与 `region/`），
因此不能以 `level.dat` 作为世界标志。选择服务器根目录即可一次性处理全部维度。

世界列表同时显示中文名与原始目录名，名称来自 GTNH 中文维基「维度」页面的维度 ID 一览表
（版本 2.9.0），映射见 `dimensions.py`。维基未收录的维度显示为「维基未收录」并保留原始目录名。

以下分类默认不勾选，需要时可在列表中手动勾选：

| 分类 | 目录特征 | 说明 |
| --- | --- | --- |
| 私人维度（家园） | `PERSONAL_DIM_*`、编号 180 起 | 玩家自行建造的基地 |
| 空间站 | `DIM_SPACESTATION*`、编号 3 至 24（不含 7） | Galacticraft 气体采集站 |
| 母舰 | `DIM_MOTHERSHIP*` | UHV 阶段可建造的移动空间站 |
| 技术维度 | `AE2`、`EnderStorage` | 模组内部存储，可能存有玩家搬运的建筑与容器内容 |

浏览器出于安全限制不会向页面提供拖入目录的绝对路径，只能提供名称。
拖入后服务会在本地磁盘上按名称做受限深度搜索；若找到多个同名目录会列出候选。
需要精确定位时使用「选择文件夹」按钮（调用 Windows 原生对话框）或直接粘贴路径。

## 性能

基准环境：Windows，28 核 CPU，合成 1.7.10 存档（每区块约 68 KiB 压缩数据）。
测试规模 30000 区块 / 1.99 GiB，执行 `python bench/bench.py 30000` 可复现。

| 配置 | 耗时 | 吞吐 | 相对基线 |
| --- | --- | --- | --- |
| 基线：完整解压，单进程 | 10.55 s | 2,844 区块/s，189 MiB/s | 1.0x |
| 基线：完整解压，8 进程 | 2.14 s | 14,020 区块/s，931 MiB/s | 4.9x |
| 当前：8 KiB 前缀，单进程 | 1.17 s | 25,740 区块/s，1.7 GiB/s | 9.1x |
| 当前：8 KiB 前缀，8 进程 | 0.73 s | 41,121 区块/s，2.7 GiB/s | 14.5x |

写入性能（同一数据集，删除比例约 30%）：

| 方式 | 耗时 | 结果 |
| --- | --- | --- |
| 压实重写 | 1.25 s | 1993 MiB 降至 1389 MiB |
| 快速打补丁 | 1.00 s | 文件体积不变，仅清除区块索引 |

性能来源：

1. 仅解压每个区块的前 8 KiB。`InhabitedTime` 位于 NBT 前部，无需解压完整区块数据。
2. 仅读取每个区块前 16 KiB 压缩数据，磁盘读取量约为文件体积的四分之一。
3. 使用 mmap 读取 region 文件，避免重复载入大文件。
4. 按 region 文件粒度多进程并行。
5. 仅重写存在删除操作的 region 文件。

## 目录结构

```
gtnh_map_slim.py          主程序与命令行入口
dimensions.py             维度名称映射与默认勾选策略
gui/server.py             图形界面服务（本地 HTTP + JSON 接口）
gui/pick_folder.py        Windows 原生目录选择框
gui/static/               页面资源（HTML / CSS / JS，无框架、无 CDN）
run_gui.bat               Windows 启动脚本
tests/test_map_slim.py    核心逻辑单元测试
tests/test_gui_api.py     界面接口、维度映射与存档单元发现测试
bench/bench.py            性能基准脚本
LICENSE                   MIT 许可证
NOTICE                    第三方项目声明
```

## 测试与基准

```bash
python tests/test_map_slim.py     # 10 项单元测试
python tests/test_gui_api.py      # 20 项界面接口与维度映射测试
python bench/bench.py 30000       # 性能基准，测试数据在临时目录生成并自动清理
```

单元测试覆盖：预览不写入、阈值边界（等于 18000 时不删除）、gzip 与 zlib 压缩、
缺失字段策略、区块全部删除时移除文件、CSV 导出、`--ticks` 覆盖、`--no-compact`、
`--compact-only`、多进程处理，以及保留区块字节一致性校验。

## 判定规则与实现依据

| 环节 | 实现 |
| --- | --- |
| 阈值换算 | `分钟数 x 1200` ticks，等价于 MCA Selector 的 `parseDuration(raw) * 20` |
| 取值位置 | 1.7.10 至 1.17 读取 `Level.InhabitedTime`；1.18 及以后读取根节点 `InhabitedTime` |
| 缺失字段 | 默认保留；`--missing-as-zero` 时按 0 处理 |
| 删除标记 | 清零该区块的偏移、扇区数与时间戳 |
| 写入流程 | 重写 region 文件并压实扇区；区块全部删除时移除该文件及对应 `c.x.z.mcc` |

保留的区块按扇区原样复制，不重新压缩、不重新编码 NBT。

## 风险与限制

- 删除区块等同于该区块回到未生成状态，再次进入时重新生成地形，原有建筑、机器、容器与实体不再存在。
- `InhabitedTime` 仅统计区块被加载的时长，不等同于玩家是否在该区块内有建设行为。
  建议先通过 `--csv` 导出清单核对坐标。
- 跨区块边界的多方块结构或线缆可能出现连接中断。
- 未处理 `entities/`、`poi/` 目录（1.7.10 存档不含该结构）。
- 执行前应关闭游戏或服务端，并确保存在可用的存档副本。

## 许可证

本项目采用 MIT 许可证，详见 `LICENSE`。
判定规则与写入流程参考 MCA Selector（MIT），第三方声明详见 `NOTICE`。
