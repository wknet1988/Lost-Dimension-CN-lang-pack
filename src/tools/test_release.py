# -*- coding: utf-8 -*-
"""发布包端到端验收：模拟一台干净机器上的安装/卸载全过程。

在 work/publish/_test 下造一个假游戏目录（只有原版 Data.bra），把发布包铺进去，
然后真的运行 setup.exe 与 LDZH_Overlay.exe，校验：
  1) install 后 Data.bra 的 md5 == 汉化版
  2) status 识别正确
  3) uninstall 后 Data.bra 的 md5 == 原版
  4) 浮层 exe 能独立跑起来（selftest：索引 + 字体 + 分层窗口）
测试期间 LDZH_NO_SHORTCUT=1，不会碰桌面上的真快捷方式。
"""
import hashlib
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))      # …\work\tools  或  <仓库>\src\tools
ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
WORK = os.path.join(ROOT, 'work')
if not os.path.isdir(WORK):
    WORK = os.path.dirname(_HERE)                       # 独立仓库布局
PUB = os.path.join(WORK, 'publish', 'LostDimension_ZH_v1.0')
TEST = os.path.join(WORK, 'publish', '_test')
GAME = os.path.join(TEST, 'Lost Dimension')

MD5_VANILLA = 'efa74fdf2ebf5f9a35cb4841eb77e5b2'
MD5_ZH = 'af94d59a4bd23b26e89ae3f8d4a8f6cb'

ok_all = True


def md5_of(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def check(name, cond, extra=''):
    global ok_all
    print('%-42s %s %s' % (name, 'PASS' if cond else 'FAIL', extra))
    if not cond:
        ok_all = False


def run(cmd, label):
    env = dict(os.environ, LDZH_NO_SHORTCUT='1')
    r = subprocess.run(cmd, cwd=os.path.dirname(cmd[0]), env=env,
                       capture_output=True)
    raw = (r.stdout or b'') + (r.stderr or b'')
    # 这些 exe 往控制台写的是系统代码页（中文 Windows = GBK），不是 UTF-8；
    # 先试 GBK 再退回 UTF-8，否则测试会因为乱码而误判成功能失败。
    for enc in ('gbk', 'utf-8'):
        try:
            out = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        out = raw.decode('utf-8', 'replace')
    print('\n----- %s (exit=%d) -----' % (label, r.returncode))
    print(out.strip()[:2000])
    return r.returncode, out


def main():
    global ok_all
    if not os.path.isdir(PUB):
        raise SystemExit('没有发布目录，先跑 build_release.py')
    if os.path.isdir(TEST):
        shutil.rmtree(TEST)
    os.makedirs(GAME)

    # 假游戏目录：只有原版 Data.bra（模拟没装过汉化的正版玩家）
    shutil.copy2(os.path.join(ROOT, 'Data.bra.orig_backup'), os.path.join(GAME, 'Data.bra'))
    check('准备：原版 Data.bra', md5_of(os.path.join(GAME, 'Data.bra')) == MD5_VANILLA)

    # 把发布包解压进游戏目录（就是用户的做法）
    for item in os.listdir(PUB):
        s, d = os.path.join(PUB, item), os.path.join(GAME, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    setup = os.path.join(GAME, 'LDZH', 'setup.exe')
    overlay = os.path.join(GAME, 'LDZH', 'tools', 'LDZH_Overlay.exe')
    check('setup.exe 存在', os.path.isfile(setup))
    check('overlay.exe 存在', os.path.isfile(overlay))

    code, _ = run([setup, 'install'], 'install')
    check('install 退出码 0', code == 0, '(got %d)' % code)
    check('安装后 md5 == 汉化版', md5_of(os.path.join(GAME, 'Data.bra')) == MD5_ZH)
    check('原版备份已生成', os.path.isfile(os.path.join(GAME, 'Data.bra.LDZH_backup')))
    check('启动器 Play.vbs 已写', os.path.isfile(os.path.join(GAME, 'LDZH', 'Play.vbs')))

    code, out = run([setup, 'status'], 'status')
    check('status 认出汉化版', code == 0 and '汉化版' in out)

    # 重复安装：应当识别"已是汉化版"且不改文件
    code, out = run([setup, 'install'], 'install again')
    check('重复安装不报错', code == 0 and '已经是汉化版' in out)

    # 浮层能不能独立跑（打包是否漏模块/字体/索引）
    code, out = run([overlay, 'selftest'], 'overlay selftest')
    check('overlay selftest 退出码 0', code == 0, '(got %d)' % code)
    check('overlay 读到译文索引', '译文索引' in out)
    check('overlay 命中示例台词', 'selftest 译文' in out)
    check('overlay 说出人名', '姬野晓' in out)

    code, _ = run([setup, 'uninstall'], 'uninstall')
    check('uninstall 退出码 0', code == 0)
    check('卸载后 md5 == 原版', md5_of(os.path.join(GAME, 'Data.bra')) == MD5_VANILLA)

    print('\n结果：%s' % ('全部通过' if ok_all else '有失败项'))
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
