# -*- coding: utf-8 -*-
"""构建《Lost Dimension》汉化绿色版发布包。

产出：
    work/publish/LostDimension_ZH_v1.0/          （目录，可直接解压到游戏根）
    work/publish/LostDimension_ZH_v1.0.zip       （最终发布物）

目录结构（用户解压到游戏根之后）：
    <游戏根>/
      安装.bat  卸载.bat  说明.txt  IPA_Font_License_1.0.txt
      LDZH/
        setup.exe               安装/卸载器
        Data.bra.patch          2.1MB 差分补丁（不含游戏原始资源）
        tools/LDZH_Overlay.exe  浮层（免 Python）
        ps3map/*.json + fonts/  译文索引与字体
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))      # …\work\tools  或  <仓库>\src\tools
ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
WORK = os.path.join(ROOT, 'work')
if not os.path.isdir(WORK):
    # 独立仓库布局：源码直接放在 src/ 下，数据在同级的 ps3map/
    WORK = os.path.dirname(_HERE)
PUB = os.path.join(WORK, 'publish')
NAME = 'LostDimension_ZH_v1.0'
OUT = os.path.join(PUB, NAME)
BUILD = os.path.join(PUB, 'build')
PY = sys.executable
TOOLS = os.path.join(WORK, 'tools')
PS3 = os.path.join(WORK, 'ps3map')
# 安装器的两种摆放位置：原始工程 work/publish/src/，仓库内 src/installer/
SETUP_SRC = next((p for p in (os.path.join(PUB, 'src', 'setup_driver.py'),
                              os.path.join(ROOT, 'src', 'installer', 'setup_driver.py'))
                  if os.path.isfile(p)), os.path.join(PUB, 'src', 'setup_driver.py'))

sys.path.insert(0, TOOLS)


def sh(cmd, **kw):
    print('> ' + ' '.join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    if r.returncode != 0:
        raise SystemExit('命令失败（%d）：%s' % (r.returncode, ' '.join(cmd)))


def clean():
    for d in (OUT, BUILD):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(OUT, 'LDZH', 'tools'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'LDZH', 'ps3map', 'fonts'), exist_ok=True)
    os.makedirs(BUILD, exist_ok=True)


def copy_data():
    # 1) 差分补丁
    patch = os.path.join(PUB, 'Data.bra.patch')
    if not os.path.isfile(patch):
        import bsdiff4
        a = open(os.path.join(ROOT, 'Data.bra.orig_backup'), 'rb').read()
        b = open(os.path.join(ROOT, 'Data.bra'), 'rb').read()
        open(patch, 'wb').write(bsdiff4.diff(a, b))
    shutil.copy2(patch, os.path.join(OUT, 'LDZH', 'Data.bra.patch'))

    # 2) 浮层运行期数据
    shutil.copy2(os.path.join(PS3, 'overlay_index.json'),
                 os.path.join(OUT, 'LDZH', 'ps3map', 'overlay_index.json'))
    shutil.copy2(os.path.join(PS3, 'charname_zh.json'),
                 os.path.join(OUT, 'LDZH', 'ps3map', 'charname_zh.json'))
    shutil.copy2(os.path.join(PS3, 'fonts', 'ZhanYaMing_A.ttf'),
                 os.path.join(OUT, 'LDZH', 'ps3map', 'fonts', 'ZhanYaMing_A.ttf'))

    # 3) 配置：发布版关掉画面识别热键（该模块不进发布包），字体回落到系统字体
    cfg = json.load(open(os.path.join(PS3, 'overlay_config.json'), encoding='utf-8'))
    cfg.setdefault('hotkeys', {})['screen_vk'] = 0
    cfg['font']['fallback'] = ['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf']
    with open(os.path.join(OUT, 'LDZH', 'ps3map', 'overlay_config.json'),
              'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def build_overlay():
    sh([PY, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--noupx',
        '--name', 'LDZH_Overlay',
        '--distpath', os.path.join(OUT, 'LDZH', 'tools'),
        '--workpath', os.path.join(BUILD, 'ov'),
        '--specpath', BUILD,
        '--paths', TOOLS,
        '--exclude-module', 'numpy',
        '--exclude-module', 'screen_ocr',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'matplotlib',
        os.path.join(TOOLS, 'ld_overlay.py')])


def build_setup():
    sh([PY, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--noupx',
        '--name', 'setup',
        '--distpath', os.path.join(OUT, 'LDZH'),
        '--workpath', os.path.join(BUILD, 'setup'),
        '--specpath', BUILD,
        '--hidden-import', 'bsdiff4',
        '--hidden-import', 'pylnk3',
        '--exclude-module', 'numpy',
        '--exclude-module', 'PIL',
        '--exclude-module', 'tkinter',
        SETUP_SRC])


BAT_INSTALL = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lost Dimension 汉化包
if not exist "%~dp0LDZH\setup.exe" (
  echo 找不到 LDZH\setup.exe，压缩包可能没有完整解压。
  pause
  exit /b 1
)
"%~dp0LDZH\setup.exe" install
echo.
pause
'''

BAT_UNINSTALL = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lost Dimension 汉化包 - 卸载
if not exist "%~dp0LDZH\setup.exe" (
  echo 找不到 LDZH\setup.exe。
  pause
  exit /b 1
)
"%~dp0LDZH\setup.exe" uninstall
echo.
pause
'''

README = '''Lost Dimension 简体中文补丁 v1.0
================================

这是什么
--------
把《Lost Dimension》的剧情对白翻成中文。游戏本体文件只改一个字库归档
（Data.bra），其余全部由外挂浮层绘制——浮层只读游戏内存、不改进程、不注入。

注意事项（先读）
----------------
1. 只支持**窗口模式**。全屏 / 无边框全屏下，浮层可能不显示、或者
   和对白框对不齐（游戏本身不受影响，只是看不到中文）。
   请在游戏内「设置 → 画面」里选窗口模式运行。
2. **只汉化了剧情部分**。剧情对白、事件台词、说话人名字牌是中文；
   系统菜单、道具/装备/出击列表、技能与敌人名、CONFIG 设置项仍是官方
   英文，这是游戏字库机制的限制，不是漏翻（详见文末「已知限制」）。

安装（30 秒）
-------------
1. 完全退出游戏。
2. 把压缩包解压到**游戏目录**，也就是和 LostDimension.exe 同一层：

     ...\\steamapps\\common\\Lost Dimension\\

   解压完会多出一个 LostDimension_ZH_v1.0 文件夹，进去双击 安装.bat。
   （多套一层没关系，安装器会自己往上找游戏根。）
3. 看到「安装完成」就好了。
4. 以后从桌面或开始菜单的「Lost Dimension 汉化版」启动游戏。

为什么必须用那个图标
--------------------
浮层需要在游戏之前起来等候。这个图标做的事是：先悄悄地拉起浮层，
再通过 Steam 启动游戏。从 Steam 库或直接双击 exe 进去不会带浮层。

另附：日语语音包
----------------
PC 版默认是英文语音。需要日语语音的话，这里另附一份日语语音包，
和本中文补丁互相独立、可以单独使用：

  https://drive.google.com/file/d/1vSILToNcUuJhm0Mp_9hFgH2k7nRJHC6Q/view?usp=drive_link

- 它不是本补丁的组成部分，本补丁压缩包里没有任何音频文件，
  装不装都不影响中文显示。
- 装之前请自己先备份会被替换的文件——安装.bat 只备份 Data.bra，
  不会去碰语音相关文件。
- 具体装法以语音包内附的说明为准。

浮层的特性
----------
- 只读附着，不写游戏内存，不注入 DLL，不联网；
- 中文面板盖住原生英文对白框，蓝色名牌上显示说话人中文名；
- 随游戏启动、随游戏退出，不常驻、不开机自启、不占后台；
- Ctrl+F12 显隐，Ctrl+F11 遮挡/浮标，Ctrl+F10 自动识别/固定比例。

卸载
----
双击 卸载.bat，会从备份还原 Data.bra 并删掉快捷方式。
也可以直接在 Steam 里「验证游戏文件完整性」，效果一样。
卸载后整个 LostDimension_ZH_v1.0 文件夹可以直接删掉。

Steam 自动更新会盖掉汉化
------------------------
Steam 校验到 Data.bra 和服务器不一致时会重新下载覆盖它，汉化就失效了
（游戏本身不会坏）。这时重新双击一次 安装.bat 即可。
如果安装器提示「版本不匹配」，先验证游戏完整性再装。

杀软误报
--------
LDZH_Overlay.exe 会读取游戏进程的内存（只读，用来取当前这句台词），
某些杀软会因此报毒。加信任即可；补丁不含任何可执行代码注入。

备份在哪
--------
安装时原版 Data.bra 会备份成游戏目录下的 Data.bra.LDZH_backup，
不会自动删除。确认游戏正常后可以手动删掉它。

已知限制
--------
- 本补丁**只覆盖剧情**。背包/装备/出击/道具列表、技能与敌人名、CONFIG 菜单
  全部保持官方英文，不提供中文。
- 列表类文本没法中文化是游戏绘字机制导致的：只有 420 个字形槽，汉字放进去
  会被引擎直接丢弃（早期版本试过，整列变空白），所以这部分保持原版。
- CONFIG 等菜单的**设置项名称和取值是贴图**，属于改图范畴，本补丁不动。
- 只支持窗口模式运行，全屏 / 无边框全屏不保证显示。
- 只支持这个 Steam 版本（Data.bra 1,788,459 字节）。游戏更新后，
  安装器会检测到版本不符并拒绝打补丁，不会破坏你的文件。

版权
----
- 本补丁不包含任何游戏原始资源，发布的是差分数据；你必须拥有正版游戏。
- 字体：澹雅明体（IPA Font License 1.0），可随包分发，许可全文见
  IPA_Font_License_1.0.txt。
- 补丁免费提供，禁止用于售卖。
- 《Lost Dimension》版权归 FURYU / Lancarse 及其发行方所有。
'''


def write_docs():
    # .bat 用 UTF-8（无 BOM）+ 首行 chcp 65001：cmd 是逐行读取脚本的，
    # 切了代码页之后的中文 echo 才正常。若用 GBK 保存又 chcp 65001，
    # 中文必然乱码；setup.exe 的输出也是 UTF-8，两边必须一致。
    # 说明.txt 用 UTF-8 BOM，记事本才认得出来。
    def w(name, text, enc='utf-8'):
        p = os.path.join(OUT, name)
        with open(p, 'w', encoding=enc, newline='\r\n') as f:
            f.write(text)
        return p

    w('安装.bat', BAT_INSTALL, 'utf-8')
    w('卸载.bat', BAT_UNINSTALL, 'utf-8')
    w('说明.txt', README, 'utf-8-sig')
    lic = os.path.join(PUB, 'src', 'IPA_Font_License_1.0.txt')
    if os.path.isfile(lic):
        shutil.copy2(lic, os.path.join(OUT, 'IPA_Font_License_1.0.txt'))


def make_zip():
    zpath = os.path.join(PUB, NAME + '.zip')
    if os.path.isfile(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(OUT):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, OUT)
                z.write(full, os.path.join(NAME, rel))
    return zpath


def main():
    zip_only = '--zip-only' in sys.argv      # 只更新文档与压缩包，不重编译 exe
    if zip_only:
        write_docs()
    else:
        clean()
        copy_data()
        write_docs()
        build_overlay()
        build_setup()
    total = sum(os.path.getsize(os.path.join(b, f))
                for b, _, fs in os.walk(OUT) for f in fs)
    print('\n发布目录：%s（%.1f MB）' % (OUT, total / 1048576.0))
    for b, _, fs in os.walk(OUT):
        for f in sorted(fs):
            p = os.path.join(b, f)
            print('  %8.2f MB  %s' % (os.path.getsize(p) / 1048576.0,
                                      os.path.relpath(p, OUT)))
    z = make_zip()
    print('\n压缩包：%s（%.1f MB）' % (z, os.path.getsize(z) / 1048576.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
