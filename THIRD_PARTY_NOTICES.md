# 第三方组件与权利声明

## 字体

**澹雅明体 A / ZhanYaMing-A（`ZhanYaMing_A.ttf`）**

- 基于「一点明体 / I.Ming」衍生的明体字体，采用 **IPA Font License 1.0** 授权。
- 该许可允许再分发、嵌入与商用，条件之一是随附许可全文并保留字体名称说明。
- 许可全文：仓库根目录 [`IPA_Font_License_1.0.txt`](IPA_Font_License_1.0.txt)，发布包内也附有同名文件。
- 发布包**未对字体做子集化或改动**（改动会使其成为 IPA 许可意义上的 Derived Program，需要额外履行改名与回退义务）。

字体在补丁中的用途：浮层渲染中文面板与名字条时的字形来源。

## 游戏资源

《Lost Dimension》（Steam AppID `626600`）的游戏本体、可执行文件、2D 贴图、音频、
原始文本数据等，版权归其原作者与发行商所有。

本仓库**不包含**上述任何原始资源：

- `dist/` 中的安装包内含 `Data.bra.patch`，是基于玩家自有正版文件生成的**二进制增量差分**，
  脱离原版 `Data.bra` 无法还原出任何可用内容，也无法反向获得游戏资源。
- 仓库中不包含 `Data.bra`、`Graphic2D.bra` 或任何游戏文件的完整副本。
- 安装器在打补丁前会用 md5 校验原版文件（`efa74fdf2ebf5f9a35cb4841eb77e5b2`），
  版本不符时拒绝操作，不会修改任何文件。

`docs/images/` 下的预览图是运行时的游戏截图，仅用于说明补丁效果。

## 第三方 Python 库

构建与运行时使用，均通过各自的开源许可分发，未修改后随仓库发布：

| 库 | 用途 | 许可 |
|---|---|---|
| [Pillow](https://python-pillow.org/) | 图像绘制与抓帧 | HPND |
| [bsdiff4](https://github.com/ilanschnell/bsdiff4) | 生成 / 应用二进制差分 | BSD-2-Clause |
| [pylnk3](https://github.com/strayge/pylnk3) | 生成 Windows 快捷方式 | MIT |
| [PyInstaller](https://pyinstaller.org/) | 打包为单文件 exe | GPL-2.0（含 bootloader 例外条款，允许分发打包后的非自由程序） |

## 免责

本项目为学习与技术交流目的制作。请支持正版软件。如相关权利方认为本项目有不妥之处，
请通过 Issue 联系，会立即处理。
