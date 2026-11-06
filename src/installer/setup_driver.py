# -*- coding: utf-8 -*-
"""Lost Dimension 汉化包 · 安装/卸载器（绿色版）。

用法（由 安装.bat / 卸载.bat 调用）：
    setup.exe install     校验游戏版本 → 备份 → 打补丁 → 生成启动器与快捷方式
    setup.exe uninstall   从备份还原 Data.bra → 删快捷方式
    setup.exe status      只看状态，不改任何东西

设计约束：
  - 只碰 Data.bra（并先备份），不碰 exe / ini / 存档 / 其它资源；
  - 用原版 md5 做闸门：版本不符就拒绝打补丁，绝不覆盖用户文件；
  - 所有输出用中文，终端直接能看懂。
"""
import hashlib
import os
import shutil
import sys

# --- 版本闸门（这两个 md5 是本项目实测值，换汉化版本时必须同步更新） -------
MD5_VANILLA = 'efa74fdf2ebf5f9a35cb4841eb77e5b2'   # 官方原版 Data.bra 1,788,459 B
MD5_ZH = 'af94d59a4bd23b26e89ae3f8d4a8f6cb'        # 汉化版 Data.bra 3,790,412 B
SIZE_VANILLA = 1788459

APPID = '626600'
LINK_NAME = 'Lost Dimension 汉化版.lnk'
# 自动化测试时置 1：不碰桌面/开始菜单（免得起草稿测试把真快捷方式删掉）
SKIP_LNK = os.environ.get('LDZH_NO_SHORTCUT') == '1'

def _find_root(start):
    """从工具所在目录向上找游戏根（认 LostDimension.exe / Data.bra）。

    用户有时会把压缩包解压成「Lost Dimension / LostDimension_ZH_v1.0 / …」这种
    多套一层的结构，这里也能装对；最多向上找 3 层。
    """
    cur = start
    for _ in range(4):
        if (os.path.isfile(os.path.join(cur, 'LostDimension.exe'))
                or os.path.isfile(os.path.join(cur, 'Data.bra'))):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            break
        cur = up
    return os.path.dirname(start)


HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False)
                                      else __file__))
ROOT = _find_root(HERE)                          # 游戏根目录（本工具放在 <root>/…/LDZH）
DATA = os.path.join(ROOT, 'Data.bra')
BACKUP = os.path.join(ROOT, 'Data.bra.LDZH_backup')
PATCH = os.path.join(HERE, 'Data.bra.patch')
TOOLS = os.path.join(HERE, 'tools')


def say(msg=''):
    try:
        print(msg, flush=True)
    except Exception:
        pass


def md5_of(path, limit=None):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def die(msg, code=1):
    say('')
    say('[失败] %s' % msg)
    say('')
    return code


# ---------------------------------------------------------------- 快捷方式
def lnk_targets():
    home = os.path.expanduser('~')
    desk = os.path.join(home, 'Desktop')
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer'
                            r'\User Shell Folders') as k:
            v, _ = winreg.QueryValueEx(k, 'Desktop')
            v = os.path.expandvars(v)
            if os.path.isdir(v):
                desk = v
    except Exception:
        pass
    start = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows',
                         'Start Menu', 'Programs')
    return [d for d in (desk, start) if d and os.path.isdir(d)]


def write_launcher():
    """生成 LDZH/Play.vbs：先起浮层（后台），再通过 Steam 启动游戏。

    路径全部由脚本自身位置推导，整个文件夹可以搬到任何盘符。
    """
    vbs = os.path.join(HERE, 'Play.vbs')
    body = (
        "Option Explicit\n"
        "Dim sh, fso, here, exe\n"
        "Set sh = CreateObject(\"WScript.Shell\")\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
        "here = fso.GetParentFolderName(WScript.ScriptFullName)\n"
        "exe = here & \"\\tools\\LDZH_Overlay.exe\"\n"
        "If Not fso.FileExists(exe) Then\n"
        "    MsgBox \"Overlay not found: \" & exe, 16, \"Lost Dimension\"\n"
        "    WScript.Quit 1\n"
        "End If\n"
        "sh.CurrentDirectory = here\n"
        "sh.Run \"\"\"\" & exe & \"\"\" live\", 0, False\n"
        "WScript.Sleep 1200\n"
        "sh.Run \"steam://rungameid/" + APPID + "\", 1, False\n"
    )
    with open(vbs, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(body)
    return vbs


def make_shortcuts():
    vbs = write_launcher()          # 启动器本身总要生成（测试里也要验证）
    if SKIP_LNK:
        say('（测试模式：跳过桌面/开始菜单快捷方式）')
        return []
    made = []
    try:
        import pylnk3
    except Exception:
        return made
    icon = os.path.join(ROOT, 'LostDimension.exe')
    if not os.path.isfile(icon):
        icon = os.path.join(TOOLS, 'LDZH_Overlay.exe')
    for d in lnk_targets():
        out = os.path.join(d, LINK_NAME)
        try:
            pylnk3.for_file(os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                                         'System32', 'wscript.exe'),
                            lnk_name=out,
                            arguments='"%s"' % vbs,
                            description='Lost Dimension 汉化版：先拉起中文浮层，再启动游戏',
                            icon_file=icon, icon_index=0, work_dir=ROOT,
                            window_mode=pylnk3.WINDOW_NORMAL)
            made.append(out)
        except Exception as e:
            say('  [警告] 快捷方式创建失败 %s：%r' % (out, e))
    return made


def remove_shortcuts():
    if SKIP_LNK:
        return 0
    n = 0
    for d in lnk_targets():
        p = os.path.join(d, LINK_NAME)
        if os.path.isfile(p):
            try:
                os.remove(p)
                n += 1
            except Exception:
                pass
    return n


# ---------------------------------------------------------------- 安装
def install():
    say('=' * 62)
    say(' Lost Dimension 汉化包 · 安装')
    say('=' * 62)
    say('游戏目录：%s' % ROOT)
    say('')

    if not os.path.isfile(DATA):
        say('这个文件夹里没有 Data.bra。')
        say('请把压缩包**解压到游戏目录**（和 LostDimension.exe 同一层）后再运行。')
        return 1
    if not os.path.isfile(PATCH):
        return die('缺少补丁文件 %s' % PATCH)

    cur = md5_of(DATA)
    size = os.path.getsize(DATA)
    say('当前 Data.bra：%d 字节  md5=%s' % (size, cur))

    if cur == MD5_ZH:
        say('')
        say('已经是汉化版了，无需重复安装。')
        make_shortcuts()
        say('已刷新桌面/开始菜单快捷方式。')
        return 0
    if cur != MD5_VANILLA:
        say('')
        say('[!] 游戏版本不匹配，已停止（没有改动任何文件）。')
        say('    期望原版：%d 字节  md5=%s' % (SIZE_VANILLA, MD5_VANILLA))
        say('    实际文件：%d 字节  md5=%s' % (size, cur))
        say('')
        say('常见原因：Steam 更新过游戏、装过其它汉化/Mod、或文件损坏。')
        say('处理办法：Steam 库里右键游戏 → 属性 → 已安装文件 → 验证游戏文件完整性，')
        say('          再重新运行本安装器。')
        return 2

    if not os.path.isfile(BACKUP):
        say('备份原版 → %s' % os.path.basename(BACKUP))
        shutil.copy2(DATA, BACKUP)
    else:
        say('已有备份 %s（保留不覆盖）' % os.path.basename(BACKUP))

    say('正在打补丁…')
    try:
        import bsdiff4
        src = open(DATA, 'rb').read()
        patch = open(PATCH, 'rb').read()
        out = bsdiff4.patch(src, patch)
        del src
    except Exception as e:
        return die('打补丁出错：%r（原文件仍在，未被改动）' % (e,))

    got = hashlib.md5(out).hexdigest()
    if got != MD5_ZH:
        return die('补丁结果校验不一致（%s），已放弃写入。' % got)

    tmp = DATA + '.ldzh_tmp'
    with open(tmp, 'wb') as f:
        f.write(out)
    os.replace(tmp, DATA)
    del out
    say('已写入：%d 字节  md5=%s' % (os.path.getsize(DATA), md5_of(DATA)))

    made = make_shortcuts()
    say('')
    for m in made:
        say('快捷方式 → %s' % m)
    say('')
    say('安装完成。以后从这里启动带浮层：')
    say('  桌面 / 开始菜单的「Lost Dimension 汉化版」')
    say('浮层随游戏启动、随游戏退出，不常驻、不开机自启。')
    return 0


# ---------------------------------------------------------------- 卸载
def uninstall():
    say('=' * 62)
    say(' Lost Dimension 汉化包 · 卸载')
    say('=' * 62)

    n = remove_shortcuts()
    say('已删除 %d 个快捷方式。' % n)

    if not os.path.isfile(BACKUP):
        say('')
        say('没有找到备份（可能本来就没装过，或备份被手动删了）。')
        say('如果 Data.bra 还是汉化版，可以在 Steam 里「验证游戏文件完整性」还原。')
        return 0

    say('正在从备份还原 Data.bra…')
    try:
        shutil.copy2(BACKUP, DATA)
    except Exception as e:
        return die('还原失败：%r' % (e,))
    got = md5_of(DATA)
    say('已还原：%d 字节  md5=%s %s'
        % (os.path.getsize(DATA), got, '（原版）' if got == MD5_VANILLA else '（注意：与原版不一致）'))
    say('')
    say('卸载完成。备份文件保留在：%s' % BACKUP)
    say('确认游戏正常后可以手动删掉它。')
    return 0


def status():
    say('游戏目录：%s' % ROOT)
    if not os.path.isfile(DATA):
        say('Data.bra：不存在')
        return 1
    cur = md5_of(DATA)
    kind = ('原版' if cur == MD5_VANILLA else
            '汉化版' if cur == MD5_ZH else '未知（第三方修改或版本不同）')
    say('Data.bra：%d 字节  %s' % (os.path.getsize(DATA), kind))
    say('备份：%s' % ('有' if os.path.isfile(BACKUP) else '无'))
    say('浮层：%s' % ('已安装' if os.path.isfile(os.path.join(TOOLS, 'LDZH_Overlay.exe'))
                     else '缺失'))
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else 'install'
    if mode == 'install':
        return install()
    if mode == 'uninstall':
        return uninstall()
    if mode == 'status':
        return status()
    say('用法：setup.exe install | uninstall | status')
    return 1


if __name__ == '__main__':
    sys.exit(main())
