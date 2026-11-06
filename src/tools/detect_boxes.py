#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从游戏画面里识别对白框：靠左右两根等高竖向蓝色饰条配对定位。

为什么不用"找完整描边"：实机对白框的上下边缘不是蓝色，蓝色只出现在
  - 顶部名字牌（实心蓝块，宽 ~0.26W，不满足饰条窄度条件，天然排除）
  - 框左右两端的圆角饰条（窄竖条，高度=框高，y 范围一致）
两根饰条的外沿即框的左右边界，饰条的 y 范围即框的上下边界。
上下双开模式 = 两对饰条（y 范围不同），按 y 分组自然支持。

纯 PIL 实现（运行环境无 numpy）：掩膜用 point()/ImageChops 在 C 层完成，
连通标记在下采样粗网格上做；只读画面，不写内存、不碰游戏文件。

校准基准（1920x1080 实机截图）：
  左饰条 (380,756)-(414,930)  右饰条 (1506,756)-(1542,930)
  -> 框 (380,756) 1162x174
  名字牌 (362,688)-(868,752)，按框比例推导（随框缩放）。
"""
import sys
from PIL import Image, ImageChops, ImageDraw

# ---- 蓝色谓词（可按实机再校准）------------------------------------------
B_MIN, BR_DIFF, G_MIN, R_MAX = 150, 45, 80, 130


def blue_mask(img):
    """返回 'L' 掩膜：蓝边像素=255。全部在 PIL C 层完成。"""
    r, g, b = img.convert('RGB').split()
    r1 = r.point(lambda v: 255 if v <= R_MAX else 0)
    g1 = g.point(lambda v: 255 if v >= G_MIN else 0)
    b1 = b.point(lambda v: 255 if v >= B_MIN else 0)
    d1 = ImageChops.subtract(b, r).point(lambda v: 255 if v >= BR_DIFF else 0)
    m = ImageChops.multiply(ImageChops.multiply(r1, g1),
                            ImageChops.multiply(b1, d1))
    return m


def coarse_cells(mask, stride):
    """下采样：BOX 平均后阈值，任何含蓝的 stride 方块都置 1。
    返回 (cells 列表按行展开, cw, ch)。"""
    W, H = mask.size
    cw, ch = max(1, (W + stride - 1) // stride), max(1, (H + stride - 1) // stride)
    small = mask.resize((cw, ch), Image.BOX).point(lambda v: 255 if v >= 30 else 0)
    return list(small.tobytes()), cw, ch


def label_components(cells, cw, ch):
    """8 连通分量，返回 [(像素数, minx, miny, maxx, maxy)]（格坐标）。"""
    labels = [0] * (cw * ch)
    comps = []
    for start in range(cw * ch):
        if not cells[start] or labels[start]:
            continue
        cid = len(comps) + 1
        stack = [start]
        labels[start] = cid
        n = 0
        minx = maxx = start % cw
        miny = maxy = start // cw
        while stack:
            cur = stack.pop()
            n += 1
            x, y = cur % cw, cur // cw
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < cw and 0 <= ny < ch:
                        idx = ny * cw + nx
                        if cells[idx] and not labels[idx]:
                            labels[idx] = cid
                            stack.append(idx)
        comps.append((n, minx, miny, maxx, maxy))
    return comps


def find_dialog_boxes(img, cfg=None, stride=None):
    """返回 (boxes, accents)。boxes=[(x,y,w,h)]（img 坐标，按 y 排序），
    accents=参与配对的饰条列表，供诊断。cfg 可提供 stride 等参数。"""
    cfg = cfg or {}
    stride = stride or cfg.get('stride', 3)
    W, H = img.size
    cells, cw, ch = coarse_cells(blue_mask(img), stride)
    comps = label_components(cells, cw, ch)

    min_h = H * cfg.get('min_accent_h_ratio', 0.05)
    max_w = W * cfg.get('max_accent_w_ratio', 0.06)
    min_aspect = cfg.get('min_accent_aspect', 1.0)

    acc = []
    for n, x0, y0, x1, y1 in comps:
        w, h = (x1 - x0 + 1) * stride, (y1 - y0 + 1) * stride
        if h >= min_h and w <= max_w and h / max(1, w) >= min_aspect:
            acc.append([x0 * stride, y0 * stride,
                        (x1 + 1) * stride, (y1 + 1) * stride])
    # 碎片合并：x 中心接近且 y 相邻的竖条合并成完整饰条
    merged = []
    for a in acc:
        ax = (a[0] + a[2]) / 2
        for m in merged:
            mx = (m[0] + m[2]) / 2
            if abs(ax - mx) <= W * 0.02 and not (a[1] > m[3] + H * 0.02
                                                 or a[3] < m[1] - H * 0.02):
                m[0] = min(m[0], a[0]); m[1] = min(m[1], a[1])
                m[2] = max(m[2], a[2]); m[3] = max(m[3], a[3])
                break
        else:
            merged.append(list(a))
    # 只保留够高的完整饰条
    accents = [m for m in merged if m[3] - m[1] >= H * cfg.get('min_box_h_ratio', 0.10)]

    # 按 y 中心分组，同组内取最左+最右配成框
    groups = {}
    for a in accents:
        cy = (a[1] + a[3]) / 2
        key = None
        for k in groups:
            if abs(cy - k) <= H * cfg.get('pair_y_tol_ratio', 0.03):
                key = k
                break
        if key is None:
            key = cy
            groups[key] = []
        groups[key].append(a)

    min_box_w = W * cfg.get('min_box_w_ratio', 0.25)
    boxes = []
    for key, grp in sorted(groups.items()):
        if len(grp) < 2:
            continue
        # 高度相近才配对（原生两根饰条等高）
        grp.sort(key=lambda m: m[3] - m[1])
        ref_h = grp[len(grp) // 2][3] - grp[len(grp) // 2][1]
        grp = [m for m in grp if abs((m[3] - m[1]) - ref_h) <= ref_h * 0.4]
        if len(grp) < 2:
            continue
        grp.sort(key=lambda m: m[0])
        x0, y0 = grp[0][0], min(m[1] for m in grp)
        x1, y1 = grp[-1][2], max(m[3] for m in grp)
        if x1 - x0 >= min_box_w:
            boxes.append((x0, y0, x1 - x0, y1 - y0))
    boxes.sort(key=lambda b: b[1])
    return boxes, accents


def find_nameplate(img, box, cfg=None):
    """在框正上方找**原生蓝色名牌**的实心块，返回 (x,y,w,h)（img 坐标）。
    名牌是实心蓝块，宽 ~0.44W、高 ~0.37H(框高)，因太宽而不满足饰条窄度条件，
    在 find_dialog_boxes 里被排除——这里专门把它捞出来，用于精确覆盖。
    找不到（无名牌 / 颜色不符 / 尺寸异常）时返回 None，调用方回落到比例推导。"""
    cfg = cfg or {}
    s = cfg.get('nameplate_search', {})
    x, y, w, h = box
    W, H = img.size
    # 只取框顶以上的一条带：下界卡在框顶上方一点，避免把左右饰条并进同一个连通块
    top = max(0, int(y - h * s.get('up', 0.95)))
    bottom = min(H, int(y - max(1, h * s.get('gap', 0.008))))
    left = max(0, int(x - w * s.get('left', 0.06)))
    right = min(W, int(x + w * s.get('right', 0.60)))
    rw, rh = right - left, bottom - top
    if rw < 80 or rh < 16:
        return None
    data = blue_mask(img.crop((left, top, right, bottom))).tobytes()
    # 1) 行投影：名牌是「一整条横向密布蓝」的带，饰条/装饰只有窄窄一列
    rows = [data[i * rw:(i + 1) * rw].count(255) for i in range(rh)]
    thr = max(12, int(rw * s.get('row_ratio', 0.10)))
    best = None
    i = 0
    while i < rh:
        if rows[i] >= thr:
            j = i
            while j + 1 < rh and rows[j + 1] >= thr:
                j += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j + 1
        else:
            i += 1
    if best is None:
        return None
    y0, y1 = best
    # 1b) 上下扩展：名牌有渐隐边，核心带之外还有蓝像素，先吃掉再量尺寸
    low = max(3, int(rw * s.get('row_low_ratio', 0.04)))
    while y0 - 1 >= 0 and rows[y0 - 1] >= low:
        y0 -= 1
    while y1 + 1 < rh and rows[y1 + 1] >= low:
        y1 += 1
    # 2) 列投影：在该行带里取蓝像素占多数的列（跳过牌内白字的空洞）
    need = max(2, int((y1 - y0 + 1) * s.get('col_ratio', 0.30)))
    cols = [0] * rw
    for yy in range(y0, y1 + 1):
        base = yy * rw
        for xx in range(rw):
            if data[base + xx] == 255:
                cols[xx] += 1
    xs = [k for k, c in enumerate(cols) if c >= need]
    if not xs:
        return None
    nx0, nx1 = xs[0], xs[-1]
    bw, bh = nx1 - nx0 + 1, y1 - y0 + 1
    if bw < w * 0.12 or bh < h * 0.10 or bh > h * 0.85 or bw < bh:
        return None
    return (left + nx0, top + y0, bw, bh)


def nameplate_from_box(box, cfg=None):
    """按校准比例从对白框推导名字牌矩形（随框缩放）。"""
    cfg = cfg or {}
    np = cfg.get('nameplate_from_box', {})
    x, y, w, h = box
    dx = np.get('dx', -0.016) * w
    dy = np.get('dy', -0.39) * h
    return (int(x + dx), int(y + dy),
            int(w * np.get('w', 0.436)), int(h * np.get('h', 0.368)))


def panel_from_box(box, cfg=None):
    """框 -> 面板矩形：四周内缩，让原生蓝饰条/框线露出，面板盖住文字区。"""
    cfg = cfg or {}
    ix = cfg.get('panel_inset_x', 0.008)
    iy = cfg.get('panel_inset_y', 0.035)
    x, y, w, h = box
    px, py = int(w * ix), int(h * iy)
    return (x + px, y + py, w - 2 * px, h - 2 * py)


def annotate(img, boxes, path):
    ann = img.convert('RGB').copy()
    d = ImageDraw.Draw(ann)
    for i, (x, y, w, h) in enumerate(boxes):
        d.rectangle([x, y, x + w, y + h], outline=(255, 40, 40), width=3)
        d.text((x + 6, y + 4), 'box%d' % i, fill=(255, 255, 0))
        nx, ny, nw, nh = nameplate_from_box((x, y, w, h))
        d.rectangle([nx, ny, nx + nw, ny + nh], outline=(0, 255, 0), width=2)
        d.text((nx + 4, ny + 2), 'name', fill=(0, 255, 0))
        pb = panel_from_box((x, y, w, h))
        d.rectangle([pb[0], pb[1], pb[0] + pb[2], pb[1] + pb[3]],
                    outline=(255, 255, 0), width=1)
    ann.save(path)
    return path


def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument('image')
    ap.add_argument('--stride', type=int, default=3)
    ap.add_argument('--save', default='')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    img = Image.open(a.image).convert('RGB')
    W, H = img.size
    boxes, accents = find_dialog_boxes(img, {'stride': a.stride})
    if a.json:
        print(json.dumps({'size': [W, H], 'boxes': boxes,
                          'accents': accents}, ensure_ascii=False))
        return
    print('图片 %dx%d，识别到 %d 个对白框（饰条 %d 根）：'
          % (W, H, len(boxes), len(accents)))
    for i, (x, y, w, h) in enumerate(boxes):
        print('  框%d: (%d,%d) %dx%d | x=%.3f y=%.3f w=%.3f h=%.3f'
              % (i, x, y, w, h, x / W, y / H, w / W, h / H))
        print('        名字牌 %s  面板 %s' % (nameplate_from_box((x, y, w, h)),
                                            panel_from_box((x, y, w, h))))
    if a.save:
        annotate(img, boxes, a.save)
        print('标注图 %s' % a.save)


if __name__ == '__main__':
    main()
