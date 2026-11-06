# -*- coding: utf-8 -*-
"""《Lost Dimension》台词覆盖层（只读·不注入·不改包）。

架构分工（2026-11-05 用户定调）：
  - CONFIG / UI 文本：仍走游戏原生资源（Data.bra 文本表 + 原字库）；
  - 剧情台词：改由本覆盖层用系统字体显示**完整译文**，
    从而绕开对白工作纹理的 420 码位硬墙，不需要收敛用字。

运行时链路：
  只读附着已在运行的游戏 → 从消息对象 / 当前表定位「这一句」的 u16 码位串
   → md5 指纹 O(1) 查 overlay_index.json → 完整中文译文
   → Win32 分层窗口绘制（绑定游戏窗体：仅在游戏前台显示，不外溢到桌面/其它应用）。

显示布局（2026-11-06 用户定调）：**对齐并盖住原生对白框**。
面板坐标按客户区比例取自实机对白截图，默认完全不透明，
原生英文台词显示与否不重要。
**名字牌用汉字**：覆盖层另画一条蓝色名字牌盖住原生英文，
说话人 ID 直接从台词串的 `0x8096 <角色ID>` 控制码读出（切换时才出现，
故沿用上一次 ID），汉字名表见 `charname_zh.json`。
CONFIG / UI 菜单不在覆盖范围内。

严格只读：不启动游戏、不写进程内存、不改 Data.bra / exe / ini。
本程序只附着**已经运行**的游戏窗口；启动游戏由用户自行控制。

用法（需带 PIL 的 Python，见文末启动器说明）：
  python ld_overlay.py selftest      # 离线：索引查表 + 分层窗口绘制 + 对齐预览图
  python ld_overlay.py diagnose     # 只读：附着，打印地址链与命中（需批准）
  python ld_overlay.py live         # 随游戏启动的浮层：等游戏出现即挂载（需批准）
                                    # 默认：画面识别对白框位置（任意分辨率/黑边
                                    # /上下双开自动适配），中文面板精确压框盖原文
  python ld_overlay.py live --manual     # 回落：固定比例定位（不再推荐）
  python ld_overlay.py live --float      # 可选：半透明浮标（不遮挡原生台词）
生命周期：游戏没开 → 等待；游戏开着 → 挂载并在游戏获得焦点时抢占最前端；
      切到其它应用/最小化 → 只隐藏；**游戏退出 → 浮层随之退出**。不做开机自启。
热键（全部需按住 Ctrl，避免与游戏按键冲突）：Ctrl+F12 显隐 / Ctrl+F11 遮挡-浮标 /
      Ctrl+F10 识别-固定比例。没有退出热键（游戏退出即自行退出）。
"""
import os, sys, io, json, time, struct, re, threading, ctypes as C
import unicodedata
from ctypes import wintypes as W

if getattr(sys, 'frozen', False):
    # 打包成 exe 之后 __file__ 指向 PyInstaller 的临时解包目录，
    # 必须按 exe 自身的真实位置推导，否则 WORK/ROOT 全错、索引和字体都找不到。
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
ROOT = os.path.dirname(WORK)


def _crash_hook(t, e, tb):
    """pythonw 没有控制台：任何未捕获异常都写进 overlay/crash.log，避免静默死亡。"""
    try:
        import traceback
        d = os.path.join(WORK, 'overlay')
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(os.path.join(d, 'crash.log'), 'a', encoding='utf-8') as f:
            f.write('\n==== %s ====\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
            f.write('argv=%r\n' % (sys.argv,))
            f.write(''.join(traceback.format_exception(t, e, tb)))
    except Exception:
        pass


sys.excepthook = _crash_hook

sys.path.insert(0, HERE)
import probe_overlay as p          # 复用已验证的 Win32 分层窗口 / 指纹 / 窗口枚举
import detect_boxes                # 对白框像素识别（饰条配对，分辨率无关）

from PIL import Image, ImageDraw, ImageFont

CFG = os.path.join(WORK, 'ps3map', 'overlay_config.json')
IDX = os.path.join(WORK, 'ps3map', 'overlay_index.json')
NAMES = os.path.join(WORK, 'ps3map', 'charname_zh.json')
OUT = os.path.join(WORK, 'overlay')
SPEAKER_TAG = 0x8096          # 台词串内嵌的「切换说话人」控制码
k32, u32, g32 = p.k32, p.u32, p.g32
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
DUMP_BASE = 0x3C0000   # 离线反汇编假定的模块基址（build_overlay_map.py: MOD_VA=0x3C0000）；
                       # 实机 exe 由 ASLR 加载到不同基址，地址需按 (实机基址 - DUMP_BASE) 重定位


def get_module_base(pid):
    """枚举进程模块，返回主模块 LostDimension.exe 的实装基址（ASLR 下每次运行不同）。
    任何枚举失败都返回 None（不允许抛异常把整个浮层带崩）。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        psapi = C.windll.psapi
        psapi.EnumProcessModules.argtypes = [W.HANDLE, C.c_void_p, C.c_ulong,
                                             C.POINTER(C.c_ulong)]
        psapi.GetModuleFileNameExW.argtypes = [W.HANDLE, C.c_void_p,
                                              C.c_wchar_p, C.c_ulong]
        hMod = (W.HANDLE * 1024)()
        cb = C.c_ulong(0)
        if not psapi.EnumProcessModules(h, C.byref(hMod), C.sizeof(hMod), C.byref(cb)):
            return None
        n = min(cb.value // C.sizeof(W.HANDLE), 1024)
        for i in range(n):
            buf = C.create_unicode_buffer(260)
            if not psapi.GetModuleFileNameExW(h, hMod[i], buf, 260):
                continue
            if 'LostDimension.exe' not in buf.value:
                continue
            # 基址必须真的是本进程内的 exe 镜像（首两字节 MZ）。
            # EnumProcessModules 偶发返回不可靠的 hMod，直接拿去做重定位会让
            # 整条地址链落在别处（读回全 0）——那一次就是这么"彻底不显示"的。
            mb = C.create_string_buffer(2)
            got = C.c_size_t(0)
            if k32.ReadProcessMemory(h, hMod[i], mb, 2, C.byref(got)) \
                    and got.value == 2 and mb.raw == b'MZ':
                return int(hMod[i])
        return None
    except Exception:
        return None
    finally:
        try:
            k32.CloseHandle(h)
        except Exception:
            pass


u32.GetAsyncKeyState.argtypes = [C.c_int]
u32.GetAsyncKeyState.restype = C.c_short
u32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
u32.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int,
                             C.c_int, C.c_int, C.c_uint]
u32.IsWindow.argtypes = [W.HWND]; u32.IsWindow.restype = C.c_bool
u32.IsIconic.argtypes = [W.HWND]; u32.IsIconic.restype = C.c_bool
u32.IsWindowVisible.argtypes = [W.HWND]; u32.IsWindowVisible.restype = C.c_bool
u32.GetForegroundWindow.argtypes = []; u32.GetForegroundWindow.restype = W.HWND
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(C.c_ulong)]
u32.GetWindowThreadProcessId.restype = C.c_ulong
u32.GetParent.argtypes = [W.HWND]
u32.GetParent.restype = W.HWND


def game_focus_state(target_hwnd):
    """判断游戏窗口当前是否应显示覆盖层（把覆盖层「绑定」到游戏窗体，
    避免它外溢到桌面或其它应用程序之上）。

    返回：
      'closed'     窗口已销毁（游戏退出或崩溃）
      'minimized'  已最小化（任务栏收起，不显示）
      'background' 非前台（被其它窗口遮挡、Alt+Tab 切走、开始菜单等）
      'ok'         前台且可见（应显示并跟随游戏窗体）
    """
    if not u32.IsWindow(target_hwnd):
        return 'closed'
    if u32.IsIconic(target_hwnd):
        return 'minimized'
    fg = u32.GetForegroundWindow()
    if not fg:
        return 'background'
    if fg == target_hwnd:
        return 'ok'
    # 前台是同一游戏进程内的其它窗口（如设置/弹窗）也视为在看游戏
    gp = C.c_ulong(); fp = C.c_ulong()
    u32.GetWindowThreadProcessId(target_hwnd, C.pointer(gp))
    u32.GetWindowThreadProcessId(fg, C.pointer(fp))
    return 'ok' if gp.value == fp.value else 'background'


def is_fullscreen(rect):
    """客户区是否基本铺满整个屏幕（全屏 / 无边框窗口化）。
    游戏全屏时 OpenGL 直接铺满客户区，挂在其下的子窗口会被游戏画面压住，
    必须改成桌面顶层窗口才看得见。"""
    try:
        sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    except Exception:
        return False
    try:
        _, _, w, h = rect
    except Exception:
        return False
    return bool(sw and sh) and w >= sw * 0.98 and h >= sh * 0.98


def client_rect(hwnd):
    """客户区在屏幕上的 (x, y, w, h)；比 GetWindowRect 少掉标题栏/边框，
    面板比例才能和实机画面里的对白框对齐。"""
    r = W.RECT()
    if not u32.GetClientRect(hwnd, C.byref(r)):
        return None
    pt = W.POINT(0, 0)
    u32.ClientToScreen(hwnd, C.byref(pt))
    return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)


_LOG = None          # 后台守护（pythonw 无控制台）时的日志句柄


def _ensure_log():
    """无控制台时把 say 的输出落到 overlay/live.log（超过 2MB 轮转一份 .1）。"""
    global _LOG
    if _LOG is not None or sys.stdout is not None:
        return
    try:
        d = os.path.join(WORK, 'overlay')
        if not os.path.isdir(d):
            os.makedirs(d)
        p = os.path.join(d, 'live.log')
        if os.path.exists(p) and os.path.getsize(p) > 2 * 1024 * 1024:
            try:
                if os.path.exists(p + '.1'):
                    os.remove(p + '.1')
                os.replace(p, p + '.1')
            except OSError:
                pass
        _LOG = io.open(p, 'a', encoding='utf-8')
    except Exception:
        _LOG = None


def say(s):
    if _LOG is not None:
        try:
            _LOG.write(s + '\n')
            _LOG.flush()
            return
        except Exception:
            pass
    try:
        print(s, flush=True)
    except Exception:
        pass


def load_index():
    d = json.load(io.open(IDX, encoding='utf-8'))
    say('译文索引：%d 指纹 / %d 条绑定 / %d 冲突' %
        (d['meta']['fingerprints'], d['meta']['matched_translations'],
         d['meta']['ambiguous_fingerprints']))
    return d['index']


def load_cfg():
    return json.load(io.open(CFG, encoding='utf-8'))


def load_names():
    """汉字角色名表：角色 ID -> 显示名。缺表时返回空字典（名字条仍会画空条）。"""
    if not os.path.isfile(NAMES):
        say('[!] 未找到汉字角色名表 %s' % NAMES)
        return {}
    return json.load(io.open(NAMES, encoding='utf-8'))['names']


def speaker_id(codes):
    """从当前台词码位串里取说话人 ID：0x8096 <id> <param>。
    实测 id 取值 1..11，且 TalkEvent0N 正好用 1 与 N，对应 CharacterName 表条目。
    该控制码只在说话人切换时插入，调用方需沿用上一次结果。"""
    for i, v in enumerate(codes or []):
        if v == SPEAKER_TAG and i + 1 < len(codes):
            sid = codes[i + 1]
            if 1 <= sid <= 21:
                return sid
    return None


def speaker_name(names, sid):
    rec = names.get(str(sid)) if sid else None
    return rec['zh'] if isinstance(rec, dict) else (rec or '')


# 名牌上「汉字全名 + 英文 First Name」里的 First Name（2026-11-06 23:22 用户要求）：
# 原生牌子是「First Last」，中文名是「姓 名」，附上首名玩家才对得上号。
# 但 The End / Researcher A / Core Materia / Young Sho 这类首词是冠词或称谓的，
# 附上去会变成「终结者 The」「研究员 Researcher」，纯属添乱 —— 一律不附。
_NON_NAME_HEADS = ('the', 'researcher', 'young', 'core', 'a', 'an', 'mr', 'ms',
                   'dr', 'miss', 'sir')


def first_name_of(names, sid):
    """取首名（given name）的英文；显式 first 字段优先（见 charname_zh.json）。

    不能无脑取 en 的第一个词：姬野晓 的官方英文是 `Himeno Akatsuki`（日式顺序，
    姓在前），首词 Himeno 是姓，首名是 Akatsuki。表里因此手工填了 `first`。
    """
    rec = names.get(str(sid)) if sid else None
    if not isinstance(rec, dict):
        return ''
    if (rec.get('first') or '').strip():
        return rec['first'].strip()
    parts = (rec.get('en') or '').strip().split()
    if len(parts) < 2 or parts[0].lower().strip('.') in _NON_NAME_HEADS:
        return ''
    return parts[0]


def nameplate_text(names, sid):
    """名字条上的显示文本：汉字全名 + 空格 + 英文首名（如「马可·巴巴托 Marco」）。"""
    zh = speaker_name(names, sid)
    fn = first_name_of(names, sid)
    return ('%s %s' % (zh, fn)).strip() if fn else zh


# 量词集合（真数字后接的标记），刻意不含 行/本/列 等动词词干
# （「46本当に」「12行きましょ」里的 46/12 是引擎代码，本/行 是动词词干）。
_JP_UNIT = set("人年月日時分秒回度％円ドル点件頁ペ倍割歳才番着軒棟台艘機基柱挺連組膳椀服個冊枚杯皿足階週匹頭羽")
_CN_UNIT = set("年月日时天人个次回天名岁度号条张只位节章页倍百分之百千万亿第间棵朵把辆瓶碗双对群套件颗粒")
_UNITS = _JP_UNIT | _CN_UNIT

# 句读符号：其后出现的孤立数字才视为引擎代码候选。
# 注意：绝不能包含 小数点(.) / 冒号(:) / 空格 / 斜杠(/) / 括号 / 半角逗号(,) 等——
# 这些后面的数字是真实数值/时间/编号（如 2014.04.04、08:12、File 1／2、1,000），
# 误剥会变乱码。半角 !? 保留以覆盖英文台词结尾后的代码数字。
_SEP = "…。，、！？!?"


def _is_text_start(ch):
    """后接字符是否为『正文首字』（汉字/假名）。

    引擎代码后接的是正文第一个字（如 `02虽` 的「虽」、`3C哪` 的「哪」、
    `M相` 的「相」、`…1没` 的「没」）；而真实数字/拉丁缩写后接的是量词
    （13天）、标点、更长数字或拉丁字母（HP/STR/M1129F）。只认 CJK 汉字与
    日文假名可精确区分：避免误剥 `HP提升`/`STR提升`/`G力` 这类拉丁缩写。"""
    if not ch:
        return False
    return ('\u4e00' <= ch <= '\u9fff') or ('\u3040' <= ch <= '\u30ff')


# 真实专有名词前缀（单字母 + 中文）：是缩写而非引擎代码，禁止剥离。
_PREFIX_KEEP = ("G力",)
# 数字分隔符：其后出现的数字是真实数值/编号（1,000 / 2014.04 / 08:12 / File 1／2）
# 的一部分，禁止当作代码剥离。
_NUM_SEP = ",.:/／"


def normalize_zh(zh):
    """剥离原版引擎遗留的「台词/语音/表情代码」（孤立的数字 / 数字+字母 / 单字母）。

    原脚本数据里约 7% 的台词含这类标记（日文 `02ちょっと…`、`3C哪能…`、
    单字母 `M相当好战`、夹在省略号后的 `…1没想到…` 里的 1），引擎把它们当指令码
    消费掉、**不在屏幕上显示**；但译文把原始串原样翻成中文，于是变成可见杂质。

    判定核心（只认中文/假名首字，避免误伤 `HP提升`/`G力` 等拉丁缩写）：
      · A 类 「数字+字母」（3C/7B/6A/5D…）—— 指令码，开头即剥；
      · B 类 「单字母」（M/C/D/I/E/J/P/G…）—— 指令码，后接中文即剥；
          例外：已知专有名词前缀（G力）保留；
      · C 类 「纯数字」（02/ 3/ 4/ 8…）—— 后接中文/标点/符号/空白即剥；
          保留：后接量词（13天/15人/10年/3回合/12颗）、更长数字、
          小数点/冒号/斜杠后的编号（2014.04.04、08:12、1,000、File 1／2）、
          N世纪/N年间、以及数字前是普通汉字（第1章/约5人）；
      · 句读符号（…。，、！？）之后出现的孤立数字代码（…1 / 。2）也一并剥离。
    代码前缀允许前导空白（如 ` 3这些家伙`），循环剥离直到稳定。"""
    if not zh:
        return zh
    s = zh
    # 反复剥离开头的引擎代码前缀（前导空白可叠加）
    while True:
        s = s.lstrip()
        if not s:
            break
        # A 类：数字 + 字母 + 后接中文首字（如 3C哪 / 7B那 / 6A我）—— 指令码
        # 必须后接中文，避免误剥 88Q.E.D. 这类「数字+拉丁缩写」。
        m = re.match(r'^(\d{1,2}[A-Za-z])(?=[\u4e00-\u9fff])', s)
        if m:
            s = s[m.end():]
            continue
        # C 类：纯数字
        m = re.match(r'^(\d{1,2})', s)
        if m:
            a = s[m.end():m.end() + 1]
            if a and (a.isdigit() or a in _UNITS or a == '世' or a in _NUM_SEP):
                break                       # 真数字（含 N世纪 / 千分位 / 时间 / 编号）→ 保留
            s = s[m.end():]                  # 代码：后接中文/标点/符号/空白 → 剥离
            continue
        # B 类：单字母（M/C/D/I/E/J/P/G…）
        m = re.match(r'^([A-Za-z])', s)
        if m:
            if s[:2] in _PREFIX_KEEP:
                break                       # 专有名词（G力）→ 保留
            a = s[1:2]
            if a and _is_text_start(a):
                s = s[1:]                   # 代码：后接中文首字 → 剥离
                continue
            break
        break
    # 句读符号之后的孤立数字代码（…1 / 。2 之类）
    def _repl(mm):
        a = s[mm.end():mm.end() + 1]
        return '' if (a not in _UNITS and _is_text_start(a)) else mm.group(0)
    s = re.sub(r'(?<=[' + _SEP + r'])\d{1,2}(?=[^\d])', _repl, s)
    return s


# ---------------------------------------------------------------- 内存只读
def read_mem(h, va, n):
    if not va or n <= 0:
        return None
    buf = C.create_string_buffer(n)
    got = C.c_size_t(0)
    if not k32.ReadProcessMemory(h, C.c_void_p(va), buf, n, C.byref(got)):
        return None
    return buf.raw[:got.value] if got.value else None


def u32at(b, off=0):
    return struct.unpack_from('<I', b, off)[0] if b and len(b) >= off + 4 else None


def codes_at(h, va, maxn):
    """从 va 读一条 0x0000 结尾的 u16 码位串。"""
    raw = read_mem(h, va, maxn * 2 + 2)
    if not raw or len(raw) < 8:
        return []
    n = len(raw) // 2
    vals = struct.unpack('<%dH' % n, raw[:n * 2])
    out = []
    for v in vals:
        if v == 0:
            return out
        if len(out) >= maxn:
            return []
        out.append(v)
    return []


def good_code(c):
    """引擎码位合法区间。2026-11-06 起原生台词还原为英文原文，
    原版串含 1..12 的低位控制符（实测 4431 处）与 0x8000..0x8097 控制码
    （6957 处，其中 1825 条串以控制码开头），故放宽到全部合法码位。
    仍以「命中译文索引」为唯一判据，放宽不会造成误译。"""
    if 1 <= c <= 2400:
        return True
    if 0x8000 <= c <= 0x80FF:
        return True
    return False


def good_first(c):
    return (1 <= c <= 2400) or (0x8000 <= c <= 0x80FF)


class Locator(object):
    """从消息对象定位当前台词码位串；地址链来自纯离线反汇编（尚未实机验证），
    因此这里做多层兜底：内联串 / 指针跟随 / 对象内扫描 / 对象内指针跟随。"""

    def __init__(self, h, index, cfg, base=None):
        self.h, self.index, self.m = h, index, cfg['memory']
        self.base = base
        if base is not None:
            # 实机基址 (ASLR) 与离线 dump 基址不一致，按 (实机 - DUMP_BASE) 重定位
            self.tm_va = base + (self.m['table_manager_va'] - DUMP_BASE)
            self.mo_va = base + (self.m['message_object_va'] - DUMP_BASE)
        else:                       # 离线自测：直接使用 dump 相对 VA
            self.tm_va = self.m['table_manager_va']
            self.mo_va = self.m['message_object_va']
        self.last_diag = {}
        self.last_codes = []          # 命中时的完整码位串，供说话人 ID 解析

    def lookup(self, codes):
        if not codes:
            return None
        keys = (p.fp(codes),
                p.fp([c for c in codes if 1 <= c < 0x8000]))
        for key in keys:
            rec = self.index.get(key)
            if rec:
                self.last_codes = list(codes)
                return normalize_zh(rec[0]['zh']), rec[0]['ref'], len(codes)
        return None

    def chain(self):
        m = self.m
        P = u32at(read_mem(self.h, self.tm_va, 4))
        idx = u32at(read_mem(self.h, P + m['idx_off'], 4)) if P else None
        T = (u32at(read_mem(self.h, P + m['table_array_off'] +
                            m['table_stride'] * (idx or 0), 4))
             if P and idx is not None else None)
        M = u32at(read_mem(self.h, self.mo_va, 4))
        magic = None
        if T:
            head = read_mem(self.h, T, 4)
            magic = head == b'TBB1'
        self.last_diag = dict(P=P, idx=idx, T=T, M=M, tbb1=magic)
        return P, idx, T, M

    def scan_inline(self, blob):
        """在一块内存里找 0x0000 结尾的码位串并查译文；也可用于离线自测。"""
        n = len(blob) // 2
        if n < 4:
            return None
        vals = struct.unpack('<%dH' % n, blob[:n * 2])
        maxn = self.m['max_codes']
        for i in range(n - 3):
            if not good_first(vals[i]):
                continue
            cs, j = [], i
            while j < n and vals[j] != 0 and len(cs) < maxn and good_code(vals[j]):
                cs.append(vals[j])
                j += 1
            if j < n and vals[j] == 0 and len(cs) >= 3:
                hit = self.lookup(cs)
                if hit:
                    return hit[0], hit[1], 'inline@+0x%X' % (i * 2)
        return None

    def probe(self):
        P, idx, T, M = self.chain()
        if not M:
            return None
        off, maxn, span = (self.m['text_buffer_off'],
                           self.m['max_codes'], self.m['scan_span'])

        # 1) +0x98 内联码位串
        hit = self.lookup(codes_at(self.h, M + off, maxn))
        if hit:
            return hit[0], hit[1], 'inline+0x98', self.last_diag

        # 2) +0x98 是指针，跟随读取
        tp = u32at(read_mem(self.h, M + off, 4))
        if tp and 0x10000 < tp < 0x80000000:
            hit = self.lookup(codes_at(self.h, tp, maxn))
            if hit:
                return hit[0], hit[1], 'ptr+0x98', self.last_diag

        blob = read_mem(self.h, M, span)
        if not blob:
            return None

        # 3) 消息对象内部内联串扫描（2 字节对齐）
        hit = self.scan_inline(blob)
        if hit:
            return hit[0], hit[1], hit[2], self.last_diag

        # 4) 对象内指针跟随（限量，避免跟随到无关堆）
        seen, tried = set(), 0
        for o in range(0, min(len(blob) - 4, 0x400), 4):
            v = u32at(blob, o)
            if not v or v in seen or not (0x10000 < v < 0x80000000) or v & 3:
                continue
            seen.add(v)
            tried += 1
            if tried > 64:
                break
            hit = self.lookup(codes_at(self.h, v, maxn))
            if hit:
                return hit[0], hit[1], 'ptr@+0x%X' % o, self.last_diag
        return None


# ---------------------------------------------------------------- 分层窗口
class OverlayWindow(object):
    _cls = 'LDZHDialogOverlayWnd'
    _proc = None

    def __init__(self, x, y, w, h, parent=None):
        if OverlayWindow._proc is None:
            @p.WNDPROC
            def _proc(hwnd, msg, wp, lp):
                return u32.DefWindowProcW(hwnd, msg, wp, lp)
            OverlayWindow._proc = _proc
        wc = p.WNDCLASSEXW()
        wc.cbSize = C.sizeof(p.WNDCLASSEXW)
        wc.lpfnWndProc = OverlayWindow._proc
        wc.hInstance = k32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls
        if not u32.RegisterClassExW(C.byref(wc)) and C.get_last_error() != 1410:
            raise RuntimeError('RegisterClassExW %d' % C.get_last_error())
        ex = 0x00080000   # WS_EX_LAYERED：仅用于启用 color-key 透明（挖空色键色）
        if parent:
            style = 0x40000000            # WS_CHILD：作为游戏窗口子窗口，随其移动/全屏/缩放
        else:
            style = 0x80000000            # WS_POPUP
            ex |= 0x00000008 | 0x00000020 | 0x08000000 | 0x00000080
            # WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.hwnd = u32.CreateWindowExW(ex, self._cls, 'LD ZH Dialogue',
                                        style, x, y, w, h,
                                        parent, None, None, None)
        if not self.hwnd and parent:
            # 子窗口创建失败（极少见），回落到桌面顶层窗口
            ex = 0x00080000 | 0x00000008 | 0x00000020 | 0x08000000 | 0x00000080
            self.hwnd = u32.CreateWindowExW(ex, self._cls, 'LD ZH Dialogue',
                                            0x80000000, x, y, w, h,
                                            None, None, None, None)
        if not self.hwnd:
            raise RuntimeError('CreateWindowExW %d' % C.get_last_error())
        # 仅当真正成功成为子窗口时才记录 parent（c_void_p 不能直接 int()/==比较，
        # 须取 .value；曾因比较失败误判为 None，覆盖层退回桌面坐标定位而全屏失效）
        def _hv(v):
            v = getattr(v, 'value', v)
            return int(v or 0)
        _gp = u32.GetParent(self.hwnd)
        self.parent = parent if (parent and _hv(_gp) == _hv(parent)) else None
        u32.ShowWindow(self.hwnd, 5)
        self.x, self.y, self.w, self.h = x, y, w, h
        self._dib(w, h)

    def _dib(self, w, h):
        if getattr(self, '_hdc', None):
            g32.DeleteObject(self._hbmp)
            g32.DeleteDC(self._hdc)
        hdc = g32.CreateCompatibleDC(None)
        bmi = p.BITMAPINFO()
        bmi.bmiHeader.biSize = C.sizeof(p.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = C.c_void_p()
        hbmp = g32.CreateDIBSection(hdc, C.byref(bmi), 0, C.byref(bits), None, 0)
        if not hbmp or not bits:
            raise RuntimeError('CreateDIBSection %d' % C.get_last_error())
        self._hdc, self._hbmp, self._bits = hdc, hbmp, bits
        g32.SelectObject(hdc, hbmp)

    def render(self, img, x=None, y=None):
        w, h = img.size
        size_changed = (w, h) != (self.w, self.h)
        pos_changed = x is not None and (x, y) != (self.x, self.y)
        if x is not None:
            self.x, self.y = x, y
        if size_changed:
            self.w, self.h = w, h
            self._dib(w, h)
        # 父窗口为游戏时，把屏幕坐标换算成「相对游戏客户区」坐标：
        # 子窗口坐标相对父客户区，游戏移动/全屏/缩放时子窗口自动跟随，无需重算。
        if self.parent:
            o = W.POINT(0, 0)
            u32.ClientToScreen(self.parent, C.byref(o))
            dx, dy = self.x - o.x, self.y - o.y
        else:
            dx, dy = self.x, self.y
        if size_changed or pos_changed:
            u32.SetWindowPos(self.hwnd, None, dx, dy, w, h, 0x0010 | 0x0004)
        r, g, b, a = img.split()
        bgra = Image.merge('RGBA', (b, g, r, a)).tobytes()
        C.memmove(self._bits, bgra, len(bgra))
        pt, sz, src = W.POINT(dx, dy), p.SIZE(self.w, self.h), W.POINT(0, 0)
        blend = p.BLENDFUNCTION(0, 0, 255, 1)      # AC_SRC_OVER / AC_SRC_ALPHA
        if not u32.UpdateLayeredWindow(self.hwnd, None, C.byref(pt), C.byref(sz),
                                       self._hdc, C.byref(src), 0,
                                       C.byref(blend), 2):
            raise RuntimeError('UpdateLayeredWindow %d' % C.get_last_error())

    def hide(self):
        u32.ShowWindow(self.hwnd, 0)

    def show(self):
        u32.ShowWindow(self.hwnd, 5)
        self.to_front()

    def to_front(self):
        """抢占最前端：子窗口放到同层最上；顶层窗口置为 TOPMOST。
        游戏获得焦点时调用，保证浮层永远压在游戏画面之上、不被其它窗口盖住。"""
        try:
            flags = 0x0001 | 0x0002 | 0x0010      # NOSIZE | NOMOVE | NOACTIVATE
            if self.parent:
                u32.SetWindowPos(self.hwnd, W.HWND(0), 0, 0, 0, 0, flags)      # HWND_TOP
            else:
                u32.SetWindowPos(self.hwnd, W.HWND(-1), 0, 0, 0, 0, flags)     # HWND_TOPMOST
        except Exception:
            pass

    def pump(self):
        msg = p.MSG()
        for _ in range(8):
            if not u32.PeekMessageW(C.byref(msg), None, 0, 0, 1):
                break
            u32.TranslateMessage(C.byref(msg))
            u32.DispatchMessageW(C.byref(msg))

    def close(self):
        try:
            u32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        if getattr(self, '_hdc', None):
            g32.DeleteObject(self._hbmp)
            g32.DeleteDC(self._hdc)


# ---------------------------------------------------------------- 面板绘制
def _font_candidates(cfg):
    f = cfg['font']
    paths = []
    p = f.get('path')
    if p:
        paths.append(p)
    fb = f.get('fallback')
    if isinstance(fb, (list, tuple)):
        paths.extend(fb)
    elif fb:
        paths.append(fb)
    # 解析相对路径（相对 work 目录），并去重
    out = []
    for p in paths:
        if not p:
            continue
        if not os.path.isabs(p):
            p = os.path.join(WORK, p)
        if p not in out:
            out.append(p)
    return out


def load_font(cfg, size):
    for path in _font_candidates(cfg):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def pick_font(cfg, game_h):
    f = cfg['font']
    size = max(f['min_size'], min(f['max_size'], int(game_h * f['size_ratio'])))
    return load_font(cfg, size), size


def clean_text(text):
    """去掉译文里的填充空格/控制符残留，保留正常字间距。"""
    s = str(text).replace('\\n', '\n').strip()
    s = re.sub(r'[ \u3000]{2,}', ' ', s)
    return s


def wrap_text(draw, text, font, maxw):
    lines = []
    for para in clean_text(text).split('\n'):
        cur = ''
        for ch in para:
            t = cur + ch
            if cur and draw.textlength(t, font=font) > maxw:
                lines.append(cur)
                cur = ch
            else:
                cur = t
        lines.append(cur)
    return lines


def make_panel(text, w, h, cfg, game_h, opaque=True, cover=True):
    """cover=True：完全不透明、不描第二道边（让原生蓝框露出），精确盖住对白框。"""
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = cfg['colors']
    rgb = tuple(c['bg'][:3])
    if cover and opaque:
        bg, outline = rgb + (255,), rgb + (255,)
    elif opaque:
        bg, outline = tuple(c['bg']), tuple(c['border'])
    else:
        bg, outline = rgb + (190,), tuple(c['border'])
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=14, fill=bg,
                        outline=outline, width=2)
    font, size = pick_font(cfg, game_h)
    pad = max(18, int(w * 0.024))
    lines = wrap_text(d, text, font, w - 2 * pad)
    lh = int(size * cfg['font']['line_gap'])
    floor = cfg['font']['min_size']
    # 长台词自适应缩小字号，避免末尾被面板裁掉
    while len(lines) * lh > h - 12 and size > floor:
        size = max(floor, size - 2)
        font = load_font(cfg, size)
        lh = int(size * cfg['font']['line_gap'])
        lines = wrap_text(d, text, font, w - 2 * pad)
    y = max(10, (h - len(lines) * lh) // 2)
    for line in lines[:max(1, int(h / lh))]:
        d.text((pad + 1, y + 1), line, font=font, fill=tuple(c['shadow']))
        d.text((pad, y), line, font=font, fill=tuple(c['fg']))
        y += lh
    return img


# ---------------------------------------------------------------- 画面文本（选单/说明/注释）
# 剧情台词走内存读串（最准）；选单、道具说明、教程提示这些走内存读不到，
# 改成「截屏 -> 逐字识别游戏位图字体 -> 查 screen_text_index.json -> 原位盖中文」。
# 识别引擎在 screen_ocr.py，这里只负责调度与绘制。
# 用户 23:13 定调：菜单/说明的追踪识别放弃，默认关闭（不再每 2 秒抓全屏）。
# 代码保留，需要时 Ctrl+F9 可临时开回来做实验，不影响剧情浮层。
SCR_ON = [False]                # 画面文本覆盖开关（Ctrl+F9 切换）
_SCR = {'runs': [], 'size': None, 'stamp': 0.0, 'sig': None, 'n': 0}
_SCR_FONT = {}


def scr_font(cfg, size):
    size = max(11, min(40, int(size)))
    f = _SCR_FONT.get(size)
    if f is None:
        f = load_font(cfg, size)
        _SCR_FONT[size] = f
    return f


def sample_bg(src, box, pad=5):
    """取文本框外一圈像素的中位色当盖板底色，让盖板和原界面融在一起。"""
    if src is None:
        return (10, 14, 22)
    W, H = src.size
    x0, y0, x1, y1 = [int(v) for v in box]
    pts = []
    for y in range(max(0, y0 - 3), min(H, y1 + 4), 2):
        for x in (max(0, x0 - pad), min(W - 1, x1 + pad)):
            pts.append(src.getpixel((x, y)))
    if not pts:
        return (10, 14, 22)
    pts.sort(key=lambda c: c[0] + c[1] + c[2])
    return pts[len(pts) // 2]


def make_screen_image(size, runs, cfg, src=None):
    """把识别到的每段英文原位盖成中文（多行段落按整段重排）。"""
    W, H = size
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for r in runs:
        x0, y0, x1, y1 = [int(v) for v in r['box']]
        zh = r['zh']
        if not zh:
            continue
        nl = max(1, int(r.get('lines', 1)))
        bh = max(12, y1 - y0 + 1)
        px0, py0 = max(0, x0 - 5), max(0, y0 - 3)
        # 面板宽度：先按原英文宽度，中文放不下往右扩，但不越过画面右边
        pw_max = W - px0 - 3
        pw = min(pw_max, max(x1 - x0 + 10, 60))
        fsz = max(11, int(bh / nl * 0.95))
        font = scr_font(cfg, fsz)
        lines = wrap_text(d, zh, font, pw - 12)
        lh = int(fsz * 1.16)
        # 放不下：先缩字号，再扩宽
        while len(lines) * lh > bh + 4 and fsz > 11:
            fsz -= 1
            font = scr_font(cfg, fsz)
            lines = wrap_text(d, zh, font, pw - 12)
            lh = int(fsz * 1.16)
        while len(lines) > nl and pw < pw_max:
            pw = min(pw_max, pw + 60)
            lines = wrap_text(d, zh, font, pw - 12)
        ph = max(bh + 7, len(lines) * lh + 6)
        bg = sample_bg(src, (x0, y0, x1, y1))
        d.rounded_rectangle([px0, py0, px0 + pw, py0 + ph],
                            radius=max(3, int(ph * 0.16)),
                            fill=(bg[0], bg[1], bg[2], 255))
        ty = py0 + max(0, (ph - len(lines) * lh) / 2.0)
        for ln in lines:
            d.text((px0 + 6, ty), ln, font=font, fill=(238, 244, 255, 255))
            ty += lh
    return img


class TextWatcher(threading.Thread):
    """后台线程：定时抓帧 -> 识别画面英文 -> 查中文，主循环只负责贴图。

    关键细节：抓帧会把**我们自己画的盖板**也抓进去（分层窗口参与合成），
    下一轮就会把中文当成英文再认一遍，导致盖板时有时无地闪烁。
    所以每次抓帧前先把盖板窗口藏起来、抓完立刻让主循环重画——损失约 60ms，
    换到的是「识别看的始终是干净的原画面」。
    """

    def __init__(self, rect_fn, cfg, hide_cb=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.rect_fn = rect_fn
        self.cfg = cfg
        self.hide_cb = hide_cb
        self.bot = None
        self.stop = False
        self.err = 0
        self.cadence = float(cfg.get('screen_text_seconds', 1.6))
        self.lock = threading.Lock()
        self.pause = False          # 对白场景时暂停（那块由内存读串负责）

    @staticmethod
    def _pick(runs):
        """只覆盖「说明/注释」这类成段文字。

        用户 22:52 定调：列表里的短名字（装备/出击/道具名）不碰，免得盖板
        压到旁边的数量列、图标，也免得半中半英更难看。判据：
          - 打过分的整段（换行合并后行数≥2）——说明文字几乎都是多行；
          - 或单行但字符数≥18、译文≥8 字——单行长注释也放过；
          - 且匹配距离足够小（默认 0.17），宁可不盖也不盖错。
        """
        out = []
        for r in runs:
            if not r['zh'] or r['dist'] > 0.17:
                continue
            n = len(r['raw'].replace(' ', ''))
            nl = int(r.get('lines', 1))
            if nl >= 2 or (n >= 18 and len(r['zh']) >= 8):
                out.append(r)
        return out

    def run(self):
        while not self.stop:
            t0 = time.time()
            try:
                if SCR_ON[0] and not self.pause:
                    if self.bot is None:
                        import screen_ocr
                        self.bot = screen_ocr.ScreenOCR()
                        say('画面识别引擎就绪（选单/说明/注释原位覆盖中文）')
                    rect = self.rect_fn()
                    if rect:
                        if self.hide_cb:
                            try:
                                self.hide_cb()
                            except Exception:
                                pass
                            time.sleep(0.05)
                        img = grab_client_raw(rect)
                        if img is not None:
                            runs = self.bot.scan(img)
                            keep = self._pick(runs)
                            with self.lock:
                                old = _SCR['runs']
                                _SCR['runs'] = keep
                                _SCR['size'] = img.size
                                _SCR['src'] = img
                                _SCR['stamp'] = time.time()
                                if [r['box'] for r in keep] != [r['box'] for r in old]:
                                    _SCR['sig'] = None      # 有变化 -> 主循环重画并重新显示
                                elif not keep:
                                    _SCR['sig'] = None
            except Exception as e:
                self.err += 1
                if self.err in (1, 5, 50):
                    say('[!] 画面识别异常（第 %d 次）：%r' % (self.err, e))
            time.sleep(max(0.15, self.cadence - (time.time() - t0)))


def np_sane(box, rect, cfg):
    """名字条几何兜底：算出来的矩形必须真的看得见。

    以前名字条只在「文本/说话人变化时」画一次，一旦这一帧的几何被识别结果
    带歪（宽高退化成几十像素、或 y 顶到客户区外），窗口照样 IsWindowVisible=True，
    日志里写「名牌=显示」，画面上却什么都没有 —— 而且不会自己回来。
    这里把不合理/出界的矩形夹回客户区，实在太小就退回固定比例。
    """
    cx, cy, cw, ch = int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
    npcfg = cfg['nameplate']
    try:
        x, y, w, h = [int(v) for v in box]
    except Exception:
        x = y = w = h = 0
    # 彻底不可用 → 直接用固定比例重算（不是夹取：夹取会把名字条贴在屏幕最上边，
    # 看起来像"框跑歪了"，反而不如按比例给一个正常位置）
    if (w < 60 or h < 16 or w > cw * 0.9 or h > ch * 0.2
            or w / float(max(1, h)) > 14.0
            or y + h <= cy + 4 or y >= cy + ch - 4
            or y < cy - h * 0.35
            or x + w <= cx + 4 or x >= cx + cw - 4):
        x = cx + int(cw * npcfg['x_ratio'])
        y = cy + int(ch * npcfg['y_ratio'])
        w = int(cw * npcfg['w_ratio'])
        h = int(ch * npcfg['h_ratio'])
    x = max(cx, min(x, cx + cw - w))
    y = max(cy, min(y, cy + ch - h))
    return x, y, w, h


def make_nameplate(name, w, h, cfg, game_h):
    """蓝色名字条 + 汉字名，用来盖住原生英文名字牌。"""
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for x in range(w):
        t = x / max(1, w - 1)
        d.line((x, 0, x, h), fill=(int(8 + 20 * t), int(60 + 90 * t),
                                   int(150 + 80 * t), 255))
    cut = max(14, int(h * 0.55))          # 右端斜切，和原生名字牌形状呼应
    d.polygon([(w - cut, 0), (w, 0), (w, h), (w - cut * 2, h)],
              fill=(0, 0, 0, 0))
    d.line((2, 1, w - cut, 1), fill=(140, 228, 255, 255), width=2)
    d.line((2, h - 2, w - cut * 2, h - 2), fill=(60, 150, 230, 255), width=2)
    if name:
        nf = cfg['nameplate']
        size = max(nf['min_size'],
                   min(nf['max_size'], int(game_h * nf['font_size_ratio'])))
        font = load_font(cfg, size)
        pad = max(12, int(w * 0.05))
        while (d.textlength(name, font=font) > w - pad * 2 - cut
               and size > nf['min_size']):
            size -= 2
            font = load_font(cfg, size)
        tw = d.textlength(name, font=font)
        tx = max(pad, (w - cut - tw) / 2)
        ty = max(1, (h - size * 1.25) / 2)
        d.text((tx + 1, ty + 1), name, font=font, fill=(0, 0, 0, 160))
        d.text((tx, ty), name, font=font, fill=(248, 252, 255, 255))
    return img


# ---------------------------------------------------------------- 窗口查找
def pid_of_window(hwnd):
    try:
        pid = W.DWORD()
        u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return pid.value or None
    except Exception:
        return None


def game_pids():
    """枚举进程名为 LostDimension.exe 的 pid 集合（不依赖 tasklist 的中文输出）。"""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD),
                    ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', C.c_void_p),
                    ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
                    ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', C.c_long),
                    ('dwFlags', W.DWORD), ('szExeFile', C.c_wchar * 260)]

    out = set()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == C.c_void_p(-1).value:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = C.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, C.byref(e))
        while ok:
            if e.szExeFile.lower() == 'lostdimension.exe':
                out.add(e.th32ProcessID)
            ok = k32.Process32NextW(snap, C.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def find_game(pid_hint=None, verbose=False):
    """找游戏主窗口。

    类名 `LostDimension` 是窗口模式下的名字；**切全屏/无边框时游戏会重建窗口，
    新窗口的类名不一定还是它**，所以再加两条回退：同进程顶层窗口、标题含
    "Lost Dimension"。否则切换模式后会被误判成"游戏已退出"。
    """
    found = []
    pids = game_pids()
    if pid_hint:
        pids = pids | {pid_hint}

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(hwnd, _):
        pid = W.DWORD()
        u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        cls = C.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        ttl = C.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, ttl, 256)
        r = W.RECT()
        u32.GetWindowRect(hwnd, C.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if cls.value == 'LostDimension':
            hit = True
        elif pid.value in pids:
            # 同进程：排除托盘/幽灵小窗与 IME 之类，只认像"主画面"的窗口
            hit = (w > 200 and h > 150) and not cls.value.startswith(('IME', 'MSCTF'))
        else:
            hit = 'Lost Dimension' in ttl.value
        if hit:
            found.append((pid.value, hwnd, r.left, r.top, w, h, cls.value, ttl.value))
        return True

    u32.EnumWindows(cb, 0)
    if verbose:
        for t in sorted(found, key=lambda x: -x[4] * x[5]):
            say('  候选 pid=%d hwnd=%s %dx%d @(%d,%d) class=%s title=%s'
                % (t[0], hex(t[1]), t[4], t[5], t[2], t[3], t[6], t[7]))
    if not found:
        return None
    best = max(found, key=lambda t: t[4] * t[5])
    return best[:6]


def attach():
    g = find_game()
    if not g:
        say('未找到运行中的 LostDimension 窗口；请先自行启动游戏（本程序不启动游戏）。')
        return None
    pid, hwnd, x, y, w, h = g
    # 基址枚举偶发失败（EnumProcessModules 拒绝访问/返回空）。以前一失败就回落到
    # dump 基址硬跑 —— 那等于整条地址链全错、画面永远空白，比不启动更隐蔽。
    # 现在重试几次，仍失败就报告并交给守护循环退避重试。
    base = None
    for _i in range(6):
        base = get_module_base(pid)
        if base is not None:
            break
        time.sleep(0.2)
    if base is None:
        say('[!] 模块基址枚举失败（已重试 6 次）。不猜测基址，稍后自动重试。')
        return None
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                             False, pid)
    if not handle:
        say('OpenProcess 失败 %d' % C.get_last_error())
        return None
    say('只读附着 pid=%d 窗口 %dx%d @(%d,%d) 模块基址=0x%X' % (pid, w, h, x, y, base))
    return handle, hwnd, (x, y, w, h), base


# ---------------------------------------------------------------- 三种模式
def offline_roundtrip(idx, cfg):
    """离线闭环：从当前实装 Data.bra 取一条真实剧情码位串，塞进模拟内存，
    验证「扫描内存 → 指纹查表 → 完整中文译文」整条链路（不需要游戏进程）。"""
    import zlib
    import ld_bra, tbb
    bra = os.path.join(ROOT, 'Data.bra')
    d = open(bra, 'rb').read()
    _, ents = ld_bra.bra_read_full(bra)
    target = 'Table/Event/Event100_City.mbb'
    e = next(x for x in ents if x['name'].replace('\\', '/') == target)
    raw = zlib.decompress(d[e['doff'] + 16: e['doff'] + e['total']], -15)
    en = tbb.endian(raw)
    rows = tbb.entries(raw, tbb.tables(raw)[0], en)
    codes = next(cs for i, off, cs in rows if i == 2 and cs and len(cs) > 6)
    packed = b''.join(struct.pack('<H', c) for c in codes) + b'\x00\x00'
    blob = bytearray(os.urandom(2048))
    blob[0x98:0x98 + len(packed)] = packed
    hit = Locator(None, idx, cfg).scan_inline(bytes(blob))
    return len(codes), max(codes), hit


def offline_speaker_check(cfg, names):
    """离线验证「台词串 0x8096 <id> → 汉字名」这条新链路。"""
    import zlib
    import ld_bra, tbb
    bra = os.path.join(ROOT, 'Data.bra')
    d = open(bra, 'rb').read()
    _, ents = ld_bra.bra_read_full(bra)
    target = 'Table/Talk/TalkEvent02.mbb'
    e = next(x for x in ents if x['name'].replace(chr(92), '/') == target)
    raw = zlib.decompress(d[e['doff'] + 16:e['doff'] + e['total']], -15)
    en = tbb.endian(raw)
    for i, off, cs in tbb.entries(raw, tbb.tables(raw)[0], en):
        sid = speaker_id(cs) if cs else None
        if sid:
            return sid, speaker_name(names, sid), len(cs)
    return None, '', 0


def detect_selftest(cfg):
    """对白框识别离线自测：实机截图应识别出 1 个框（≈校准基准
    (380,756) 1162x174）；把框条带复制一份模拟上下双开，应识别出 2 个框。
    纯文件操作，不启动游戏。"""
    src = os.path.join(os.path.expanduser('~'), '.workbuddy', 'clipboard-images',
                       'clipboard-2026-11-04T13-28-20-806Z-a9c6f597.jpg')
    if not os.path.isfile(src):
        say('识别自测：找不到实机对白截图，跳过')
        return True
    img = Image.open(src).convert('RGB')
    W, H = img.size
    boxes, acc = detect_boxes.find_dialog_boxes(img, cfg.get('detect', {}))
    ok = len(boxes) == 1
    if ok:
        x, y, w, h = boxes[0]
        ok = (abs(x - 380) <= 8 and abs(y - 756) <= 8
              and abs(w - 1162) <= 16 and abs(h - 174) <= 16)
    say('识别自测（单框）：%d 框 %s（饰条 %d 根）| %s'
        % (len(boxes), boxes, len(acc), 'OK' if ok else 'FAIL'))
    dual_ok = False
    if ok:
        x0, y0, w0, h0 = boxes[0]
        strip = img.crop((x0, y0, x0 + w0, y0 + h0))
        canvas = img.copy()
        canvas.paste(strip, (x0, int(H * 0.28)))
        b2, _ = detect_boxes.find_dialog_boxes(canvas, cfg.get('detect', {}))
        dual_ok = len(b2) == 2
        say('识别自测（双开模拟）：%d 框 %s | %s'
            % (len(b2), b2, 'OK' if dual_ok else 'FAIL'))
    return ok and dual_ok


def make_align_preview(text, cfg, speaker=''):
    """离线合成：把汉字名字条 + 译文面板按同一组比例叠到真实对白截图上，
    直观看对齐/遮挡效果。纯文件操作，不启动游戏、不截屏、不附着进程。"""
    src = os.path.join(os.path.expanduser('~'), '.workbuddy', 'clipboard-images',
                       'clipboard-2026-11-04T13-28-20-806Z-a9c6f597.jpg')
    if not os.path.isfile(src):
        say('对齐预览：找不到实机对白截图，跳过')
        return None
    base = Image.open(src).convert('RGBA')
    W, H = base.size                      # 实机全屏 1920x1080
    # 与 live() 相同的识别定位：直接在实机截图上找框
    boxes, _acc = detect_boxes.find_dialog_boxes(base, cfg.get('detect', {}))
    if not boxes:
        say('对齐预览：截图上未识别到对白框，跳过')
        return None
    bx, by, bw, bh = boxes[-1]            # 主框 = 最下面
    say('对齐预览：识别到 %d 个框，主框 (%d,%d) %dx%d' % (len(boxes), bx, by, bw, bh))
    nx, ny, nw, nh = detect_boxes.nameplate_from_box((bx, by, bw, bh),
                                                     cfg.get('detect', {}))
    plate = make_nameplate(speaker, nw, nh, cfg, H)
    base.alpha_composite(plate, (nx, ny))
    px, py, pw, ph = detect_boxes.panel_from_box((bx, by, bw, bh),
                                                 cfg.get('detect', {}))
    panel = make_panel(text, pw, ph, cfg, H, True, True)
    base.alpha_composite(panel, (px, py))
    out = os.path.join(OUT, 'overlay_align_preview.png')
    base.convert('RGB').save(out)
    say('对齐预览 %s | 名字条 (%d,%d) %dx%d「%s」/ 面板 (%d,%d) %dx%d / 画面 %dx%d'
        % (out, nx, ny, nw, nh, speaker, px, py, pw, ph, W, H))
    return out


def selftest():
    os.makedirs(OUT, exist_ok=True)
    idx, cfg = load_index(), load_cfg()
    # 下面两步要从游戏表里取真实码位串做闭环，必须正版 Data.bra；
    # 直接拿仓库源码跑时没有游戏文件，跳过而不是报错。
    has_bra = os.path.isfile(os.path.join(ROOT, 'Data.bra'))
    if has_bra:
        n, cmax, rt = offline_roundtrip(idx, cfg)
        say('离线链路自测：真实剧情码位串 %d 个（最大码 %d）→ %s'
            % (n, cmax, ('命中 %s | %s' % (rt[2], rt[0][:40])) if rt else '未命中'))
        if not rt:
            say('索引/扫描链路未闭环，终止'); return 1
    else:
        say('离线链路自测：跳过（缺 %s，需正版游戏目录）'
            % os.path.join(ROOT, 'Data.bra'))
    # 取一条真实、较长的剧情译文
    sample = None
    for v in idx.values():
        r = v[0]
        z = r.get('zh', '')
        if not (18 <= len(z) <= 120) or '<' in z:
            continue
        if not any('\u4e00' <= ch <= '\u9fff' for ch in z):
            continue
        if not r['ref'].startswith(('Table/Talk/', 'Table/Event/', 'Table/Quest/')):
            continue
        if z.count(' ') > len(z) * 0.2:      # 跳过带填充空格的格式串
            continue
        sample = (z, r['ref'])
        break
    if not sample:
        say('索引里找不到示例译文'); return 1
    text, ref = sample
    say('selftest 译文 %s | %s' % (ref, text[:48]))
    hit = Locator(None, idx, cfg)
    # 查表自测：用该条目的另一种指纹构造（直接验证 index 可查）
    say('selftest 索引键可查：%s' % ('是' if any(ref in [r['ref'] for r in v]
                                             for v in idx.values()) else '否'))
    names = load_names()
    if has_bra:
        sid, sname, slen = offline_speaker_check(cfg, names)
        sdisp = nameplate_text(names, sid) or sname   # 名牌实际显示：全名 + First Name
        say('说话人链路自测：%d 码台词串 → 0x8096 id=%s → 汉字名「%s」→ 名牌「%s」'
            % (slen, sid, sname, sdisp))
        if not sid:
            say('说话人 ID 解析失败，终止'); return 1
    else:
        sid = next((k for k, v in names.items()
                    if isinstance(v, dict) and v.get('zh')), '1')
        sdisp = nameplate_text(names, sid)
        say('说话人链路自测：跳过（无游戏文件），名牌示例「%s」' % sdisp)
    if not detect_selftest(cfg):
        say('对白框识别自测未通过，终止'); return 1

    game_w, game_h = 1296, 759          # 探针实测窗口客户区尺寸
    w = int(game_w * cfg['panel']['w_ratio'])
    h = int(game_h * cfg['panel']['h_ratio'])
    x, y = 260, 220
    npcfg = cfg['nameplate']
    nw = int(game_w * npcfg['w_ratio'])
    nh = int(game_h * npcfg['h_ratio'])
    nx, ny = 260, 150
    ov = OverlayWindow(x, y, w, h)
    ov_name = OverlayWindow(nx, ny, nw, nh)
    try:
        img = make_panel(text, w, h, cfg, game_h, True, True)
        ov.render(img, x, y)
        ov_name.render(make_nameplate(sdisp, nw, nh, cfg, game_h), nx, ny)
        time.sleep(0.5)
        ov.pump()
        make_align_preview(text, cfg, sdisp)
        from PIL import ImageGrab
        shot = ImageGrab.grab(all_screens=True)
        png = os.path.join(OUT, 'overlay_selftest.png')
        shot.save(png)
        # 面板底色像素计数（不透明底框应大量可见）
        bg = tuple(cfg['colors']['bg'][:3])
        crop = shot.convert('RGB').crop((x, y, min(shot.width, x + w),
                                         min(shot.height, y + h)))
        raw = crop.tobytes()
        n = sum(1 for i in range(0, len(raw), 3)
                if all(abs(raw[i + k] - bg[k]) <= 6 for k in range(3)))
        say('selftest 面板底色可见像素 %d / %d -> %s | 截图 %s'
            % (n, w * h, '可见 OK' if n > w * h * 0.4 else '不可见', png))
        return 0 if n > w * h * 0.4 else 1
    finally:
        ov.close()
        ov_name.close()


def diagnose():
    idx, cfg = load_index(), load_cfg()
    a = attach()
    if not a:
        return 1
    h, hwnd, rect, base = a
    loc = Locator(h, idx, cfg, base)
    try:
        for k in range(20):
            r = loc.probe()
            d = loc.last_diag
            say('[%02d] P=%s idx=%s T=%s tbb1=%s M=%s | %s'
                % (k, hex(d['P']) if d.get('P') else None, d.get('idx'),
                   hex(d['T']) if d.get('T') else None, d.get('tbb1'),
                   hex(d['M']) if d.get('M') else None,
                   ('命中 %s / %s / %s' % (r[2], r[1], r[0][:40])) if r else '未命中'))
            time.sleep(0.5)
    finally:
        k32.CloseHandle(h)
    return 0


def wndinfo():
    """窗口体检：不读游戏内存、不附着进程，只枚举窗口。
    切到无边框/全屏后运行 `LDZH_Overlay.bat wndinfo`，输出即可判断浮层为何不可见。"""
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass
    sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    pids = game_pids()
    say('屏幕 %dx%d | LostDimension.exe pid: %s'
        % (sw, sh, sorted(pids) if pids else '未运行'))
    if not pids:
        say('游戏没在运行。')
        return 1
    fg = u32.GetForegroundWindow()
    rows = []

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(hwnd, _):
        pid = W.DWORD()
        u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        cls = C.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        ttl = C.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, ttl, 512)
        r = W.RECT()
        u32.GetWindowRect(hwnd, C.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if pid.value not in pids and cls.value != OverlayWindow._cls:
            return True
        rows.append(dict(pid=pid.value, hwnd=hwnd, cls=cls.value, title=ttl.value,
                         x=r.left, y=r.top, w=w, h=h,
                         style=u32.GetWindowLongW(hwnd, -16) & 0xffffffff,
                         ex=u32.GetWindowLongW(hwnd, -20) & 0xffffffff,
                         parent=u32.GetParent(hwnd),
                         vis=bool(u32.IsWindowVisible(hwnd)),
                         fg=(hwnd == fg)))
        return True

    u32.EnumWindows(cb, 0)

    # 子窗口：overlay 是 WS_CHILD，不在 EnumWindows 里
    kids = []

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def kcb(hwnd, _):
        cls = C.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        if cls.value == OverlayWindow._cls:
            r = W.RECT()
            u32.GetWindowRect(hwnd, C.byref(r))
            pid = W.DWORD()
            u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
            kids.append(dict(hwnd=hwnd, pid=pid.value,
                             rect=(r.left, r.top, r.right - r.left, r.bottom - r.top),
                             vis=bool(u32.IsWindowVisible(hwnd)),
                             parent=u32.GetParent(hwnd)))
        return True

    for row in rows:
        if row['cls'] == OverlayWindow._cls or not row['vis']:
            continue
        u32.EnumChildWindows(row['hwnd'], kcb, 0)

    for row in sorted(rows, key=lambda r: (r['cls'] != 'LostDimension', -r['w'] * r['h'])):
        st, ex = row['style'], row['ex']
        caption = bool(st & 0x00C00000)
        layers = []
        if st & 0x80000000:
            layers.append('WS_POPUP')
        if st & 0x40000000:
            layers.append('WS_CHILD')
        if st & 0x10000000:
            layers.append('WS_VISIBLE')
        if caption:
            layers.append('WS_CAPTION')
        if ex & 0x00080000:
            layers.append('WS_EX_LAYERED')
        if ex & 0x00000008:
            layers.append('WS_EX_TOPMOST')
        say('[窗口] pid=%d hwnd=%s class=%s title=%r'
            % (row['pid'], hex(row['hwnd']), row['cls'], row['title']))
        say('        rect=(%d,%d) %dx%d  visible=%s  foreground=%s  parent=%s'
            % (row['x'], row['y'], row['w'], row['h'], row['vis'], row['fg'],
               hex(row['parent']) if row['parent'] else None))
        say('        style=0x%08X ex=0x%08X  %s'
            % (st, ex, '|'.join(layers) or '(none)'))
        if row['cls'] == 'LostDimension' or row['w'] >= sw * 0.9:
            full = (row['w'] >= sw * 0.9 and row['h'] >= sh * 0.9)
            if full and not caption:
                verdict = 'borderless 全屏（窗口铺满屏幕、无标题栏）→ 子窗口叠加应当可见'
            elif full:
                verdict = '带标题栏的铺满窗口'
            else:
                verdict = '窗口模式 %dx%d' % (row['w'], row['h'])
            say('        判定: %s' % verdict)
    for k in kids:
        say('[浮层子窗口] hwnd=%s pid=%d rect=%s visible=%s parent=%s'
            % (hex(k['hwnd']), k['pid'], k['rect'], k['vis'],
               hex(k['parent']) if k['parent'] else None))
    if not kids:
        say('[浮层子窗口] 无 —— 浮层进程没在跑，或窗口还没创建（无对白时不画）')
    say('（本命令只枚举窗口，不读内存、不改任何东西）')
    return 0


def grab_client_raw(rect, timeout=2.0):
    """纯粹抓一帧客户区（不带 _CAP 退避状态）。

    画面识别线程要用它：它跟对白框识别各抓各的，绝不能互相把 _CAP 判死
    （以前共用一个状态，画面识别一慢就会顺手把台词定位也停掉）。
    """
    from PIL import ImageGrab
    x, y, w, h = rect
    box = {}

    def _work():
        try:
            box['img'] = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        except Exception as e:
            box['err'] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise RuntimeError('抓帧阻塞')
    if 'err' in box:
        raise box['err']
    return box['img']


def capture_client(rect):
    """抓取游戏客户区画面用于对白框识别。
    本覆盖层面板是纯深色(8,16,34)、不描蓝边，名字条尺寸不满足饰条条件，
    都不会触发蓝框识别；原生饰条在面板内缩边缘外仍然可见。

    独占全屏 / 显示模式切换期间 GDI 抓帧会**阻塞**（20:27 那次就是卡在这里
    25 秒被看门狗收尸的）。所以这里做超时退避：一旦单次抓帧过慢或抓到黑屏，
    就暂停抓帧一段时间，改用固定比例定位——面板照样出现，只是不再像素对齐。"""
    from PIL import ImageGrab
    # 兼容两种矩形来源：find_game() 的 (pid,hwnd,x,y,w,h) 与 attach() 的 (x,y,w,h)。
    # 少了这层兼容，改 find_game 返回结构时会在这里抛 ValueError，
    # 而它又被上层 try 吞掉 → 表现为「框识别失败回落」，很难看出真因。
    if len(rect) == 6:
        _, _, x, y, w, h = rect
    else:
        x, y, w, h = rect
    now = time.time()
    if _CAP['dead']:
        raise RuntimeError('抓帧已停用（本会话改用固定比例定位）')
    if now < _CAP['skip_until']:
        raise RuntimeError('截屏冷却中（%.1fs 后重试）'
                           % (_CAP['skip_until'] - now))
    # GDI 抓帧在显示模式切换 / 独占全屏期间会长时间阻塞，join 超时保证它
    # 永远拖不死主循环：超时就判死抓帧线程，本会话退到固定比例定位。
    import threading
    box = {}
    def _work():
        try:
            box['img'] = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        except Exception as e:
            box['err'] = e
    t0 = time.time()
    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(2.0)
    if th.is_alive():
        _CAP['dead'] = True
        say('  [诊断] 抓帧阻塞超过 2 秒（全屏/模式切换下 GDI 抓不动），'
            '本会话停用截屏识别，改用固定比例定位')
        raise RuntimeError('抓帧阻塞')
    if 'err' in box:
        raise box['err']
    img = box['img']
    dt = time.time() - t0
    # 黑屏/全同色：抓帧成功但拿不到画面（独占全屏常见），同样不可用于识别
    dark = False
    try:
        lo, hi = img.convert('L').getextrema()
        dark = (hi - lo) < 6
    except Exception:
        pass
    if dt > 1.2 or dark:
        _CAP['skip_until'] = time.time() + 8.0
        _CAP['fails'] += 1
        if _CAP['fails'] <= 3:
            say('  [诊断] 抓帧%s（%.2fs），暂停截屏 8 秒并改用固定比例定位'
                % ('为黑屏/空白' if dark else '过慢', dt))
    return img


_NP_CALIB = None    # 名牌相对对白框的偏移比例：(dx,dy,w,h)÷框尺寸；像素识别一次后固定
_BOX_LOCK = None    # 上一次采用的对白框；识别抖动在死区内就沿用它，避免面板/名牌乱跳
_BEAT = [0.0]       # 主循环心跳时间戳（0 = 当前没有活动会话）
_STAGE = ['init']   # 主循环当前所处的阶段：卡死时看门狗把它写进日志，才知道卡在哪
_CAP = {'skip_until': 0.0, 'fails': 0, 'dead': False}   # 抓帧退避状态
_LAST_FB = [0.0]    # 上次打印「回落固定比例」的时间（日志限流）
_LAST_STATUS = [0.0]    # 上次打印状态行的时间（日志限流 5s）
_PROOF = [0.0, False]   # [面板开始显示的时刻, 本次会话是否已取证]
_TEXT_T = [0.0]         # 最近一次读到台词的时刻（只用于日志/诊断）
_HIDE_LOG = {}          # 收起原因的日志限流
_DIAG = {'weak': 0, 'none': 0, 'blank': 0}   # 本状态周期内的收帧统计
_LAST_FAIL_SHOT = [0.0]


def _note_hide(why):
    """记录「面板为什么被收起」。3 秒限流，避免刷屏。
    没有这条，日志里只剩「面板=隐藏」，事后根本分不清是没台词、
    被 Ctrl+F12 关了，还是判据误杀。"""
    now = time.time()
    if now - _HIDE_LOG.get(why, 0.0) < 3.0:
        return
    _HIDE_LOG[why] = now
    say('  [收起] 面板：%s' % why)


def _dump_fail_shot(img):
    """识别不到对白框时把画面留一份（10 秒最多一次）。
    全屏/无边框的对白框位置与窗口模式不同，有这个现场图就能直接校准，
    不用再让用户反复截图描述。"""
    if time.time() - _LAST_FAIL_SHOT[0] < 10.0:
        return
    _LAST_FAIL_SHOT[0] = time.time()
    try:
        p_ = os.path.join(WORK, 'overlay', 'fail_shot.png')
        img.save(p_)
        say('  [诊断] 未识别到对白框，现场图已存 work/overlay/fail_shot.png')
    except Exception:
        pass


def _proof_shot(rect):
    """面板稳定显示 3 秒后拍一张全屏：用来区分「没画出来」和「画了但被盖住」。
    放后台线程，抓不动也不影响主循环。"""
    import threading

    def run():
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(os.path.join(WORK, 'overlay', 'live_proof.png'))
            say('  [取证] 面板稳定显示 3 秒，全屏截图已存 work/overlay/live_proof.png')
        except Exception as e:
            say('  [取证] 截图失败：%r' % (e,))

    threading.Thread(target=run, daemon=True).start()


def _start_watchdog():
    """卡死自救。主循环可能阻塞在 ImageGrab/UpdateLayeredWindow/内存读里，
    那样它会一直活着却什么都不画，同时霸占单实例锁 —— 表现就是之后每次
    双击 bat 都静默退出（20:11 那次卡了十分钟就是这么来的）。
    这里在 25 秒没有心跳且游戏仍在运行时强杀自己，把锁释放掉。"""
    import threading

    def run():
        while True:
            time.sleep(3.0)
            try:
                if _BEAT[0] and time.time() - _BEAT[0] > 25.0 and find_game():
                    say('[!] 主循环 25 秒无响应（卡在 %s 阶段），强制退出以释放单实例锁'
                        % _STAGE[0])
                    os._exit(0)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


def _bump_np_calib(img, box, det):
    """用**原生蓝色名牌**的像素位置校准一次比例，之后不再重算。
    为什么只校准一次：覆盖层本身也会画蓝色名牌，后续截图会拍到自己的产物，
    反复识别就会自我漂移。一次校准 + 跟随框 = 既贴合又稳定。"""
    global _NP_CALIB
    if _NP_CALIB is not None or img is None:
        return
    try:
        nb = detect_boxes.find_nameplate(img, box, det)
    except Exception as e:
        say('  名牌校准失败（回落比例推导）: %s' % e)
        return
    if not nb:
        return
    nx, ny, nw, nh = nb
    bx, by, bw, bh = box
    # 识别到的是蓝像素核心带，名牌上下有渐隐边：外扩一点点保证「盖得住」
    px, py = int(nw * 0.02), int(nh * 0.04)
    nx, ny, nw, nh = nx - px, ny - py, nw + 2 * px, nh + 2 * py
    dx_r, dy_r = (nx - bx) / float(bw), (ny - by) / float(bh)
    w_r, h_r = nw / float(bw), nh / float(bh)
    if not (-0.7 < dy_r < 0.2 and 0.10 < w_r < 0.85 and 0.10 < h_r < 0.85):
        say('  名牌校准结果不合理，忽略（dx=%.3f dy=%.3f w=%.3f h=%.3f）'
            % (dx_r, dy_r, w_r, h_r))
        return
    _NP_CALIB = (dx_r, dy_r, w_r, h_r)
    say('  名牌像素校准完成：dx=%.3f dy=%.3f w=%.3f h=%.3f（此后固定跟随框）'
        % _NP_CALIB)


def layout_of(rect, cfg, auto, allow_calib=True):
    """计算本次要画的位置。auto=True：抓画面识别对白框（任意分辨率/黑边/
    上下双开自动适配，取最下面的框为主说话人）；识别失败回落固定比例。"""
    global _BOX_LOCK
    cx, cy, cw, chh = rect
    det = cfg.get('detect', {})
    boxes, img = [], None
    if auto:
        try:
            _STAGE[0] = 'capture'
            img = capture_client(rect)
            _STAGE[0] = 'detect'
            boxes, _acc = detect_boxes.find_dialog_boxes(img, det)
            if not boxes and img is not None:
                _dump_fail_shot(img)      # 留现场，避免再靠猜
            if len(boxes) > 1:
                say('  检测到 %d 个对白框（双开模式），面板跟随最下方的框' % len(boxes))
        except Exception as e:
            # 每帧都会走到这里（抓帧停用后尤为频繁），日志必须限流，否则刷屏
            if time.time() - _LAST_FB[0] > 10.0:
                _LAST_FB[0] = time.time()
                say('  框识别不可用，回落到固定比例: %s' % e)
            boxes = []
    if boxes:
        # 合理性闸门：画面里角色的青色饰带 / 发光特效也会被当成「竖向蓝饰条」，
        # 于是识别出的框左边界贴到画面左边（实测 x=96，真值 x≈380），
        # 面板和名牌整体左移、名牌盖不住原生英文名。这类结果必须丢掉。
        iw, ih = img.size if img is not None else (cw, chh)
        def _plausible(b):
            bx, by, bw, bh = b
            return (bx >= iw * 0.10 and bx + bw <= iw * 0.92
                    and ih * 0.07 <= bh <= ih * 0.24
                    and 4.0 <= bw / float(bh) <= 14.0)
        good = [b for b in boxes if _plausible(b)]
        if len(good) != len(boxes):
            if time.time() - _LAST_FB[0] > 10.0:
                _LAST_FB[0] = time.time()
                say('  框识别结果不合理（左边界贴边/尺寸异常），已丢弃并回落固定比例')
            boxes = good
    if boxes:
        b = boxes[-1]                                  # 主框 = 最下面的
        # 抖动死区：粗网格识别的框每帧会漂 1~3px，超过阈值才认新位置
        if _BOX_LOCK and all(abs(u - v) <= 4 for u, v in zip(_BOX_LOCK, b)):
            b = _BOX_LOCK
        else:
            _BOX_LOCK = b
        bx, by, bw, bh = b
        if allow_calib:
            _bump_np_calib(img, b, det)
        px, py, pw, ph = detect_boxes.panel_from_box(b, det)
        if _NP_CALIB:
            dx_r, dy_r, w_r, h_r = _NP_CALIB
            nx, ny = int(bx + dx_r * bw), int(by + dy_r * bh)
            nw, nh = int(bw * w_r), int(bh * h_r)
        else:
            nx, ny, nw, nh = detect_boxes.nameplate_from_box(b, det)
        return {'panel': (cx + px, cy + py, pw, ph),
                'nameplate': (cx + nx, cy + ny, nw, nh), 'box': b}
    # 回落：写死比例（1920x1080 校准值），仅作识别失败时的保险
    p, npcfg = cfg['panel'], cfg['nameplate']
    yr = p['y_ratio'] if cfg.get('cover', True) else p.get('bilingual_y_ratio', 0.36)
    return {'panel': (cx + int(cw * p['x_ratio']), cy + int(chh * yr),
                      int(cw * p['w_ratio']), int(chh * p['h_ratio'])),
            'nameplate': (cx + int(cw * npcfg['x_ratio']),
                          cy + int(chh * npcfg['y_ratio']),
                          int(cw * npcfg['w_ratio']), int(chh * npcfg['h_ratio'])),
            'box': None}


def _live_once():
    """单个游戏会话：附着当前运行的游戏并显示，游戏退出/窗口消失时返回。"""
    global _BOX_LOCK
    _BOX_LOCK = None                 # 新会话：作废旧窗口坐标系下的框
    _BEAT[0] = time.time()           # 进入会话即开始计时
    idx, cfg = load_index(), load_cfg()
    a = attach()
    if not a:
        return 1
    h, hwnd, fallback_rect, base = a
    try:
        u32.SetProcessDPIAware()      # 截屏与窗口坐标统一到物理像素
    except Exception:
        pass
    loc = Locator(h, idx, cfg, base)
    hk = cfg['hotkeys']
    npcfg = cfg['nameplate']
    names = load_names()
    say('汉字角色名表 %d 条' % len(names))
    opaque = cfg.get('opaque', True) and '--float' not in sys.argv
    # 用户定调：默认精确对齐并盖住原生对白框；--bilingual 仅在固定比例回落时生效
    cover = cfg.get('cover', True) and '--bilingual' not in sys.argv
    # 画面识别定位：分辨率/黑边/上下双开自动适配；--manual 或 F10 可切回固定比例
    auto = cfg.get('layout', 'auto') == 'auto' and '--manual' not in sys.argv
    visible, last_text, last_rect = True, None, None
    last_name, last_speaker = None, None
    ov, ov_name, ov_text = None, None, None
    lay, np_last, np_shown = None, None, None
    ov_key = None
    frame = 0
    # 画面识别线程：剧情走内存读串，选单/说明/注释走这条路（后台慢速轮询，不卡主循环）
    _rect_now = [None]
    watcher = TextWatcher(lambda: _rect_now[0], cfg,
                          hide_cb=lambda: (ov_text and u32.IsWindowVisible(ov_text.hwnd)
                                           and ov_text.hide()))
    watcher.start()
    lost = 0                    # 连续找不到游戏窗口的帧数（窗口重建/退出的判定）
    say('覆盖层已挂载（桌面歌词式置顶：不看前台、始终压在游戏画面之上，'
        '只有游戏最小化才收起）：%s + 汉字名字条；'
        'Ctrl+F12 显隐 / Ctrl+F11 遮挡-浮标 / Ctrl+F10 定位方式（无退出键）；'
        '画面文本覆盖默认关闭（Ctrl+F9 可开）'
        % ('画面识别定位（对齐对白框，盖住原文）' if auto
           else '固定比例定位（1920x1080 校准值）'))
    # 热键一律要求按住 Ctrl：游戏本身也用 ESC / F11（全屏切换）等键，
    # 之前用裸 ESC 退出，玩家在游戏里一按 ESC 开菜单就把覆盖层关掉了。
    hk_ctrl = bool(hk.get('require_ctrl', True))

    def hot(vk):
        if hk_ctrl and not (u32.GetAsyncKeyState(0x11) & 0x8000):   # VK_CONTROL
            return False
        return bool(u32.GetAsyncKeyState(vk) & 0x8000)

    try:
        while True:
            # 心跳必须每转一圈都喂：以前只在「游戏在前台」的分支里喂，
            # 结果玩家切到浏览器/聊天窗口看日志超过 25 秒，看门狗就把它
            # 当成卡死自杀了（20:27 那次「没浮层」正是这么没的）。
            _BEAT[0] = time.time()
            if ov:
                ov.pump()
            if hot(hk['toggle_vk']):
                visible = not visible
                for w_ in (ov, ov_name, ov_text):
                    if w_:
                        (w_.show if visible else w_.hide)()
                time.sleep(0.25)
            if hot(hk['mode_vk']):
                opaque = not opaque
                last_text = None
                time.sleep(0.25)
            if hot(hk.get('layout_vk', 121)):
                auto = not auto
                last_text = None
                time.sleep(0.25)
            if hot(hk.get('screen_vk', 120)):          # Ctrl+F9 画面文本覆盖开关
                SCR_ON[0] = not SCR_ON[0]
                _SCR['sig'] = None
                say('画面文本覆盖：%s（选单/说明/注释）' % ('开' if SCR_ON[0] else '关'))
                time.sleep(0.25)
            if hot(hk.get('shot_vk', 119)):            # Ctrl+F8 抓现场图（校准用）
                try:
                    r_ = client_rect(hwnd) or fallback_rect
                    grab_client_raw(r_).save(os.path.join(WORK, 'overlay', 'shot_manual.png'))
                    say('已存现场图 work/overlay/shot_manual.png（%s）' % (r_,))
                except Exception as e:
                    say('[!] 抓现场图失败：%r' % (e,))
                time.sleep(0.4)

            # ---- 绑定游戏窗体：仅在游戏前台且可见时显示，否则隐藏/退出 ----
            gst = game_focus_state(hwnd)
            if gst == 'closed':
                # 窗口句柄失效：游戏可能重建了窗口（切全屏/改分辨率/重进关卡）。
                # 先尝试重新绑定到新窗口，只有在持续找不到时才真正退出。
                ng = find_game(pid_hint=pid_of_window(hwnd))
                if ng and int(ng[1]) != int(hwnd):
                    say('游戏窗口已重建（新句柄 %s），重新绑定…' % hex(ng[1]))
                    for w_ in (ov, ov_name, ov_text):
                        if w_:
                            try:
                                w_.close()
                            except Exception:
                                pass
                    ov, ov_name, ov_text = None, None, None
                    last_text, last_rect, last_name = None, None, None
                    try:
                        k32.CloseHandle(h)
                    except Exception:
                        pass
                    a2 = attach()
                    if a2:
                        h, hwnd, fallback_rect, base = a2
                        loc = Locator(h, idx, cfg, base)
                        lost = 0
                        continue
                lost += 1
                if lost == 1:
                    say('窗口句柄失效（切全屏/无边框/改分辨率？），正在查找重建后的窗口…')
                if lost % 20 == 0:          # 周期性打印候选窗口，便于事后排查
                    find_game(verbose=True)
                # 进程级判定：游戏进程全没了才是真退出，不必再干等满 6 秒。
                # 只要进程还在（窗口正在重建/切全屏），就继续等新窗口。
                if not game_pids():
                    say('游戏进程已退出，本次会话结束')
                    break
                if lost > 60:               # 约 6 秒仍找不到 → 认为本局已结束，交回守护
                    say('游戏窗口已消失，本次会话结束')
                    break
                time.sleep(0.1)
                continue
            lost = 0
            # 桌面歌词式置顶（用户 20:43 定调）：不看前台是谁，只要游戏还在跑就
            # 一直把译文压在屏幕最上层。以前「切走就隐藏」导致玩家一切窗口就以为
            # 浮层挂了；现在只有游戏最小化/窗口消失才收起。
            if gst == 'minimized':
                if ov and u32.IsWindowVisible(ov.hwnd):
                    ov.hide()
                if ov_name and u32.IsWindowVisible(ov_name.hwnd):
                    ov_name.hide()
                time.sleep(1.0 / cfg['fps'])
                continue

            rect = client_rect(hwnd) or fallback_rect
            # 子窗口 / 顶层窗口的选择：全屏（无边框）必须用桌面顶层 TOPMOST，
            # 挂成游戏 WS_CHILD 子窗口会被 OpenGL 画面压住。
            want_par = None if (is_fullscreen(rect) and '--child' not in sys.argv) else hwnd
            # 画面识别线程要的是 (x,y,w,h)；find_game 的 6 元组在这里先拆掉
            _rect_now[0] = ((rect[2], rect[3], rect[4], rect[5])
                            if len(rect) == 6 else tuple(rect))
            _BEAT[0] = time.time()          # 心跳：喂看门狗，卡死时它能收尸放锁
            # 单帧异常（内存读失败/窗口被销毁/UpdateLayeredWindow 失败）不应让整个
            # 覆盖层退出：清掉窗口、下一帧重建，避免切全屏或改分辨率后「直接没了」。
            try:
                _STAGE[0] = 'probe'
                hit = loc.probe()
                text = hit[0] if hit else None
                sid = speaker_id(loc.last_codes) if hit else None
                # 弱命中 = 消息对象里「非主文本槽」的指针（日志里的 ptr@+0x50 /
                # ptr@+0x394）。主文本槽是 +0xF4（实机统计 537/608 次命中）。
                # 事件播完之后这些旁槽仍指向旧串，且串里没有 0x8096 说话人码 ——
                # 于是浮层会把上一句台词一直压在自由行动的画面上（23:26 的
                # Event500_Pillar 就是这样：人已经在 Lobby 里走动，浮层还挂着独白）。
                # 判据：弱命中且本次拿不到说话人 ID → 当没台词处理，直接收起来。
                # 判据只有一条：内存里读到台词 → 显示；读不到 → 隐藏。
                # 不再按「主文本槽 / 旁槽」区分、也不做时间宽限 —— 那些规则会让
                # 面板在日常跑图时被误判成「没有台词」，表现就是浮层时有时无。
                # 旁槽残留旧串的副作用（事件播完仍压着一句）由「无对话框」本身的
                # 读数变化来收敛：游戏一收起对话框，读串就空了。
                if text:
                    _TEXT_T[0] = time.time()
                else:
                    _DIAG['blank'] += 1
                # 有台词时画面识别暂停：对白区由内存读串负责，避免两套盖板打架
                watcher.pause = bool(text)
                if sid:
                    last_speaker = sid      # 控制码只在换人时出现，沿用上次 ID
                name = speaker_name(names, last_speaker)
                # 名字条显示「全名 + 英文首名」，日志仍用纯汉字名
                np_name = nameplate_text(names, last_speaker) if last_speaker else name
                # 有台词就显示、没有就隐藏 —— 唯一判据，其余状态一律不参与
                want_panel = bool(visible and text)
                frame += 1
                # 布局变化（换行/移窗/改说话人）或周期性重识别（双开开关可能不带换行）
                if (text != last_text or rect != last_rect or name != last_name
                        or (auto and frame % 30 == 0)):
                    last_text, last_rect, last_name = text, rect, name
                    _STAGE[0] = 'layout'
                    lay = layout_of(rect, cfg, auto, allow_calib=(ov is None))
                    px, py, pw, ph = lay['panel']
                    # 全屏/无边框：游戏用 OpenGL 直接铺满整个客户区，挂在其下的
                    # WS_CHILD 子窗口会被游戏画面压住 —— 窗口模式下正常，全屏就「消失」。
                    # 所以全屏时改用桌面顶层 TOPMOST 窗口叠上去（坐标自动走屏幕坐标）；
                    # 窗口模式仍用子窗口，跟着游戏移动/缩放。
                    if (ov is not None and bool(ov.parent) != bool(want_par)) or \
                            (ov_name is not None and bool(ov_name.parent) != bool(want_par)):
                        for w_ in (ov, ov_name, ov_text):        # parent 策略变了 → 重建
                            if w_:
                                try:
                                    w_.close()
                                except Exception:
                                    pass
                        ov, ov_name, ov_text = None, None, None
                    if hit and text:
                        say('  %-14s %-42s [%s] %s'
                            % (hit[2], hit[1], name or '?', text[:36]))
                # ---- 对白面板：每帧闭环（不再绑在「文本变化」块里）----
                # 以前只在本帧 text 有值时才画，而「文本没变」的帧整段跳过 →
                # 任何一次 hide 都会一直保持到下一句台词，日志却照样写「显示」。
                if want_panel and lay is not None:
                    px, py, pw, ph = lay['panel']
                    if ov is None or bool(ov.parent) != bool(want_par):
                        if ov:
                            try:
                                ov.close()
                            except Exception:
                                pass
                        ov = OverlayWindow(px, py, pw, ph, parent=want_par)
                        ov_key = None
                    pk = (text, px, py, pw, ph, opaque)
                    if pk != ov_key or not u32.IsWindowVisible(ov.hwnd):
                        _STAGE[0] = 'draw'
                        img = make_panel(text, pw, ph, cfg, rect[3], opaque, cover)
                        _STAGE[0] = 'render'
                        ov.render(img, px, py)
                        ov_key = pk
                    _STAGE[0] = 'front'
                    if not u32.IsWindowVisible(ov.hwnd):
                        ov.show()           # 被藏过/被系统收起 → 每帧自动恢复
                    ov.to_front()           # 游戏在前台时抢占最前端
                    if not _PROOF[0]:
                        _PROOF[0] = time.time()
                    elif (not _PROOF[1]) and time.time() - _PROOF[0] > 3.0:
                        # 面板已连续显示 3 秒 → 拍一张全屏留证：画面里有没有它，
                        # 一眼就能分辨「没画出来」和「画了但被盖住」。
                        _PROOF[1] = True
                        _proof_shot(rect)
                elif ov is not None and not want_panel:
                    _note_hide('已按 Ctrl+F12 关闭显示' if not visible
                               else '当前没有对话框（内存里读不到台词）')
                    if u32.IsWindowVisible(ov.hwnd):
                        ov.hide()
                    ov_key = None
                # ---- 汉字名字条：盖住原生英文名字牌（主框=最下面的框）----
                # 每帧保活，而不是「只在换句时画一次」：以前一旦某帧把它 hide 掉、
                # 或者几何被识别结果带歪（窗口可见但落在客户区外/退化成几十像素），
                # 下一句文本又没变，就再也没机会重画 —— 表现就是「人名框坏了」。
                np_box = (lay.get('nameplate')
                          if (lay and want_panel and text
                              and (name or npcfg.get('always_draw', True)))
                          else None)
                if np_box:
                    nx, ny, nw, nh = np_sane(np_box, rect, cfg)
                    np_key = (np_name or '', nx, ny, nw, nh)
                    if ov_name is None or bool(ov_name.parent) != bool(want_par):
                        if ov_name:
                            try:
                                ov_name.close()
                            except Exception:
                                pass
                        ov_name = OverlayWindow(nx, ny, nw, nh, parent=want_par)
                        np_last, np_shown = None, None
                    if np_key != np_last or not u32.IsWindowVisible(ov_name.hwnd):
                        np_last = np_key
                        ov_name.render(make_nameplate(np_name, nw, nh, cfg, rect[3]), nx, ny)
                        if np_shown != np_key:
                            np_shown = np_key
                            say('  [名牌] %s @ (%d,%d) %dx%d'
                                % (np_name or '(空)', nx, ny, nw, nh))
                    if not u32.IsWindowVisible(ov_name.hwnd):
                        ov_name.show()          # 被藏过/被系统收起 → 每帧自动恢复
                    ov_name.to_front()
                elif ov_name:
                    if u32.IsWindowVisible(ov_name.hwnd):
                        ov_name.hide()
                    np_last = None
                # ---- 选单 / 说明 / 注释：画面识别结果贴到全屏透明层 ----
                with watcher.lock:
                    scr_runs = list(_SCR['runs']) if time.time() - _SCR['stamp'] < 6.0 else []
                    scr_size = _SCR['size']
                    scr_src = _SCR.get('src')
                have_scr = bool(SCR_ON[0] and visible and not text and scr_runs and scr_size)
                if have_scr:
                    sx, sy = int(_rect_now[0][0]), int(_rect_now[0][1])
                    sig = (scr_size, tuple((r['box'], r['zh']) for r in scr_runs))
                    if sig != _SCR['sig']:
                        _SCR['sig'] = sig
                        want_par2 = (None if (is_fullscreen(rect) and '--child' not in sys.argv)
                                     else hwnd)
                        simg = make_screen_image(scr_size, scr_runs, cfg, scr_src)
                        if (ov_text is None or bool(ov_text.parent) != bool(want_par2)
                                or (ov_text.w, ov_text.h) != scr_size):
                            if ov_text:
                                try:
                                    ov_text.close()
                                except Exception:
                                    pass
                            ov_text = OverlayWindow(sx, sy, scr_size[0], scr_size[1],
                                                    parent=want_par2)
                        ov_text.render(simg, sx, sy)
                        ov_text.to_front()
                    if ov_text is not None and not u32.IsWindowVisible(ov_text.hwnd):
                        ov_text.show()          # 抓帧时临时藏过，这里恢复
                elif ov_text is not None:
                    _SCR['sig'] = None
                    if u32.IsWindowVisible(ov_text.hwnd):
                        ov_text.hide()
            except Exception as e:
                say('[!] 单帧异常（%s 阶段），重建覆盖层：%r' % (_STAGE[0], e))
                for w_ in (ov, ov_name, ov_text):
                    if w_:
                        try:
                            w_.close()
                        except Exception:
                            pass
                ov, ov_name, ov_text = None, None, None
                last_text, last_rect, last_name = None, None, None
                time.sleep(0.2)
            # 状态行：日志里能事后回答「当时浮层到底显示了没有」，
            # 不用再靠玩家口述。5 秒一条，不刷屏。
            if time.time() - _LAST_STATUS[0] > 5.0:
                _LAST_STATUS[0] = time.time()
                try:
                    # 名牌窗口的实际屏幕矩形一起打出来：万一又出现「日志说显示、
                    # 画面里没有」，一眼就能看出是跑到屏幕外了还是渲染没上去。
                    np_txt = '隐藏'
                    if ov_name and u32.IsWindowVisible(ov_name.hwnd):
                        rr = W.RECT()
                        u32.GetWindowRect(ov_name.hwnd, C.byref(rr))
                        np_txt = '显示(%d,%d %dx%d)' % (rr.left, rr.top,
                                                        rr.right - rr.left,
                                                        rr.bottom - rr.top)
                    say('[状态] 前台=%s 开关=%s 面板=%s 名牌=%s 台词=%d字 抓帧=%s 无台词帧=%d'
                        % ('游戏' if game_focus_state(hwnd) == 'ok' else '其它',
                           '显' if visible else '隐',
                           '显示' if (ov and u32.IsWindowVisible(ov.hwnd)) else
                           ('未建' if ov is None else '隐藏'),
                           np_txt,
                           len(text or ''),
                           '停用' if _CAP['dead'] else
                           ('冷却' if time.time() < _CAP['skip_until'] else '正常'),
                           _DIAG['blank']))
                    _DIAG['blank'] = 0
                except Exception:
                    pass
            time.sleep(1.0 / cfg['fps'])
    finally:
        watcher.stop = True
        for w_ in (ov, ov_name, ov_text):
            if w_:
                w_.close()
        k32.CloseHandle(h)
    say('本次会话结束（未改动任何游戏文件）')
    return 0


# ---------------------------------------------------------------- 随游戏运行的浮层
# 只保留一个「停止标志」用于人工收尾（工具/维护用），不加任何开机自启。
STOP_FLAG = os.path.join(WORK, 'overlay', '.stop')


def live():
    """随游戏启动的浮层：等游戏出现即挂载，游戏退出即退出（不驻留、不自启）。

    - 游戏没开 → 每秒轮询等待（本程序不启动游戏）
    - 游戏开了 → 自动附着、抢占最前端；切到其它应用/最小化 → 隐藏（不退出）
    - 游戏退出 → 关闭覆盖层并退出本进程
    - 没有退出热键：只能等游戏退出，或放置 overlay/.stop 标志
    """
    _ensure_log()
    say('==== %s 浮层启动 ====' % time.strftime('%Y-%m-%d %H:%M:%S'))
    # 单实例：重复启动只会空转，让后来者直接退出
    try:
        k32.CreateMutexW.restype = C.c_void_p
        k32.CreateMutexW.argtypes = [C.c_void_p, C.c_bool, C.c_wchar_p]
    except Exception:
        pass
    mtx = k32.CreateMutexW(None, False, u'Local\\LDZH_Overlay_Daemon')
    if mtx and k32.GetLastError() == 183:            # ERROR_ALREADY_EXISTS
        say('已有浮层在运行，本实例退出。')
        return 0
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass
    _start_watchdog()                                # 卡死时自动收尸、释放单实例锁
    said_wait = False
    fails = 0
    while True:
        if os.path.exists(STOP_FLAG):
            try:
                os.remove(STOP_FLAG)
            except OSError:
                pass
            say('收到停止标志，浮层退出。')
            break
        if not find_game():
            if not said_wait:
                say('等待游戏启动…（不启动游戏；检测到 LostDimension 窗口后自动挂载，'
                    '游戏退出后本程序也退出）')
                said_wait = True
            time.sleep(1.0)
            continue
        said_wait = False
        try:
            rc = _live_once()
        except Exception as e:                       # 任何异常都不许把浮层带崩
            fails += 1
            say('[!] 附着/运行异常（第 %d 次）：%r' % (fails, e))
            time.sleep(min(2.0 * fails, 10.0))
            continue
        finally:
            # 会话结束（正常/异常都算）：清心跳，免得看门狗把「守护等待」误判成卡死
            _BEAT[0] = 0.0
        if rc == 1:                                  # 附着失败：退避重试
            fails += 1
            time.sleep(min(2.0 * fails, 10.0))
            continue        # rc == 0：本次会话结束 = 游戏已退出 → 浮层随之退出
        say('游戏已退出，浮层随之退出。')
        break
    return 0


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'selftest'
    fn = {'selftest': selftest, 'diagnose': diagnose, 'wndinfo': wndinfo, 'live': live}.get(mode)
    sys.exit(fn() if fn else (say('未知模式: %s' % mode) or 1))
