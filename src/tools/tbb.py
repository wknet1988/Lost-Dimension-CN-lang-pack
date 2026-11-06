# -*- coding: utf-8 -*-
"""TBB1/TBB2 容器 + MTBL 子表的顺序解析器（用于按序号定位字符串、原地改写）。

实测结构
  TBB : 'TBB1'/'TBB2' u32 hdrsize(16) u32 tablecount u32 filesize
        随后 tablecount 个 u32 表偏移（自 0x10 起）
  MTBL: 'MTBL' u32 hdrsize(0x10) u32 payload u32 count
        u32 index[count]   ← 哈希/排序索引，**不是**字符串起点，忽略
        u32 ?              ← 一个额外 u32（首表见到 0x430 之类）
        u32 0
        pool ...           ← NUL 结尾 u16 串紧挨着排
  pool 长度 = payload - (count*4 + 8)

因此：字符串按序号定位，范围 = [start, next_start)，替换时可用到 next_start 前。
"""
import os, struct

MAGIC1 = b'TBB1'
MAGIC2 = b'TBB2'


def endian(d):
    if d[:4] == MAGIC1:
        return '<'
    if d[:4] == MAGIC2:
        return '>'
    raise ValueError('not a TBB: %r' % d[:4])


def tables(d):
    e = endian(d)
    hdrsize, tcount, fsize = struct.unpack_from(e + 'III', d, 4)
    out = []
    for k in range(tcount):
        j = struct.unpack_from(e + 'I', d, 0x10 + k * 4)[0]
        if d[j:j + 4] != b'MTBL':
            continue
        hdr, payload, cnt = struct.unpack_from(e + 'III', d, j + 4)
        pool = j + hdr + cnt * 4 + 8
        pool_end = j + hdr + payload
        if pool > len(d) or pool_end > len(d):
            continue
        out.append(dict(pos=j, hdr=hdr, payload=payload, count=cnt,
                        pool=pool, pool_end=pool_end))
    return out


def scan_pool(d, pool, pool_end, e):
    """顺序切分 pool，返回 [(start, next_start, codes)]。"""
    res = []
    p = pool
    while p + 1 < pool_end:
        start = p
        codes = []
        while p + 1 < pool_end:
            v = struct.unpack_from(e + 'H', d, p)[0]
            if v == 0:
                break
            codes.append(v)
            p += 2
        if p + 1 >= pool_end:
            break
        p += 2                       # 跳过 NUL
        res.append((start, p, codes))
    return res


def read_codes(d, off, e='<'):
    """从 off 读一条 NUL 结尾的 u16 串，返回 codes。"""
    codes = []
    p = off
    n = len(d)
    while p + 1 < n:
        v = struct.unpack_from(e + 'H', d, p)[0]
        if v == 0:
            break
        codes.append(v)
        p += 2
    return codes


def entries(d, t, e=None):
    """★ 正确枚举：index[i] 是**相对 MTBL+hdrsize 的偏移**，不是绝对偏移。

    绝对偏移 = t['pos'] + t['hdr'] + index[i]
    返回 [(i, abs_off, codes)]，i=0 是哨兵（指向索引数组自身，读出空串）。
    """
    e = e or endian(d)
    base = t['pos'] + t['hdr']
    out = []
    for i in range(t['count']):
        rel = struct.unpack_from(e + 'I', d, base + i * 4)[0]
        off = base + rel
        if off + 1 >= len(d):
            out.append((i, off, None))
            continue
        out.append((i, off, read_codes(d, off, e)))
    return out


def parse(path):
    d = open(path, 'rb').read()
    e = endian(d)
    out = []
    for t in tables(d):
        for idx, (s, n, codes) in enumerate(scan_pool(d, t['pool'], t['pool_end'], e)):
            out.append(dict(table=t, idx=idx, start=s, next=n, codes=codes))
    return out, d, e


def walk(root):
    res = []
    for dp, _, fns in os.walk(root):
        for f in sorted(fns):
            if f.lower().endswith(('.mbb', '.tbb')):
                res.append(os.path.join(dp, f))
    return sorted(res)


if __name__ == '__main__':
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else 'extracted/Data/Table'
    files = walk(root)
    nstr = nchar = 0
    bad = 0
    for p in files:
        try:
            ents, d, e = parse(p)
        except Exception as ex:
            bad += 1
            continue
        nstr += len(ents)
        nchar += sum(len(x['codes']) for x in ents)
    print('files=%d strings=%d codes=%d bad=%d' % (len(files), nstr, nchar, bad))
