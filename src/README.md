# 源码说明

这里的代码分三块：**浮层**（`tools/`）、**安装器**（`installer/`）、**运行期数据**（`ps3map/`）。

## 目录

| 路径 | 作用 |
|---|---|
| `tools/ld_overlay.py` | 浮层主体。进程附着、内存读串、指纹查表、窗口绘制、热键、主循环 |
| `tools/probe_overlay.py` | Win32 分层窗口、窗口枚举、进程/模块基址 |
| `tools/detect_boxes.py` | 对白框像素识别：找左右竖向蓝饰条配对成框，与分辨率无关 |
| `tools/ld_bra.py` / `tools/tbb.py` | 归档（.bra）与 TBB 表的读写，供诊断模式使用 |
| `tools/screen_ocr.py` | 实验性画面 OCR（识别屏幕上的英文再回查译文）。**未进发布包**，默认关闭 |
| `tools/build_release.py` | 构建发布包：打 exe、拷数据、生成差分补丁、写文档、打 zip |
| `tools/test_release.py` | 发布包端到端验收：模拟干净机器跑安装/卸载/浮层自检 |
| `tools/make_shortcut.py` / `remove_shortcut.py` | 生成 / 删除桌面与开始菜单快捷方式 |
| `installer/setup_driver.py` | 安装器：找游戏根、校验 md5、备份、打补丁、写启动器、卸载 |
| `ps3map/overlay_index.json` | 译文索引：英文原句指纹 → 中文译文（12,741 条） |
| `ps3map/charname_zh.json` | 角色名表：说话人 ID → 中文全名 + 英文首名 |
| `ps3map/overlay_config.json` | 布局比例、配色、字体、热键 |

## 直接运行浮层（调试）

```bash
python tools/ld_overlay.py selftest     # 离线自检：索引、识别、渲染，不需要游戏
python tools/ld_overlay.py live         # 常驻，等游戏窗口出现后自动附着
python tools/ld_overlay.py diagnose     # 打印内存命中与地址链
```

依赖：`pip install pillow`（`live` 只用标准库 + Pillow）。

`selftest` 里有两步需要正版游戏目录下的 `Data.bra`（从游戏表里取真实码位串做闭环验证），
找不到该文件时会打印「跳过」并继续跑其余步骤，所以从仓库直接跑也能过。

字体默认取 `ps3map/fonts/ZhanYaMing_A.ttf`（不随仓库分发，24 MB）；没有它会回落到
`overlay_config.json` 里 `font.fallback` 指定的系统字体（微软雅黑 / 黑体）。

## 构建发布包

```bash
pip install pillow bsdiff4 pylnk3 pyinstaller
python tools/build_release.py
```

需要游戏根目录下的 `Data.bra`（汉化后）和 `Data.bra.orig_backup`（官方原版）来生成差分补丁。
产物在 `src/publish/LostDimension_ZH_v1.0.zip`。

路径解析对两种布局都成立：

- **工程内**（游戏根 `/work/tools`）——数据在 `/work/ps3map`，输出到 `/work/publish`；
- **本仓库**（`src/tools`）——数据在同级 `src/ps3map`，输出到 `src/publish`。

## 数据格式

### overlay_index.json

```json
{
  "meta":  { "...": "生成参数与统计" },
  "index": {
    "<16 位指纹>": [ { "en": "英文原句", "zh": "中文译文",
                      "ref": "Table/Event/Event100_City.mbb|t0|e2", "n": 43 } ]
  }
}
```

指纹是从游戏内存里读到的**原始码位串**归一化后算出来的摘要；同一个键可能对应多条原文
（游戏里有重复台词），所以值是列表。`ref` 是出处，方便回溯原文核对。

### charname_zh.json

```json
{ "names": { "2": { "zh": "姬野晓", "en": "Himeno Akatsuki", "first": "Akatsuki" } } }
```

`first` 必须显式写，不能用 `en` 的第一个词代替：官方英文对日式角色按「姓 名」顺序，
`Himeno` 是姓。NPC 不填 `first`，名牌就只显示中文名。

## 修改译文

译文索引是从翻译工程生成的。想改某一句，编辑 `ps3map/overlay_index.json` 里对应条目的
`zh` 字段即可（浮层只在启动时读一次，改完重启浮层生效）。
