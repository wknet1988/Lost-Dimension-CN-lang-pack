# -*- coding: utf-8 -*-
"""撤销一体化启动图标：删掉桌面/开始菜单里的「Lost Dimension 汉化版.lnk」。

只删这两个快捷方式，不碰游戏文件、不碰 Steam 清单、不碰注册表。
LDZH_Play.vbs 与浮层本体留在原处（删了快捷方式就不会再被触发）。
"""
import os
import sys

NAME = 'Lost Dimension 汉化版.lnk'


def targets():
    home = os.path.expanduser('~')
    desk = os.path.join(home, 'Desktop')
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
    return [os.path.join(d, NAME) for d in (desk, start) if d and os.path.isdir(d)]


def main():
    for p in targets():
        if os.path.isfile(p):
            os.remove(p)
            print('已删除 %s' % p)
        else:
            print('没有 %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
