# -*- coding: utf-8 -*-
"""覆盖层方案「只读实机探针」。

验证两个未知数：
  ① 置顶窗口能否显示在全屏 OpenGL 游戏画面之上（截图判据）；
  ② 运行时内存里能否找到当前对白码位串，并命中「码位串 -> 完整译文」索引。

严格只读：不换 Data.bra、不改 exe、不写游戏内存、不安装任何东西。
启动游戏 -> 采样 -> 自动退出。所有产物落在 work/probe_overlay/。

用法：
  python probe_overlay.py selftest   # 纯离线自检，不启动游戏
  python probe_overlay.py run        # 实机只读探针（需用户批准）
"""
import os, sys, io, json, time, re, hashlib, subprocess, shutil
import ctypes as C
from ctypes import wintypes as W

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'work', 'tools'))
import ld_bra, tbb

EXE = os.path.join(ROOT, 'LostDimension.exe')
BRA = os.path.join(ROOT, 'Data.bra')
CORPUS = os.path.join(ROOT, 'work', 'ps3map', 'zh_story_full.jsonl')
IDXPATH = os.path.join(ROOT, 'work', 'ps3map', 'overlay_index.json')
OUT = os.path.join(ROOT, 'work', 'probe_overlay')

k32 = C.WinDLL('kernel32', use_last_error=True)
u32 = C.WinDLL('user32', use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_ALL = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


class MBI(C.Structure):
    # 64 位调用者的 MEMORY_BASIC_INFORMATION 布局（查 WOW64 目标同样按调用者位数）
    _fields_ = [('BaseAddress', C.c_ulonglong),
                ('AllocationBase', C.c_ulonglong),
                ('AllocationProtect', C.c_ulong),
                ('_a1', C.c_ulong),
                ('RegionSize', C.c_ulonglong),
                ('State', C.c_ulong),
                ('Protect', C.c_ulong),
                ('Type', C.c_ulong),
                ('_a2', C.c_ulong)]


k32.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(MBI), C.c_size_t]
k32.VirtualQueryEx.restype = C.c_size_t
k32.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t,
                                  C.POINTER(C.c_size_t)]
k32.ReadProcessMemory.restype = W.BOOL
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.OpenProcess.restype = W.HANDLE
k32.TerminateProcess.argtypes = [W.HANDLE, C.c_uint]
k32.CloseHandle.argtypes = [W.HANDLE]
u32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
u32.MapVirtualKeyW.argtypes = [C.c_uint, C.c_uint]
u32.MapVirtualKeyW.restype = C.c_uint
u32.keybd_event.argtypes = [C.c_ubyte, C.c_ubyte, W.DWORD, C.c_size_t]
u32.SetWindowLongW.argtypes = [W.HWND, C.c_int, C.c_long]
u32.SetWindowLongW.restype = C.c_long
u32.GetWindowLongW.argtypes = [W.HWND, C.c_int]
u32.GetWindowLongW.restype = C.c_long
u32.ShowWindow.argtypes = [W.HWND, C.c_int]
u32.BringWindowToTop.argtypes = [W.HWND]
u32.SetForegroundWindow.argtypes = [W.HWND]
u32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.PostMessageW.argtypes = [W.HWND, C.c_uint, W.WPARAM, W.LPARAM]
# EnumWindows 的回调由 WINFUNCTYPE 自动转换，不要设 argtypes

LOG = None


def say(s):
    print(s, flush=True)
    if LOG:
        LOG.write(s + '\n')
        LOG.flush()


def fp(codes):
    return hashlib.md5(b''.join(int(c).to_bytes(2, 'little')
                               for c in codes)).hexdigest()[:16]


def u16bytes(codes):
    return b''.join(int(c).to_bytes(2, 'little') for c in codes)


# ---------------------------------------------------------------- 索引/前缀
def build_prefix():
    """从实装 Data.bra 建「前 4 码位(8字节) -> 完整串/译文」字典。"""
    zh = {}
    for line in io.open(CORPUS, encoding='utf-8'):
        o = json.loads(line)
        z = o.get('zh', '')
        if z and z != '占位':
            zh[(o['file'], o['table'], o['entry'])] = z
    idx = json.load(io.open(IDXPATH, encoding='utf-8'))['index']
    tmp = os.path.join(OUT, '_bra_tmp')
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, exist_ok=True)
    ld_bra.bra_extract(BRA, tmp)
    prefix = {}
    samples = []
    try:
        for dp, _, fns in os.walk(os.path.join(tmp, 'Table')):
            for fn in fns:
                if not fn.endswith('.mbb'):
                    continue
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, tmp).replace('\\', '/')
                d = open(p, 'rb').read()
                try:
                    e = tbb.endian(d)
                    tbls = tbb.tables(d)
                except Exception:
                    continue
                for ti, t in enumerate(tbls):
                    try:
                        rows = tbb.entries(d, t, e)
                    except Exception:
                        continue
                    for i, off, cs in rows:
                        if cs is None or len(cs) < 4:
                            continue
                        z = zh.get((rel, ti, i))
                        if not z:
                            continue
                        b = u16bytes(cs)
                        key = b[:8]
                        rec = {'bytes': b, 'ref': '%s|t%d|e%d' % (rel, ti, i),
                               'zh': z, 'n': len(cs), 'fp': fp(cs)}
                        prefix.setdefault(key, []).append(rec)
                        if len(samples) < 6 and len(cs) >= 8:
                            samples.append(rec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return prefix, idx, samples


def scan_bytes(data, prefix, idx=None, limit=400):
    """在内存字节块里找对白码位串；返回命中列表。

    numpy 向量化：把「前 4 码位」当 uint64 键，整块做 searchsorted 批量查找，
    只对真正命中的起点回查完整串。两种字节对齐（偶/奇起点）都覆盖。
    """
    import numpy as np
    if not hasattr(scan_bytes, '_pk'):
        scan_bytes._pmap = {int.from_bytes(k, 'little'): v
                            for k, v in prefix.items()}
        scan_bytes._pk = np.sort(np.fromiter(scan_bytes._pmap.keys(), dtype='<u8'))
    pk, pmap = scan_bytes._pk, scan_bytes._pmap
    hits, seen = [], set()
    BLOCK = 8 * 1024 * 1024
    for start in range(0, len(data), BLOCK):
        # 多取 8 字节，避免窗口被块边界切开；seen 去重处理重叠部分
        chunk = data[start:min(len(data), start + BLOCK + 8)]
        # u16 串可从任意字节起点开始；8 组错位 uint64 窗口覆盖所有起点
        # （offset=i%8、步长 8，8 组合起来即每个字节位置各一次）
        for off in range(8):
            n = (len(chunk) - off) // 8
            if not n:
                continue
            vals = np.frombuffer(chunk, dtype='<u8', count=n, offset=off)
            pos = np.searchsorted(pk, vals)
            mask = pk[np.minimum(pos, len(pk) - 1)] == vals
            for j in np.flatnonzero(mask):
                i = start + off + 8 * int(j)
                for r in pmap.get(int(vals[j]), ()):
                    ln = r['n']
                    if i + ln * 2 > len(data):
                        continue
                    # 只认整串精确相等，避免前缀相同造成的误报
                    if data[i:i + ln * 2] == r['bytes'] and (i, r['ref']) not in seen:
                        seen.add((i, r['ref']))
                        hits.append({'off': i, 'ref': r['ref'], 'zh': r['zh'],
                                     'n': ln, 'exact': True})
                        if len(hits) >= limit:
                            return hits
    return hits


def selftest():
    os.makedirs(OUT, exist_ok=True)
    prefix, idx, samples = build_prefix()
    say('selftest: 前缀键 %d 个 | 索引指纹 %d 个' % (len(prefix), len(idx)))
    blob = bytearray(os.urandom(4096))
    expect = []
    for off, r in zip((128, 900, 2100), samples):
        blob[off:off + len(r['bytes'])] = r['bytes']
        expect.append(r['zh'][:24])
    hits = scan_bytes(bytes(blob), prefix, idx)
    say('selftest: 注入 %d 条 -> 命中 %d 条' % (len(expect), len(hits)))
    for h in hits[:8]:
        say('   off=%-6d n=%-3d exact=%s %s | %s' %
            (h['off'], h['n'], h['exact'], h['ref'], h['zh'][:40]))
    ok = len(hits) >= len(expect)
    say('selftest: %s' % ('通过' if ok else '未通过'))
    return 0 if ok else 1


# ---------------------------------------------------------------- 内存读取
def read_regions(h, cap_mb=250):
    """枚举可读的已提交私有内存，逐块读回。"""
    out, total = [], 0
    addr = 0x10000
    end = 0x80000000
    while addr < end and total < cap_mb * 1024 * 1024:
        mbi = MBI()
        if not k32.VirtualQueryEx(h, C.c_void_p(addr), C.byref(mbi), C.sizeof(mbi)):
            addr += 0x10000
            continue
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if size == 0 or base >= end:
            break
        addr = base + size
        if (mbi.State != MEM_COMMIT or mbi.Type != MEM_PRIVATE
                or mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)):
            continue
        n = min(size, 32 * 1024 * 1024)
        buf = C.create_string_buffer(n)
        got = C.c_size_t(0)
        if k32.ReadProcessMemory(h, C.c_void_p(base), buf, n, C.byref(got)) and got.value:
            out.append((base, buf.raw[:got.value]))
            total += got.value
    return out, total


def read_pages_fallback(h, cap_mb=250):
    """VirtualQueryEx 在 WOW64 上不可用时，按 64KB 页直接试读。"""
    out, total = [], 0
    for addr in range(0x10000, 0x80000000, 0x10000):
        if total >= cap_mb * 1024 * 1024:
            break
        buf = C.create_string_buffer(0x10000)
        got = C.c_size_t(0)
        if k32.ReadProcessMemory(h, C.c_void_p(addr), buf, 0x10000, C.byref(got)) and got.value:
            out.append((addr, buf.raw[:got.value]))
            total += got.value
    return out, total


# ---------------------------------------------------------------- 窗口/输入
def find_win(pid):
    res = []

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(hwnd, _):
        q = W.DWORD()
        u32.GetWindowThreadProcessId(hwnd, C.byref(q))
        if q.value == pid:
            c = C.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, c, 256)
            if c.value == 'LostDimension':
                r = W.RECT()
                u32.GetWindowRect(hwnd, C.byref(r))
                res.append((hwnd, r.right - r.left, r.bottom - r.top,
                            r.left, r.top))
        return True

    u32.EnumWindows(cb, 0)
    return max(res, key=lambda t: t[1] * t[2]) if res else None


def focus(hwnd):
    u32.ShowWindow(hwnd, 9)
    u32.BringWindowToTop(hwnd)
    u32.SetForegroundWindow(hwnd)


def press(vk, hold=0.12, gap=0.8):
    sc = u32.MapVirtualKeyW(vk, 0)
    u32.keybd_event(vk, sc, 2, 0)
    time.sleep(hold)
    u32.keybd_event(vk, sc, 2 | 1, 0)
    time.sleep(gap)


# ---- 纯 Win32 分层窗口（不依赖 tkinter；与正式覆盖层同一机制）----
WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, C.c_uint, W.WPARAM, W.LPARAM)


class WNDCLASSEXW(C.Structure):
    _fields_ = [('cbSize', C.c_uint), ('style', C.c_uint),
                ('lpfnWndProc', WNDPROC), ('cbClsExtra', C.c_int),
                ('cbWndExtra', C.c_int), ('hInstance', C.c_void_p),
                ('hIcon', C.c_void_p), ('hCursor', C.c_void_p),
                ('hbrBackground', C.c_void_p), ('lpszMenuName', C.c_wchar_p),
                ('lpszClassName', C.c_wchar_p), ('hIconSm', C.c_void_p)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [('biSize', W.DWORD), ('biWidth', C.c_long), ('biHeight', C.c_long),
                ('biPlanes', W.WORD), ('biBitCount', W.WORD),
                ('biCompression', W.DWORD), ('biSizeImage', W.DWORD),
                ('biXPelsPerMeter', C.c_long), ('biYPelsPerMeter', C.c_long),
                ('biClrUsed', W.DWORD), ('biClrImportant', W.DWORD)]


class BITMAPINFO(C.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', W.DWORD * 3)]


class BLENDFUNCTION(C.Structure):
    _fields_ = [('BlendOp', C.c_ubyte), ('BlendFlags', C.c_ubyte),
                ('SourceConstantAlpha', C.c_ubyte), ('AlphaFormat', C.c_ubyte)]


class SIZE(C.Structure):
    _fields_ = [('cx', C.c_long), ('cy', C.c_long)]


class MSG(C.Structure):
    _fields_ = [('hwnd', W.HWND), ('message', C.c_uint), ('wParam', W.WPARAM),
                ('lParam', W.LPARAM), ('time', W.DWORD), ('pt', W.POINT),
                ('lPrivate', W.DWORD)]


g32 = C.WinDLL('gdi32', use_last_error=True)
u32.RegisterClassExW.argtypes = [C.POINTER(WNDCLASSEXW)]
u32.RegisterClassExW.restype = C.c_ushort
u32.CreateWindowExW.argtypes = [W.DWORD, C.c_wchar_p, C.c_wchar_p, W.DWORD,
                                C.c_int, C.c_int, C.c_int, C.c_int,
                                C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
u32.CreateWindowExW.restype = W.HWND
u32.DefWindowProcW.argtypes = [W.HWND, C.c_uint, W.WPARAM, W.LPARAM]
u32.DefWindowProcW.restype = C.c_ssize_t
u32.UpdateLayeredWindow.argtypes = [W.HWND, C.c_void_p, C.POINTER(W.POINT),
                                    C.POINTER(SIZE), C.c_void_p,
                                    C.POINTER(W.POINT), W.DWORD,
                                    C.POINTER(BLENDFUNCTION), W.DWORD]
u32.DestroyWindow.argtypes = [W.HWND]
u32.PeekMessageW.argtypes = [C.POINTER(MSG), W.HWND, C.c_uint, C.c_uint, C.c_uint]
u32.TranslateMessage.argtypes = [C.POINTER(MSG)]
u32.DispatchMessageW.argtypes = [C.POINTER(MSG)]
g32.CreateCompatibleDC.argtypes = [C.c_void_p]
g32.CreateCompatibleDC.restype = C.c_void_p
g32.CreateDIBSection.argtypes = [C.c_void_p, C.POINTER(BITMAPINFO), C.c_uint,
                                 C.POINTER(C.c_void_p), C.c_void_p, W.DWORD]
g32.CreateDIBSection.restype = C.c_void_p
g32.SelectObject.argtypes = [C.c_void_p, C.c_void_p]
g32.SelectObject.restype = C.c_void_p
g32.DeleteObject.argtypes = [C.c_void_p]
g32.DeleteDC.argtypes = [C.c_void_p]
k32.GetModuleHandleW.argtypes = [C.c_wchar_p]
k32.GetModuleHandleW.restype = C.c_void_p


class OverlayProbe:
    """WS_EX_LAYERED | TOPMOST | TRANSPARENT | NOACTIVATE 的置顶覆盖窗口。"""

    _wndproc = None
    _cls = 'LDOverlayProbeWnd'

    def __init__(self, x=120, y=120, w=420, h=130):
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (w, h), (255, 0, 255, 255))
        ImageDraw.Draw(img).text((16, h // 2 - 8), 'OVERLAY PROBE',
                                 fill=(255, 255, 255, 255))
        r, g, b, a = img.split()
        bgra = Image.merge('RGBA', (b, g, r, a)).tobytes()   # GDI 需要 BGRA

        if OverlayProbe._wndproc is None:
            @WNDPROC
            def _proc(hwnd, msg, wp, lp):
                return u32.DefWindowProcW(hwnd, msg, wp, lp)
            OverlayProbe._wndproc = _proc

        wc = WNDCLASSEXW()
        wc.cbSize = C.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = OverlayProbe._wndproc
        wc.hInstance = k32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls
        if not u32.RegisterClassExW(C.byref(wc)):
            err = C.get_last_error()
            if err != 1410:       # ERROR_CLASS_ALREADY_EXISTS
                say('  [!] RegisterClassExW 失败 %d' % err)

        ex = (0x00080000 | 0x00000008 | 0x00000020 |
              0x08000000 | 0x00000080)  # LAYERED|TOPMOST|TRANSPARENT|NOACTIVATE|TOOLWINDOW
        self.hwnd = u32.CreateWindowExW(ex, self._cls, 'LD Overlay Probe',
                                        0x80000000, x, y, w, h,
                                        None, None, None, None)
        if not self.hwnd:
            raise RuntimeError('CreateWindowExW 失败 %d' % C.get_last_error())
        u32.ShowWindow(self.hwnd, 5)

        hdc = g32.CreateCompatibleDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h       # 负高度 = top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = C.c_void_p()
        hbmp = g32.CreateDIBSection(hdc, C.byref(bmi), 0, C.byref(bits), None, 0)
        if not hbmp or not bits:
            raise RuntimeError('CreateDIBSection 失败 %d' % C.get_last_error())
        C.memmove(bits, bgra, len(bgra))
        old = g32.SelectObject(hdc, hbmp)
        pt, sz, src = W.POINT(x, y), SIZE(w, h), W.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, 1)   # AC_SRC_OVER / AC_SRC_ALPHA
        if not u32.UpdateLayeredWindow(self.hwnd, None, C.byref(pt), C.byref(sz),
                                       hdc, C.byref(src), 0, C.byref(blend), 2):
            say('  [!] UpdateLayeredWindow 失败 %d' % C.get_last_error())
        self._hdc, self._hbmp, self._old = hdc, hbmp, old
        self.box = (x, y, x + w, y + h)
        self.tick()

    def tick(self):
        msg = MSG()
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
        try:
            if self._old:
                g32.SelectObject(self._hdc, self._old)
            g32.DeleteObject(self._hbmp)
            g32.DeleteDC(self._hdc)
        except Exception:
            pass


def magenta_score(im, box):
    """统计覆盖区域内的纯洋红像素；>0 说明覆盖了游戏画面。"""
    from PIL import Image
    x0, y0, x1, y1 = box
    crop = im.convert('RGB').crop((max(0, x0), max(0, y0),
                                   min(im.width, x1), min(im.height, y1)))
    n = 0
    for r, g, b in crop.getdata():
        if r > 220 and g < 60 and b > 220:
            n += 1
    return n, crop.size[0] * crop.size[1]


def run():
    global LOG
    os.makedirs(OUT, exist_ok=True)
    LOG = io.open(os.path.join(OUT, 'probe.log'), 'w', encoding='utf-8')
    say('=' * 72)
    say('覆盖层只读实机探针 | 不换包 / 不写内存 / 不改 exe')
    prefix, idx, _ = build_prefix()
    say('前缀键 %d | 译文指纹 %d' % (len(prefix), len(idx)))

    env = dict(os.environ)
    env['SteamAppId'] = '626600'
    env['SteamGameId'] = '626600'
    p = subprocess.Popen([EXE], cwd=ROOT, env=env)
    say('已启动 pid=%d' % p.pid)
    h = None
    for _ in range(100):
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                            False, p.pid)
        if h:
            break
        time.sleep(0.05)
    if not h:
        say('[!] OpenProcess 失败')
        return 1

    win = None
    for _ in range(180):
        win = find_win(p.pid)
        if win and win[1] >= 800:
            break
        time.sleep(0.4)
    if not win:
        say('[!] 游戏窗口未出现')
        k32.CloseHandle(h)
        p.terminate()
        return 1
    hwnd, W_, H_, L_, T_ = win
    say('窗口 hwnd=%s %dx%d @(%d,%d)' % (hex(hwnd), W_, H_, L_, T_))
    time.sleep(3)

    ov = None
    allhits, shots = [], []
    try:
        ov = OverlayProbe(x=L_ + 120, y=T_ + 120)
        say('覆盖窗口已创建 box=%s' % (ov.box,))
        # 采样：先画面稳定，再按几次 Enter 推进（跳过 logo / 进菜单或剧情）
        marks = [0, 6, 12, 18, 24, 30]
        t0 = time.time()
        for k in range(1, 7):
            target = marks[k - 1]
            while time.time() - t0 < target:
                ov.tick()
                time.sleep(0.2)
            ov.tick()
            from PIL import ImageGrab
            im = ImageGrab.grab(all_screens=True)
            png = os.path.join(OUT, 'shot_%02d.png' % k)
            im.save(png)
            n, total = magenta_score(im, ov.box)
            say('[%02d] t=%4.1fs 覆盖可见像素 %6d / %6d  %s'
                % (k, time.time() - t0, n, total,
                   '可见 ✓' if n > 1000 else '被遮住 ✗'))
            shots.append({'k': k, 't': round(time.time() - t0, 1),
                          'magenta': n, 'total': total, 'png': png})
            regions, nbytes = read_regions(h)
            if not regions:
                say('     VirtualQueryEx 未取到区域，改用按页直读')
                regions, nbytes = read_pages_fallback(h)
            say('     内存 %d 块 / %.1f MB' % (len(regions), nbytes / 1048576))
            hit = []
            for base, data in regions:
                for r in scan_bytes(data, prefix, idx, limit=60):
                    hit.append({'base': hex(base + r['off']), 'ref': r['ref'],
                                'zh': r['zh'], 'n': r['n'], 'exact': r['exact']})
            allhits.append({'k': k, 'hits': hit})
            say('     命中码位串 %d 条' % len(hit))
            for r in hit[:4]:
                say('       %s %s | %s' % (r['base'], r['ref'], r['zh'][:36]))
            focus(hwnd)
            press(0x0D)   # Enter 推进
    finally:
        if ov:
            ov.close()
        json.dump({'shots': shots, 'hits': allhits},
                  io.open(os.path.join(OUT, 'result.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        try:
            u32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
            time.sleep(1.0)
            if p.poll() is None:
                k32.TerminateProcess(h, 0)
        except Exception as e:
            say('  退出时: %s' % e)
        k32.CloseHandle(h)
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        say('探针结束，游戏已退出；产物在 %s' % OUT)
        LOG.close()
    return 0


def wintest():
    """不启动游戏，只验证纯 Win32 分层窗口能否绘制并被屏幕采样到。"""
    from PIL import ImageGrab
    os.makedirs(OUT, exist_ok=True)
    ov = OverlayProbe(x=200, y=200)
    time.sleep(0.5)
    ov.tick()
    im = ImageGrab.grab(all_screens=True)
    png = os.path.join(OUT, 'wintest.png')
    im.save(png)
    n, total = magenta_score(im, ov.box)
    say('wintest: 洋红像素 %d / %d -> %s | 截图 %s'
        % (n, total, '窗口可见 ✓' if n > 1000 else '不可见 ✗', png))
    ov.close()
    return 0 if n > 1000 else 1


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'selftest'
    if mode == 'selftest':
        sys.exit(selftest())
    elif mode == 'wintest':
        sys.exit(wintest())
    else:
        sys.exit(run())
