# -*- coding: utf-8 -*-
"""屏幕文本识别（画面上的英文 -> 查中文），供浮层覆盖选单/说明/注释用。

原理
----
游戏 UI 文本是用 Font18/Font22 位图图集渲染的，再整帧缩放到窗口尺寸。
所以把画面上每个字形的包围盒归一化后，和图集字形在同一倍率下的形态比对，
就能逐字认出英文；再用全库英文原串做词表纠错与整句匹配，拿到中文译文。

对外接口
--------
    ocr = ScreenOCR()
    runs = ocr.scan(rgb_array)      # [(box, en, zh, conf, ref)]
    ocr.annotate(rgb_array)         # 调试用，返回画了框和文字的图

纯只读：只读 work/ps3map/screen_text_index.json 与字体图集，不碰游戏文件。
"""
import difflib
import io
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import render_native as R           # noqa: E402

IDX = os.path.join(WORK, 'ps3map', 'screen_text_index.json')

GRID = 32
CHARS = [chr(c) for c in range(33, 127)]
FONTS = ('Font18', 'Font22')
XHEIGHT = {'Font18': 10.0, 'Font22': 12.0}      # 图集里小写字高，用来反推倍率

# 视觉上容易混的字（小字 + 抗锯齿 + 缩放），纠错时按半价算
CONFUSE = [
    set('il1|I'),
    set('o0O'),
    set('cC'),
    set('sS'),
    set('vV'),
    set('wW'),
    set('xX'),
    set('zZ'),
    set('pP'),
    set('uU'),
    set('nN'),
    set('kK'),
    set('jJ'),
    set('yY'),
    set('.,'),
    set("'\""),
    set('-~'),
]
_CONF = {}
for g in CONFUSE:
    for a in g:
        for b in g:
            if a != b:
                _CONF[(a, b)] = 0.25
_ALT = {}
for g in CONFUSE:
    for a in g:
        _ALT[a] = tuple(sorted(g))


def glyph_gray(ch, font):
    code = ord(ch) + 201
    g = R.glyph(font, code)
    if g is None:
        return None
    return np.asarray(g.convert('L')).astype(np.float32) / 255.0


_RM = {}


def _resample(sub, gh=GRID, gw=GRID):
    """双线性放大到 gh×gw（缓存重采样矩阵，比 PIL 逐字 resize 快一个量级）。"""
    h, w = sub.shape
    key = (h, w)
    R, C = _RM.get(key, (None, None))
    if R is None:
        def mat(n_in, n_out):
            pos = (np.arange(n_out) + 0.5) * (n_in / float(n_out)) - 0.5
            i0 = np.floor(pos).astype(int)
            fr = pos - i0
            i0 = np.clip(i0, 0, n_in - 1)
            i1 = np.clip(i0 + 1, 0, n_in - 1)
            M = np.zeros((n_out, n_in), np.float32)
            M[np.arange(n_out), i0] += (1 - fr)
            M[np.arange(n_out), i1] += fr
            return M
        R, C = mat(h, gh), mat(w, gw)
        _RM[key] = (R, C)
    return R @ sub @ C.T


def norm_patch(patch):
    ys, xs = np.where(patch > 0.10)
    if len(ys) == 0:
        return None, 0, 0
    h = int(ys.max() - ys.min() + 1)
    w = int(xs.max() - xs.min() + 1)
    sub = patch[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    v = _resample(np.clip(sub, 0, 1)).ravel()
    n = np.linalg.norm(v)
    if n < 1e-6:
        return None, 0, 0
    return v / n, h, w


class _Tmpl(object):
    """某个 (字体, 倍率) 下的全部字形模板，做成矩阵以便一次矩阵乘算出全部得分。"""

    def __init__(self, font, scale):
        chars, vecs, hs, ws = [], [], [], []
        for ch in CHARS:
            g = glyph_gray(ch, font)
            if g is None:
                continue
            if abs(scale - 1.0) > 1e-3:
                hh, ww = g.shape
                g = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize(
                    (max(1, int(round(ww * scale))), max(1, int(round(hh * scale)))),
                    Image.BILINEAR)).astype(np.float32) / 255.0
            v, h, w = norm_patch(g)
            if v is None:
                continue
            chars.append(ch)
            vecs.append(v)
            hs.append(h)
            ws.append(w)
        self.font = font
        self.scale = scale
        self.chars = chars
        self.M = np.array(vecs, np.float32)
        self.h = np.array(hs, np.float32)
        self.w = np.array(ws, np.float32)

    def score(self, v, h, w, size_w=0.55):
        s = self.M @ v
        pen = size_w * (np.abs(np.log(max(h, 1) / self.h)) + np.abs(np.log(max(w, 1) / self.w)))
        return s - pen


class ScreenOCR(object):
    def __init__(self, index_path=IDX, verbose=False):
        self.verbose = verbose
        d = json.load(io.open(index_path, encoding='utf-8'))
        self.entries = d['entries']
        self.words = d['words']
        self._tm = {}
        self.en_norm = [self._norm(e['en']) for e in self.entries]
        self._xl = []
        for i, e in enumerate(self.entries):
            for w in set(self._norm(e['en']).split()):
                self._xl.append((w, i))
        # 词频：用于挑「最有辨识度」的词做召回
        self._wf = {}
        for w, ids in self.words.items():
            self._wf[w] = len(ids)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _norm(s):
        s = s.replace('\n', ' ').lower()
        s = s.replace('\u2019', "'").replace('\u2018', "'")
        s = s.replace('\u201c', '"').replace('\u201d', '"')
        out = []
        for ch in s:
            out.append(ch if (ch.isalnum() or ch in "' -") else ' ')
        return ' '.join(''.join(out).split())

    def tmpl(self, font, scale):
        key = (font, round(scale * 20) / 20.0)
        if key not in self._tm:
            self._tm[key] = _Tmpl(font, key[1])
        return self._tm[key]

    # ---------------------------------------------------------------- 行检测
    @staticmethod
    def text_mask(gray):
        """文字 = 比周围背景亮。用积分图做局部背景估计，亮底暗字也兼容（取 abs）。"""
        h, w = gray.shape
        r = 12
        ii = gray.cumsum(0).cumsum(1)
        ii = np.pad(ii, ((1, 0), (1, 0)))
        y0 = np.clip(np.arange(h) - r, 0, h)
        y1 = np.clip(np.arange(h) + r + 1, 0, h)
        x0 = np.clip(np.arange(w) - r, 0, w)
        x1 = np.clip(np.arange(w) + r + 1, 0, w)
        A = ii[np.ix_(y1, x1)] - ii[np.ix_(y0, x1)] - ii[np.ix_(y1, x0)] + ii[np.ix_(y0, x0)]
        cnt = ((y1 - y0)[:, None] * (x1 - x0)[None, :]).astype(np.float32)
        bg = A / np.maximum(cnt, 1)
        delta = gray - bg
        m = (delta > 0.16) & (gray > 0.42)
        return m

    @staticmethod
    def _runs(mask):
        """按行做游程编码 + 并查集，得到连通域。返回 [x0,y0,x1,y1,area]"""
        h, w = mask.shape
        parent = []

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        boxes = []
        prev = []
        for y in range(h):
            row = mask[y]
            if not row.any():
                prev = []
                continue
            d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
            starts = np.where(d == 1)[0]
            ends = np.where(d == -1)[0] - 1
            cur = []
            for s, e in zip(starts, ends):
                nid = len(parent)
                parent.append(nid)
                boxes.append([s, y, e, y])
                for ps, pe, pid in prev:
                    if ps <= e + 1 and pe >= s - 1:
                        union(pid, nid)
                cur.append((s, e, nid))
            prev = cur
        groups = {}
        for i, b in enumerate(boxes):
            r = find(i)
            g = groups.get(r)
            if g is None:
                groups[r] = [b[0], b[1], b[2], b[3], 1]
            else:
                g[0] = min(g[0], b[0]); g[1] = min(g[1], b[1])
                g[2] = max(g[2], b[2]); g[3] = max(g[3], b[3]); g[4] += 1
        return [g for g in groups.values() if g[4] >= 6]

    def detect_lines(self, mask, min_h=8, max_h=72, min_w=10):
        """按基线聚成行，再按大间距切成「文本段」。

        不靠横向膨胀（膨胀量对空格宽度很敏感），而是先把字块按竖直中心聚类成行，
        同一行内相邻字块的间距超过 2.5 倍字高才断开——这样列表里每一栏各自成段。
        """
        comps = [c for c in self._runs(mask)
                 if c[4] >= 3 and min_h <= (c[3] - c[1] + 1) <= max_h]
        if not comps:
            return []
        # 画面里人物衣服/高光也是亮的，会被当成「字块」。文字高度会聚成一个尖峰，
        # 取高度直方图的众数当字高，再把明显不符的（背景大块、噪点）剔掉。
        hh = np.array([c[3] - c[1] + 1 for c in comps])
        bins = np.bincount(hh)
        xh = float(int(np.argmax(bins[2:]))) + 2.0
        comps = [c for c in comps
                 if 0.45 * xh <= (c[3] - c[1] + 1) <= 2.4 * xh]
        if not comps:
            return []
        comps.sort(key=lambda c: (c[1] + c[3]) / 2.0)
        rows = []
        for c in comps:
            mid = (c[1] + c[3]) / 2.0
            placed = False
            for r in rows:
                if abs(r['mid'] - mid) <= xh * 0.55:
                    r['items'].append(c)
                    r['mid'] = (r['mid'] * (len(r['items']) - 1) + mid) / len(r['items'])
                    placed = True
                    break
            if not placed:
                rows.append(dict(mid=mid, items=[c]))
        out = []
        for r in rows:
            items = sorted(r['items'], key=lambda c: c[0])
            y0 = min(c[1] for c in items)
            y1 = max(c[3] for c in items)
            if (y1 - y0 + 1) > max_h:
                continue
            seg = [items[0]]
            for c in items[1:]:
                if c[0] - max(x[2] for x in seg) > xh * 2.5:
                    out.append((seg[0][0], min(x[1] for x in seg),
                                max(x[2] for x in seg), max(x[3] for x in seg)))
                    seg = [c]
                else:
                    seg.append(c)
            out.append((seg[0][0], min(x[1] for x in seg),
                        max(x[2] for x in seg), max(x[3] for x in seg)))
        out = [b for b in out if (b[2] - b[0] + 1) >= min_w]
        out.sort(key=lambda b: (b[1] // 8, b[0]))
        return out

    # ---------------------------------------------------------------- 逐字识别
    def read(self, gray, box, font=None, scale=None):
        x0, y0, x1, y1 = box
        sub = gray[max(0, y0 - 2):y1 + 3, max(0, x0 - 2):x1 + 3]
        oy, ox = max(0, y0 - 2), max(0, x0 - 2)
        mask = sub > 0.45
        comps = self._runs(mask)
        comps = [c for c in comps if c[4] >= 3]
        if not comps:
            return [], None, 0.0
        hs = [c[3] - c[1] + 1 for c in comps]
        xh = float(np.median(hs))
        cands = []
        for f in FONTS:
            s = xh / XHEIGHT[f]
            if 0.85 <= s <= 2.6:
                cands.append((f, s))
        if font and scale:
            cands = [(font, scale)]
        if not cands:
            return [], None, 0.0
        best = None
        for f, s in cands:
            comps2 = self._merge_dots(comps, xh)
            tm = self.tmpl(f, s)
            out, tot = [], 0.0
            for bx0, by0, bx1, by1, _ in comps2:
                patch = sub[max(0, by0 - 1):by1 + 2, max(0, bx0 - 1):bx1 + 2]
                v, h, w = norm_patch(patch)
                if v is None:
                    continue
                sc = tm.score(v, h, w)
                i = int(np.argmax(sc))
                out.append((bx0 + ox, by0 + oy, bx1 + ox, by1 + oy, tm.chars[i], float(sc[i])))
                tot += float(sc[i])
            out.sort(key=lambda r: r[0])
            mean = tot / max(1, len(out))
            if best is None or mean > best[2]:
                best = (out, (f, s), mean)
        return best

    @staticmethod
    def _merge_dots(comps, xh):
        comps = [list(c) for c in comps]
        changed = True
        while changed:
            changed = False
            for i in range(len(comps)):
                if comps[i] is None:
                    continue
                for j in range(len(comps)):
                    if i == j or comps[j] is None:
                        continue
                    a, b = comps[i], comps[j]
                    ox = min(a[2], b[2]) - max(a[0], b[0]) + 1
                    if ox <= 0:
                        continue
                    gap = max(b[1] - a[3], a[1] - b[3])
                    if gap < 0 or gap > max(3, xh * 0.7):
                        continue
                    aa = (a[2] - a[0] + 1) * (a[3] - a[1] + 1)
                    bb = (b[2] - b[0] + 1) * (b[3] - b[1] + 1)
                    small, big = (a, b) if aa < bb else (b, a)
                    if (small[2] - small[0] + 1) > 0.8 * (big[2] - big[0] + 1):
                        continue
                    if (small[3] - small[1] + 1) > 0.85 * xh and (small[2] - small[0] + 1) > 0.85 * xh:
                        continue
                    comps[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]),
                                max(a[3], b[3]), a[4] + b[4]]
                    comps[j] = None
                    changed = True
                    break
                if changed:
                    break
        return [c for c in comps if c]

    @staticmethod
    def to_text(glyphs, xh):
        s, prev = '', None
        for (bx0, by0, bx1, by1, ch, sc) in glyphs:
            if prev is not None and bx0 - prev > xh * 0.55:
                s += ' '
            s += ch
            prev = bx1
        return s

    # ---------------------------------------------------------------- 纠错 / 查表
    @staticmethod
    def _wdist(a, b, limit=3.0, cutoff=None):
        la, lb = len(a), len(b)
        if abs(la - lb) > limit:
            return limit + 1
        prev = [float(j) for j in range(lb + 1)]
        for i in range(1, la + 1):
            ca = a[i - 1]
            cur = [prev[0] + 1.0] + [0.0] * lb
            best = cur[0]
            for j in range(1, lb + 1):
                cb = b[j - 1]
                sub = 0.0 if ca == cb else _CONF.get((ca, cb), 1.0)
                v = prev[j] + 1.0
                x = cur[j - 1] + 1.0
                if x < v:
                    v = x
                x = prev[j - 1] + sub
                if x < v:
                    v = x
                cur[j] = v
                if v < best:
                    best = v
            if cutoff is not None and best > cutoff:
                return cutoff + 1.0
            prev = cur
        return prev[lb]

    def fix_word(self, w, limit=1.6):
        """把 OCR 出的词纠正到词表里的真实单词。

        不逐个比对词表（一万多词太慢），而是把「易混字集合」做笛卡尔展开，
        直接查词表；命中就取词频最高的那个。
        """
        wl = w.lower()
        if wl in self.words:
            return wl, 0.0
        groups = []
        for ch in wl:
            alts = _ALT.get(ch, (ch,))
            groups.append(alts)
        n = 1
        for g in groups:
            n *= len(g)
        if n > 4096:
            return wl, 99.0
        best, bf = wl, -1
        idx = [0] * len(groups)

        def walk(i, buf):
            nonlocal best, bf
            if i == len(groups):
                cand = ''.join(buf)
                if cand in self.words:
                    f = self._wf.get(cand, 0)
                    if f > bf:
                        bf, best = f, cand
                return
            for ch in groups[i]:
                buf.append(ch)
                walk(i + 1, buf)
                buf.pop()

        walk(0, [])
        if bf >= 0:
            return best, 0.3
        return wl, 99.0

    def match(self, text, max_cand=60, max_tok_dist=0.65):
        """整串匹配：返回 (entry, score)，score 越小越可信。

        候选召回看「可靠的词」（词表纠正距离小的）在哪些条目里出现得多，
        再对召回的少数条目算整串编辑距离；截断的行（画面只显示前半句）按前缀对齐。
        """
        k = self._norm(text)
        if not k:
            return None, 9.9
        toks = k.split()
        fixed = []
        for t in toks:
            w, d = self.fix_word(t)
            fixed.append((w, d))
        target = ' '.join(w for w, _ in fixed)
        lt = len(target)

        hits = {}
        for w, d in fixed:
            if d > max_tok_dist:
                continue
            ids = self.words.get(w)
            if not ids:
                continue
            wt = 1.0 / (1.0 + np.log(len(ids)))
            for i in ids:
                hits[i] = hits.get(i, 0.0) + wt
        if hits:
            top = sorted(hits.items(), key=lambda kv: -kv[1])[:max_cand]
            pool = [i for i, _ in top]
        else:
            pool = [i for i in range(len(self.entries))
                    if 0.5 * lt <= self.entries[i]['n'] <= 1.6 * lt][:max_cand]

        best, bs = None, 9.9
        for i in pool:
            e = self.entries[i]
            ce = self.en_norm[i]
            L = max(lt, len(ce), 1)
            d = self._wdist(target, ce, float(L), cutoff=bs * L) / L
            if len(ce) > lt and lt >= 5 and d > 0.11:
                # 说明文字换行后每行只是整句的一段：先用最长公共块估出偏移，
                # 再取等长窗口算距离（比整句比对划算得多）。
                sm = difflib.SequenceMatcher(None, target, ce, autojunk=False)
                blk = sm.find_longest_match(0, lt, 0, len(ce))
                if blk.size >= 6:
                    start = max(0, min(len(ce) - lt, blk.b - blk.a))
                    win = ce[start:start + lt + 2]
                    dp = self._wdist(target, win, float(lt + 2), cutoff=bs * max(1, lt)) / max(1, lt)
                    d = min(d, dp)
            if d < bs:
                bs, best = d, e
        return best, bs

    # ---------------------------------------------------------------- 总入口
    def scan(self, rgb, min_score=0.55):
        """整屏识别：行 -> 块 -> 查中文。

        说明/注释类文字会换行，单看一行只是整句的一段，很容易匹配到别的条目。
        所以先按「左对齐 + 上下紧挨」把行聚成**文本块**，用整块文字去查表，
        再把整块一起盖掉——这样译文完整、盖板也不会只盖住半句。
        """
        gray = (np.asarray(rgb.convert('L') if hasattr(rgb, 'convert') else rgb)
                .astype(np.float32) / 255.0)
        mask = self.text_mask(gray)
        lines = self.detect_lines(mask)

        items = []
        for box in lines:
            if (box[3] - box[1] + 1) < 11:       # 太矮：界面装饰点，不是字
                continue
            glyphs, fs, mean = self.read(gray, box)
            if not glyphs or mean < min_score:
                continue
            xh = float(np.median([g[3] - g[1] + 1 for g in glyphs]))
            txt = self.to_text(glyphs, xh)
            if len(txt.replace(' ', '')) < 3 or xh < 9:
                continue
            items.append(dict(box=tuple(int(v) for v in box), raw=txt, xh=xh, cons=mean))

        # ---- 聚块：左边缘对齐、上下间距小于一行 ----
        items.sort(key=lambda r: (r['box'][1], r['box'][0]))
        blocks = []
        for it in items:
            for b in blocks:
                dx = abs(it['box'][0] - b['box'][0])
                gap = it['box'][1] - b['box'][3]
                if dx <= max(34, it['xh'] * 3) and -8 <= gap <= max(6, it['xh'] * 0.95):
                    b['items'].append(it)
                    b['box'] = (min(b['box'][0], it['box'][0]), b['box'][1],
                                max(b['box'][2], it['box'][2]), it['box'][3])
                    break
            else:
                blocks.append(dict(items=[it], box=it['box']))

        out = []
        for b in blocks:
            parts = sorted(b['items'], key=lambda r: r['box'][1])
            raw = ' '.join(p['raw'] for p in parts)
            xh = max(p['xh'] for p in parts)
            box = b['box']
            box = (min(p['box'][0] for p in parts), min(p['box'][1] for p in parts),
                   max(p['box'][2] for p in parts), max(p['box'][3] for p in parts))
            e, d = self.match(raw)
            nl = max(1, int(round((box[3] - box[1] + 1) / max(8.0, xh * 1.45))))
            out.append(dict(box=box, raw=raw, xh=xh, lines=nl,
                            cons=min(p['cons'] for p in parts),
                            en=(e['en'] if e else ''), zh=(e['zh'] if e else ''),
                            ref=(e.get('ref', '') if e else ''), dist=d))
        out.sort(key=lambda r: (r['box'][1], r['box'][0]))
        return out

    # ---------------------------------------------------------------- 调试
    def annotate(self, rgb, runs=None):
        img = rgb.convert('RGB').copy()
        runs = self.scan(img) if runs is None else runs
        dr = ImageDraw.Draw(img)
        try:
            f = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 20)
        except Exception:
            f = None
        for r in runs:
            x0, y0, x1, y1 = r['box']
            col = (80, 220, 120) if r['dist'] < 0.25 else (255, 190, 60)
            dr.rectangle([x0, y0, x1, y1], outline=col, width=2)
            dr.text((x0, max(0, y0 - 22)), '%s |%s' % (r['raw'][:38], (r['zh'] or '')[:14]),
                    font=f, fill=col)
        return img


def main():
    path = sys.argv[1]
    rgb = Image.open(path).convert('RGB')
    ocr = ScreenOCR()
    runs = ocr.scan(rgb)
    print('识别 %d 行' % len(runs))
    for r in runs:
        print('  %-4s box=%-22s 认成=%-40r 匹配=%r 距离=%.3f 中文=%r' %
              ('OK' if r['dist'] < 0.25 else '??', str(r['box']), r['raw'][:40],
               (r['en'] or '')[:40], r['dist'], (r['zh'] or '')[:20]))
    out = os.path.join(WORK, 'texprobe', 'ocr_' + os.path.basename(path))
    ocr.annotate(rgb, runs).save(out)
    print('标注图:', out)


if __name__ == '__main__':
    main()
