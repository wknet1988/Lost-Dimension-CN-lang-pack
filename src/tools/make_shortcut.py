# -*- coding: utf-8 -*-
"""生成「Lost Dimension 汉化版」一体化启动快捷方式（桌面 + 开始菜单）。

目标 = wscript.exe + LDZH_Play.vbs，图标借 LostDimension.exe。
不写注册表、不改 Steam 清单，随时删掉 .lnk 即可完全撤销。
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
VBS = os.path.join(ROOT, 'LDZH_Play.vbs')
ICON = os.path.join(ROOT, 'LostDimension.exe')
WSCRIPT = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32', 'wscript.exe')
NAME = 'Lost Dimension 汉化版.lnk'


def targets():
    home = os.path.expanduser('~')
    desk = os.path.join(home, 'Desktop')
    # 桌面被 OneDrive 重定向时，注册表里才是真路径
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as k:
            v, _ = winreg.QueryValueEx(k, 'Desktop')
            v = os.path.expandvars(v)
            if os.path.isdir(v):
                desk = v
    except Exception:
        pass
    start = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows',
                         'Start Menu', 'Programs')
    return [p for p in (desk, start) if p and os.path.isdir(p)]


def main():
    import pylnk3
    if not os.path.isfile(VBS):
        print('[ERROR] 缺少 %s' % VBS)
        return 1
    made = []
    for d in targets():
        out = os.path.join(d, NAME)
        pylnk3.for_file(WSCRIPT,
                        lnk_name=out,
                        arguments='"%s"' % VBS,
                        description='Lost Dimension 汉化版：先拉起中文浮层，再启动游戏',
                        icon_file=ICON,
                        icon_index=0,
                        work_dir=ROOT,
                        window_mode=pylnk3.WINDOW_NORMAL)
        made.append(out if os.path.isfile(out) else '[FAIL] ' + out)
    for m in made:
        print(m)
    return 0


if __name__ == '__main__':
    sys.exit(main())
