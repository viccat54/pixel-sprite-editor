#!/usr/bin/env python3
"""像素修圖台的指令工具：讓 agent 讀、畫、預覽 .pixelfixer.json 專案。

規則：agent 不直接改專案正本。所有繪圖都寫進「提案」，
由使用者看過後 accept（可只接受部分格子）才寫回專案。

專案檔旁邊會有 <名稱>.pfdata/ 資料夾：
  proposals/<id>.json   提案（每個像素：圖層、座標、原色、新色）
  history/*.pixelfixer.json  接受提案前的快照（每個專案最多 30 份）

需要 Python 3.9+ 與 Pillow。用 `pixelfixer.py <指令> -h` 看各指令說明，
完整用法見 repo 根目錄的 AGENT.md。
"""

import argparse
import base64
import io
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

PROJECT_SUFFIX = '.pixelfixer.json'
PROPOSAL_FORMAT = 'pixelfixer-proposal/1'
MAX_SNAPSHOTS = 30
TRANSPARENT = (0, 0, 0, 0)
# dump／grid 用的圖例字元（'.' 固定是透明，空白和 '?' 是「不改」）
LEGEND_CHARS = ('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                '!#$%&*+-/:;<=>@^_~')


class ToolError(Exception):
    pass


# ---------- 顏色 ----------

def parse_color(s):
    if s is None:
        raise ToolError('缺少顏色')
    t = str(s).strip().lower()
    if t in ('transparent', 'clear', 'none', '.'):
        return TRANSPARENT
    if t.startswith('#'):
        t = t[1:]
    if re.fullmatch(r'[0-9a-f]{3}', t):
        t = ''.join(c * 2 for c in t)
    if re.fullmatch(r'[0-9a-f]{6}', t):
        t += 'ff'
    if not re.fullmatch(r'[0-9a-f]{8}', t):
        raise ToolError('看不懂的顏色：%s（用 #rrggbb、#rrggbbaa 或 transparent）' % s)
    rgba = tuple(int(t[i:i + 2], 16) for i in (0, 2, 4, 6))
    return TRANSPARENT if rgba[3] == 0 else rgba


def norm(px):
    return TRANSPARENT if px[3] == 0 else tuple(px)


def fmt_color(px):
    px = norm(px)
    if px[3] == 0:
        return 'transparent'
    if px[3] == 255:
        return '#%02x%02x%02x' % px[:3]
    return '#%02x%02x%02x%02x' % px


# ---------- 專案 ----------

def decode_data_url(url):
    if not url:
        return None
    m = re.match(r'data:image/[a-z+]+;base64,(.*)$', url, re.S)
    if not m:
        raise ToolError('圖片資料格式不對')
    img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
    return img.convert('RGBA')


def encode_data_url(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


class Layer(object):
    def __init__(self, name, img, visible=True, locked=False):
        self.name = name
        self.img = img
        self.visible = visible
        self.locked = locked


class Project(object):
    def __init__(self, path):
        self.path = path
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except (OSError, ValueError) as e:
            raise ToolError('讀不到專案：%s（%s）' % (path, e))
        if not self.data.get('pxDataUrl'):
            raise ToolError('不是像素修圖台的專案檔：%s' % path)
        d = self.data
        self.name = d.get('name') or '未命名'
        self.cell = max(4, min(512, int(d.get('cell') or 32)))
        self.version = int(d.get('version') or 0)
        self.hi = decode_data_url(d.get('hiDataUrl'))
        self.base = decode_data_url(d.get('baseDataUrl'))
        px = decode_data_url(d['pxDataUrl'])
        self.layers = []
        for i, l in enumerate(d.get('layers') or []):
            self.layers.append(Layer(l.get('name') or ('圖層 %d' % (i + 1)),
                                     decode_data_url(l.get('dataUrl')),
                                     l.get('visible', True) is not False,
                                     bool(l.get('locked'))))
        if not self.layers or any(l.img is None or l.img.size != px.size for l in self.layers):
            self.layers = [Layer('底圖', px)]
        self.width, self.height = px.size

    @property
    def cols(self):
        return max(1, self.width // self.cell)

    @property
    def rows(self):
        return max(1, self.height // self.cell)

    @property
    def data_dir(self):
        p = self.path
        stem = p[:-len(PROJECT_SUFFIX)] if p.endswith(PROJECT_SUFFIX) else os.path.splitext(p)[0]
        return stem + '.pfdata'

    def layer_index(self, ref):
        """圖層可用編號（0＝最底層）或名稱。"""
        if ref is None:
            raise ToolError('要指定圖層（編號或名稱），用 info 看有哪些圖層')
        if isinstance(ref, int) or re.fullmatch(r'-?\d+', str(ref)):
            i = int(ref)
            if i < 0:
                i += len(self.layers)
            if 0 <= i < len(self.layers):
                return i
            raise ToolError('沒有第 %s 層（共 %d 層）' % (ref, len(self.layers)))
        for i, l in enumerate(self.layers):
            if l.name == ref:
                return i
        raise ToolError('沒有叫「%s」的圖層' % ref)

    def composite(self, imgs=None):
        imgs = imgs or [l.img for l in self.layers]
        out = Image.new('RGBA', (self.width, self.height), TRANSPARENT)
        for l, img in zip(self.layers, imgs):
            if l.visible:
                out.alpha_composite(img)
        return out

    def colors_in_use(self):
        used = set()
        for l in self.layers:
            for c in (l.img.getcolors(self.width * self.height) or []):
                used.add(norm(c[1]))
        for h in self.data.get('recents') or []:
            try:
                used.add(parse_color(h))
            except ToolError:
                pass
        used.discard(TRANSPARENT)
        return used

    def save(self, path=None):
        d = self.data
        d['name'] = self.name
        d['cell'] = self.cell
        d['layers'] = [{'name': l.name, 'visible': l.visible, 'locked': l.locked,
                        'dataUrl': encode_data_url(l.img)} for l in self.layers]
        d['pxDataUrl'] = encode_data_url(self.composite())
        d['version'] = self.version + 1
        d['updatedAt'] = int(time.time() * 1000)
        target = path or self.path
        tmp = target + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, target)
        self.version += 1


# ---------- 區域 ----------

def parse_pair(s, what):
    try:
        a, b = [int(v) for v in str(s).split(',')]
        return a, b
    except ValueError:
        raise ToolError('%s 要寫成 數字,數字：%s' % (what, s))


def cell_rect(proj, c, r):
    if not (0 <= c < proj.cols and 0 <= r < proj.rows):
        raise ToolError('沒有格子 %d,%d（共 %d 欄 × %d 列）' % (c, r, proj.cols, proj.rows))
    return (c * proj.cell, r * proj.cell, proj.cell, proj.cell)


def clip_rect(rect, bounds):
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    x0, y0 = max(x, bx), max(y, by)
    x1, y1 = min(x + w, bx + bw), min(y + h, by + bh)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def cell_of(proj, x, y):
    return (min(x // proj.cell, proj.cols - 1), min(y // proj.cell, proj.rows - 1))


# ---------- 提案 ----------

class Proposal(object):
    def __init__(self, proj, pid):
        self.proj = proj
        self.id = pid
        self.path = os.path.join(proj.data_dir, 'proposals', pid + '.json')
        if not os.path.exists(self.path):
            raise ToolError('沒有提案 %s（用 list 看有哪些）' % pid)
        with open(self.path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        # (layerIndex, x, y) -> [before, after]
        self.changes = {}
        for ch in self.data.get('changes', []):
            self.changes[(ch['l'], ch['x'], ch['y'])] = [parse_color(ch['b']), parse_color(ch['a'])]

    @staticmethod
    def create(proj, note='', author='agent'):
        pdir = os.path.join(proj.data_dir, 'proposals')
        os.makedirs(pdir, exist_ok=True)
        pid = 'p' + datetime.now().strftime('%Y%m%d-%H%M%S')
        n = 1
        while os.path.exists(os.path.join(pdir, pid + '.json')):
            n += 1
            pid = 'p' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-%d' % n
        now = int(time.time() * 1000)
        data = {'format': PROPOSAL_FORMAT, 'id': pid, 'project': os.path.basename(proj.path),
                'baseVersion': proj.version, 'author': author, 'note': note,
                'status': 'open', 'createdAt': now, 'updatedAt': now, 'changes': []}
        with open(os.path.join(pdir, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return Proposal(proj, pid)

    def require_open(self):
        if self.data.get('status') != 'open':
            raise ToolError('提案 %s 已經是 %s，不能再改' % (self.id, self.data.get('status')))

    def layer_images(self):
        """套用提案後每一層的樣子。"""
        imgs = [l.img.copy() for l in self.proj.layers]
        for (li, x, y), (_b, a) in self.changes.items():
            if 0 <= li < len(imgs):
                imgs[li].putpixel((x, y), a)
        return imgs

    def changed_cells(self):
        cells = {}
        for (li, x, y) in self.changes:
            k = cell_of(self.proj, x, y)
            cells[k] = cells.get(k, 0) + 1
        return cells

    def new_colors(self):
        used = self.proj.colors_in_use()
        new = {}
        for (_b, a) in self.changes.values():
            if a != TRANSPARENT and a not in used:
                new[a] = new.get(a, 0) + 1
        return new

    def save(self):
        self.data['updatedAt'] = int(time.time() * 1000)
        names = [l.name for l in self.proj.layers]
        self.data['changes'] = [
            {'l': li, 'ln': names[li] if li < len(names) else None, 'x': x, 'y': y,
             'b': fmt_color(b), 'a': fmt_color(a)}
            for (li, x, y), (b, a) in sorted(self.changes.items())]
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def summary(self):
        names = [l.name for l in self.proj.layers]
        per_layer = {}
        for (li, _x, _y) in self.changes:
            per_layer[li] = per_layer.get(li, 0) + 1
        return {
            'id': self.id, 'status': self.data.get('status'), 'note': self.data.get('note', ''),
            'author': self.data.get('author'), 'baseVersion': self.data.get('baseVersion'),
            'pixels': len(self.changes),
            'layers': {('%d %s' % (li, names[li] if li < len(names) else '?')): n
                       for li, n in sorted(per_layer.items())},
            'cells': {'%d,%d' % k: n for k, n in sorted(self.changed_cells().items())},
            'newColors': {fmt_color(c): n for c, n in self.new_colors().items()},
        }


# ---------- 繪圖操作 ----------

def bresenham(x0, y0, x1, y1):
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return pts
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


class OpContext(object):
    """把一個操作的座標換成整張圖的座標，並限制在格子（有給 cell 時）或整張圖內。"""

    def __init__(self, proj, op):
        self.proj = proj
        if 'cell' in op and op['cell'] is not None:
            c, r = op['cell']
            self.bounds = cell_rect(proj, int(c), int(r))
            self.ox, self.oy = self.bounds[0], self.bounds[1]
        else:
            self.bounds = (0, 0, proj.width, proj.height)
            self.ox = self.oy = 0

    def pt(self, x, y):
        return int(x) + self.ox, int(y) + self.oy

    def inside(self, x, y):
        bx, by, bw, bh = self.bounds
        return bx <= x < bx + bw and by <= y < by + bh

    def area(self, op):
        if 'w' in op or 'h' in op:
            x, y = self.pt(op.get('x', 0), op.get('y', 0))
            rect = (x, y, int(op['w']), int(op['h']))
        else:
            rect = self.bounds
        r = clip_rect(rect, self.bounds)
        if r is None:
            raise ToolError('範圍在格子或圖外面：%s' % json.dumps(op, ensure_ascii=False))
        return r


def run_op(proj, imgs, op, default_layer):
    """在 imgs（套用提案後的各層）上執行一個操作，回傳被改到的 (layer, x, y)。"""
    kind = op.get('op')
    ctx = OpContext(proj, op)
    li = proj.layer_index(op.get('layer', default_layer))
    if kind == 'revert':
        return ('revert', li, ctx.area(op))
    if proj.layers[li].locked:
        raise ToolError('圖層「%s」已鎖定，不能畫' % proj.layers[li].name)
    img = imgs[li]
    touched = set()

    def put(x, y, color):
        if ctx.inside(x, y):
            img.putpixel((x, y), color)
            touched.add((li, x, y))

    if kind == 'set':
        color = parse_color(op.get('color'))
        pts = op.get('points') or [[op['x'], op['y']]]
        for p in pts:
            put(*ctx.pt(p[0], p[1]), color=color)
    elif kind == 'line':
        color = parse_color(op.get('color'))
        x0, y0 = ctx.pt(op['x0'], op['y0'])
        x1, y1 = ctx.pt(op['x1'], op['y1'])
        for x, y in bresenham(x0, y0, x1, y1):
            put(x, y, color)
    elif kind == 'rect':
        color = parse_color(op.get('color'))
        x, y = ctx.pt(op['x'], op['y'])
        w, h = int(op['w']), int(op['h'])
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                edge = xx in (x, x + w - 1) or yy in (y, y + h - 1)
                if op.get('fill') or edge:
                    put(xx, yy, color)
    elif kind == 'fill':
        # 油漆桶：跟修圖台一樣只在該點所在的格子裡、只看這一層、四方向相連的同色
        color = parse_color(op.get('color'))
        sx, sy = ctx.pt(op['x'], op['y'])
        bounds = cell_rect(proj, *cell_of(proj, sx, sy))
        bx, by, bw, bh = bounds
        start = norm(img.getpixel((sx, sy)))
        if start != color:
            stack, seen = [(sx, sy)], {(sx, sy)}
            while stack:
                x, y = stack.pop()
                img.putpixel((x, y), color)
                touched.add((li, x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (bx <= nx < bx + bw and by <= ny < by + bh and (nx, ny) not in seen
                            and norm(img.getpixel((nx, ny))) == start):
                        seen.add((nx, ny))
                        stack.append((nx, ny))
    elif kind == 'replace':
        src, dst = parse_color(op.get('from')), parse_color(op.get('to'))
        x, y, w, h = ctx.area(op)
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if norm(img.getpixel((xx, yy))) == src:
                    put(xx, yy, dst)
    elif kind == 'clear':
        x, y, w, h = ctx.area(op)
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                put(xx, yy, TRANSPARENT)
    elif kind == 'grid':
        legend = {k: parse_color(v) for k, v in (op.get('legend') or {}).items()}
        legend.setdefault('.', TRANSPARENT)
        x0, y0 = ctx.pt(op.get('x', 0), op.get('y', 0))
        for dy, row in enumerate(op.get('rows') or []):
            for dx, ch in enumerate(row):
                if ch in (' ', '?'):
                    continue
                if ch not in legend:
                    raise ToolError('grid 第 %d 列用了圖例沒有的字元「%s」' % (dy, ch))
                put(x0 + dx, y0 + dy, legend[ch])
    elif kind in ('flip', 'shift', 'copy'):
        x, y, w, h = ctx.area(op)
        src_li = proj.layer_index(op['from_layer']) if kind == 'copy' and 'from_layer' in op else li
        piece = imgs[src_li].crop((x, y, x + w, y + h))
        if kind == 'flip':
            axis = op.get('axis', 'h')
            piece = piece.transpose(Image.FLIP_LEFT_RIGHT if axis == 'h' else Image.FLIP_TOP_BOTTOM)
            tx, ty = x, y
        elif kind == 'shift':
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    put(xx, yy, TRANSPARENT)
            tx, ty = x + int(op.get('dx', 0)), y + int(op.get('dy', 0))
        else:
            to = op.get('to')
            if to is None:
                raise ToolError('copy 要給 to：[x,y] 或 {"cell":[c,r]}')
            if isinstance(to, dict):
                # 貼到別的格子：沒給 x,y 就放在跟來源一樣的格內位置，並限制在那一格
                sb = cell_rect(proj, *cell_of(proj, x, y))
                tb = cell_rect(proj, *[int(v) for v in to['cell']])
                tx = tb[0] + (int(to['x']) if 'x' in to else x - sb[0])
                ty = tb[1] + (int(to['y']) if 'y' in to else y - sb[1])
                ctx.bounds = tb
            else:
                tx, ty = ctx.pt(to[0], to[1])
        keep_clear = kind != 'copy' or op.get('mode', 'replace') == 'replace'
        for yy in range(h):
            for xx in range(w):
                c = norm(piece.getpixel((xx, yy)))
                if c == TRANSPARENT and not keep_clear:
                    continue
                if c == TRANSPARENT and kind == 'shift':
                    continue
                put(tx + xx, ty + yy, c)
    elif kind == 'move_to_layer':
        # 跟修圖台一樣：只搬不透明的像素，蓋掉目標層同位置，並從來源層清掉
        dst_li = proj.layer_index(op.get('to'))
        if dst_li == li:
            raise ToolError('來源和目標是同一層')
        if proj.layers[dst_li].locked:
            raise ToolError('圖層「%s」已鎖定' % proj.layers[dst_li].name)
        x, y, w, h = ctx.area(op)
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                c = norm(img.getpixel((xx, yy)))
                if c == TRANSPARENT:
                    continue
                imgs[dst_li].putpixel((xx, yy), c)
                touched.add((dst_li, xx, yy))
                put(xx, yy, TRANSPARENT)
    else:
        raise ToolError('不認識的操作：%s（可用 set line rect fill replace clear grid flip shift '
                        'copy move_to_layer revert）' % kind)
    return touched


def apply_ops(prop, ops, default_layer):
    prop.require_open()
    proj = prop.proj
    imgs = prop.layer_images()
    for i, op in enumerate(ops):
        try:
            res = run_op(proj, imgs, op, default_layer)
        except ToolError as e:
            raise ToolError('第 %d 個操作失敗：%s（之前的操作都沒存）' % (i + 1, e))
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError('第 %d 個操作參數不對（%s）：%s' % (i + 1, e, json.dumps(op, ensure_ascii=False)))
        if isinstance(res, tuple) and res[0] == 'revert':
            _k, li, (x, y, w, h) = res
            for key in [k for k in prop.changes if k[0] == li and x <= k[1] < x + w and y <= k[2] < y + h]:
                del prop.changes[key]
                imgs[li].putpixel((key[1], key[2]), proj.layers[li].img.getpixel((key[1], key[2])))
            continue
        for (li, x, y) in res:
            before = norm(proj.layers[li].img.getpixel((x, y)))
            after = norm(imgs[li].getpixel((x, y)))
            if before == after:
                prop.changes.pop((li, x, y), None)
            else:
                prop.changes[(li, x, y)] = [before, after]
    prop.save()


# ---------- 預覽圖 ----------

_FONT_CACHE = {}


def font(size):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.load_default(size=size)
        except TypeError:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def checker(w, h, s):
    img = Image.new('RGBA', (w, h), (205, 205, 205, 255))
    d = ImageDraw.Draw(img)
    step = max(4, s // 2)
    for yy in range(0, h, step):
        for xx in range(0, w, step):
            if (xx // step + yy // step) % 2:
                d.rectangle([xx, yy, xx + step - 1, yy + step - 1], fill=(245, 245, 245, 255))
    return img


def magnify(img, rect, s, grid=True, mark=None, dim=False, cell=None):
    """把 rect 範圍放大 s 倍，畫像素格線；mark 是要框起來的像素（整張圖座標）。"""
    x, y, w, h = rect
    crop = img.crop((x, y, x + w, y + h))
    big = crop.resize((w * s, h * s), Image.NEAREST)
    out = checker(w * s, h * s, s)
    if dim:
        faded = big.copy()
        faded.putalpha(big.getchannel('A').point(lambda a: a * 35 // 100))
        out.alpha_composite(faded)
    else:
        out.alpha_composite(big)
    d = ImageDraw.Draw(out)
    if mark:
        for (mx, my) in mark:
            if x <= mx < x + w and y <= my < y + h:
                px, py = (mx - x) * s, (my - y) * s
                if dim:
                    c = img.getpixel((mx, my))
                    d.rectangle([px, py, px + s - 1, py + s - 1], fill=c if c[3] else None)
                d.rectangle([px, py, px + s - 1, py + s - 1], outline=(255, 0, 200, 255),
                            width=max(1, s // 8))
    if grid and s >= 4:
        for i in range(w + 1):
            gx = (x + i)
            strong = cell and gx % cell == 0
            col = (220, 40, 40, 255) if strong else ((70, 70, 70, 200) if gx % 8 == 0 else (120, 120, 120, 90))
            d.line([(i * s, 0), (i * s, h * s)], fill=col, width=2 if strong else 1)
        for j in range(h + 1):
            gy = (y + j)
            strong = cell and gy % cell == 0
            col = (220, 40, 40, 255) if strong else ((70, 70, 70, 200) if gy % 8 == 0 else (120, 120, 120, 90))
            d.line([(0, j * s), (w * s, j * s)], fill=col, width=2 if strong else 1)
    return out


def labeled(panel, rect, s, title, local_origin):
    """加上標題和座標刻度（local_origin 是刻度 0 對應的整張圖座標）。"""
    x, y, w, h = rect
    m = 26
    fs = 11 if s < 12 else 13
    out = Image.new('RGBA', (panel.width + m, panel.height + m + 18), (255, 255, 255, 255))
    out.alpha_composite(panel, (m, m + 18))
    d = ImageDraw.Draw(out)
    d.text((2, 1), title, fill=(0, 0, 0, 255), font=font(13))
    step = 1 if s >= 16 else (2 if s >= 8 else (4 if s >= 4 else 8))
    for i in range(w):
        v = x + i - local_origin[0]
        if v % step == 0:
            d.text((m + i * s + 1, 19), str(v), fill=(40, 40, 40, 255), font=font(fs))
    for j in range(h):
        v = y + j - local_origin[1]
        if v % step == 0:
            d.text((1, m + 18 + j * s + 1), str(v), fill=(40, 40, 40, 255), font=font(fs))
    return out


def tile(panels, per_row=3, gap=12):
    """每列最多 per_row 張，避免整張圖太寬、agent 看圖時被縮小。"""
    rows = [panels[i:i + per_row] for i in range(0, len(panels), per_row)]
    w = max(sum(p.width for p in r) + gap * (len(r) - 1) for r in rows)
    h = sum(max(p.height for p in r) for r in rows) + gap * (len(rows) - 1)
    out = Image.new('RGBA', (w, h), (255, 255, 255, 255))
    y = 0
    for r in rows:
        x = 0
        for p in r:
            out.alpha_composite(p, (x, y))
            x += p.width + gap
        y += max(p.height for p in r) + gap
    return out


def render(proj, rect, out_path, layer=None, prop=None, refs=(), scale=None, origin=None):
    x, y, w, h = rect
    if scale is None:
        scale = max(2, min(24, 640 // max(w, h)))
    s = scale
    origin = origin or (x, y)
    base_imgs = [l.img for l in proj.layers]
    after_imgs = prop.layer_images() if prop else None

    def pick(imgs):
        return imgs[layer] if layer is not None else proj.composite(imgs)

    what = ('layer %d' % layer) if layer is not None else 'composite'
    panels = []
    for ref in refs:
        if ref == 'hi' and proj.hi is not None:
            fx, fy = proj.hi.width / proj.width, proj.hi.height / proj.height
            crop = proj.hi.crop((int(x * fx), int(y * fy), int((x + w) * fx), int((y + h) * fy)))
            crop = crop.resize((w * s, h * s), Image.LANCZOS)
            p = checker(w * s, h * s, s)
            p.alpha_composite(crop)
            panels.append(labeled(p, rect, s, 'hi-res ref', origin))
        elif ref == 'base' and proj.base is not None:
            panels.append(labeled(magnify(proj.base, rect, s, cell=proj.cell), rect, s, 'base ref', origin))
        elif ref in ('prev', 'next'):
            c, r = cell_of(proj, x, y)
            nc = (c - 1) % proj.cols if ref == 'prev' else (c + 1) % proj.cols
            dx = (nc - c) * proj.cell
            nrect = (x + dx, y, w, h)
            if clip_rect(nrect, (0, 0, proj.width, proj.height)) == nrect:
                panels.append(labeled(magnify(proj.composite(after_imgs), nrect, s, cell=proj.cell),
                                      nrect, s, '%s cell %d,%d' % (ref, nc, r),
                                      (origin[0] + dx, origin[1])))
    panels.append(labeled(magnify(pick(base_imgs), rect, s, cell=proj.cell), rect, s,
                          'current %s' % what, origin))
    if prop:
        marks = {(k[1], k[2]) for k in prop.changes if layer is None or k[0] == layer}
        after = pick(after_imgs)
        panels.append(labeled(magnify(after, rect, s, cell=proj.cell), rect, s,
                              'proposal %s' % prop.id, origin))
        panels.append(labeled(magnify(after, rect, s, mark=marks, dim=True, cell=proj.cell), rect, s,
                              'changed pixels (%d)' % len([m for m in marks if x <= m[0] < x + w
                                                          and y <= m[1] < y + h]), origin))
    tile(panels).convert('RGB').save(out_path)


def resolve_rect(proj, args):
    if getattr(args, 'cell', None):
        c, r = parse_pair(args.cell, '--cell')
        rect = cell_rect(proj, c, r)
        return rect, (rect[0], rect[1])
    if getattr(args, 'rect', None):
        try:
            x, y, w, h = [int(v) for v in args.rect.split(',')]
        except ValueError:
            raise ToolError('--rect 要寫成 x,y,w,h')
        rect = clip_rect((x, y, w, h), (0, 0, proj.width, proj.height))
        if rect is None:
            raise ToolError('--rect 在圖外面')
        return rect, (0, 0)
    if getattr(args, 'row', None) is not None:
        r = int(args.row)
        cell_rect(proj, 0, r)
        return (0, r * proj.cell, proj.width, proj.cell), (0, 0)
    return (0, 0, proj.width, proj.height), (0, 0)


# ---------- 文字傾印 ----------

def dump(proj, rect, origin, img):
    x, y, w, h = rect
    colors = []
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            c = norm(img.getpixel((xx, yy)))
            if c != TRANSPARENT and c not in colors:
                colors.append(c)
    if len(colors) > len(LEGEND_CHARS):
        raise ToolError('顏色太多（%d 色），改用 render 看圖或縮小範圍' % len(colors))
    legend = {c: LEGEND_CHARS[i] for i, c in enumerate(colors)}
    lines = []
    lines.append('legend: . transparent  ' + '  '.join('%s %s' % (legend[c], fmt_color(c)) for c in colors))
    xs = [xx - origin[0] for xx in range(x, x + w)]
    pad = ' ' * 5
    if max(abs(v) for v in xs) >= 10:
        lines.append(pad + ''.join(str(abs(v) // 10 % 10) if abs(v) >= 10 else ' ' for v in xs))
    lines.append(pad + ''.join(str(abs(v) % 10) for v in xs))
    for yy in range(y, y + h):
        row = ''.join('.' if norm(img.getpixel((xx, yy))) == TRANSPARENT
                      else legend[norm(img.getpixel((xx, yy)))] for xx in range(x, x + w))
        lines.append('%4d %s' % (yy - origin[1], row))
    return '\n'.join(lines), {legend[c]: fmt_color(c) for c in colors}


# ---------- 指令 ----------

def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            print('%s: %s' % (k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v))
    else:
        print(obj)


def list_proposals(proj):
    pdir = os.path.join(proj.data_dir, 'proposals')
    res = []
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith('.json'):
                with open(os.path.join(pdir, fn), 'r', encoding='utf-8') as f:
                    d = json.load(f)
                res.append({'id': d['id'], 'status': d.get('status'), 'pixels': len(d.get('changes', [])),
                            'note': d.get('note', ''), 'author': d.get('author')})
    return res


def snapshot(proj, reason):
    hdir = os.path.join(proj.data_dir, 'history')
    os.makedirs(hdir, exist_ok=True)
    name = '%s-v%d-%s%s' % (datetime.now().strftime('%Y%m%d-%H%M%S'), proj.version,
                            re.sub(r'[^\w-]', '_', reason), PROJECT_SUFFIX)
    shutil.copy2(proj.path, os.path.join(hdir, name))
    snaps = sorted(f for f in os.listdir(hdir) if f.endswith(PROJECT_SUFFIX))
    for old in snaps[:-MAX_SNAPSHOTS]:
        os.remove(os.path.join(hdir, old))
    return name


def cmd_info(a):
    p = Project(a.project)
    info = {'name': p.name, 'file': p.path, 'version': p.version, 'size': [p.width, p.height],
            'cell': p.cell, 'grid': '%d cols x %d rows' % (p.cols, p.rows),
            'hiRef': list(p.hi.size) if p.hi else None, 'baseRef': list(p.base.size) if p.base else None,
            'layers': [{'index': i, 'name': l.name, 'visible': l.visible, 'locked': l.locked}
                       for i, l in enumerate(p.layers)],
            'recents': p.data.get('recents') or [],
            'colorsInUse': len(p.colors_in_use()),
            'proposals': list_proposals(p)}
    out(info, a.json)


def cmd_dump(a):
    p = Project(a.project)
    rect, origin = resolve_rect(p, a)
    imgs = Proposal(p, a.proposal).layer_images() if a.proposal else [l.img for l in p.layers]
    img = imgs[p.layer_index(a.layer)] if a.layer is not None else p.composite(imgs)
    text, _legend = dump(p, rect, origin, img)
    print(text)


def cmd_render(a):
    p = Project(a.project)
    rect, origin = resolve_rect(p, a)
    prop = Proposal(p, a.proposal) if a.proposal else None
    layer = p.layer_index(a.layer) if a.layer is not None else None
    refs = [r for r in (a.ref or '').split(',') if r]
    render(p, rect, a.out, layer=layer, prop=prop, refs=refs, scale=a.scale, origin=origin)
    print(a.out)


def cmd_propose(a):
    p = Project(a.project)
    prop = Proposal.create(p, note=a.note or '', author=a.author)
    print(prop.id)


def read_ops(a):
    src = a.ops if a.ops is not None else (open(a.ops_file, encoding='utf-8').read() if a.ops_file
                                           else sys.stdin.read())
    try:
        ops = json.loads(src)
    except ValueError as e:
        raise ToolError('操作要是 JSON：%s' % e)
    return ops if isinstance(ops, list) else [ops]


def cmd_edit(a):
    p = Project(a.project)
    prop = Proposal(p, a.proposal)
    apply_ops(prop, read_ops(a), a.layer)
    out(prop.summary(), a.json)


def cmd_show(a):
    p = Project(a.project)
    out(Proposal(p, a.proposal).summary(), a.json)


def cmd_list(a):
    p = Project(a.project)
    out(list_proposals(p), True)


def cmd_accept(a):
    p = Project(a.project)
    prop = Proposal(p, a.proposal)
    prop.require_open()
    cells = None
    if a.cells:
        cells = {parse_pair(c, '--cells') for c in a.cells.split(';') if c}
    take, conflicts = [], []
    for key, (b, aft) in prop.changes.items():
        li, x, y = key
        if cells is not None and cell_of(p, x, y) not in cells:
            continue
        if li >= len(p.layers) or p.layers[li].locked:
            conflicts.append(key)
            continue
        if norm(p.layers[li].img.getpixel((x, y))) != b:
            conflicts.append(key)
            continue
        take.append((key, aft))
    if not take:
        raise ToolError('沒有可以套用的像素（衝突 %d 個）' % len(conflicts))
    snap = snapshot(p, 'before-' + prop.id)
    for (li, x, y), aft in take:
        p.layers[li].img.putpixel((x, y), aft)
    p.save()
    for key, _aft in take:
        del prop.changes[key]
    for key in conflicts:
        del prop.changes[key]
    prop.data.setdefault('accepted', []).append(
        {'at': int(time.time() * 1000), 'pixels': len(take), 'conflicts': len(conflicts),
         'cells': sorted('%d,%d' % c for c in cells) if cells else 'all', 'projectVersion': p.version})
    if conflicts:
        prop.data.setdefault('conflicts', []).extend(
            {'l': k[0], 'x': k[1], 'y': k[2]} for k in conflicts)
    if not prop.changes:
        prop.data['status'] = 'accepted'
    prop.save()
    out({'applied': len(take), 'conflicts': len(conflicts),
         'conflictPixels': ['%d:%d,%d' % k for k in conflicts[:50]],
         'remaining': len(prop.changes), 'projectVersion': p.version, 'snapshot': snap}, a.json)


def cmd_reject(a):
    p = Project(a.project)
    prop = Proposal(p, a.proposal)
    prop.require_open()
    prop.data['status'] = 'rejected'
    prop.save()
    print('rejected %s' % prop.id)


def cmd_history(a):
    p = Project(a.project)
    hdir = os.path.join(p.data_dir, 'history')
    snaps = sorted(f for f in os.listdir(hdir) if f.endswith(PROJECT_SUFFIX)) if os.path.isdir(hdir) else []
    out(snaps, True)


def cmd_restore(a):
    p = Project(a.project)
    src = os.path.join(p.data_dir, 'history', a.snapshot)
    if not os.path.exists(src):
        raise ToolError('沒有這份快照：%s' % a.snapshot)
    snap = snapshot(p, 'before-restore')
    old = Project(src)
    p.layers, p.cell = old.layers, old.cell
    p.save()
    out({'restored': a.snapshot, 'projectVersion': p.version, 'snapshot': snap}, a.json)


def cmd_new(a):
    px = Image.open(a.pixel).convert('RGBA') if a.pixel else None
    hi = Image.open(a.hi).convert('RGBA') if a.hi else None
    base = Image.open(a.base).convert('RGBA') if a.base else None
    if px is None:
        raise ToolError('要給 --pixel 像素圖')
    if os.path.exists(a.project) and not a.force:
        raise ToolError('%s 已存在（要覆蓋加 --force）' % a.project)
    pid = 'proj_%d_%s' % (int(time.time() * 1000), ''.join(random.choice('abcdefghijklmnopqrstuvwxyz0123456789')
                                                         for _ in range(4)))
    data = {'id': pid, 'name': a.name or os.path.basename(a.project).replace(PROJECT_SUFFIX, ''),
            'cell': a.cell, 'recents': ['#000000', '#ffffff'],
            'hiDataUrl': encode_data_url(hi) if hi else None,
            'pxDataUrl': encode_data_url(px),
            'layers': [{'name': '底圖', 'visible': True, 'locked': False, 'dataUrl': encode_data_url(px)}],
            'baseDataUrl': encode_data_url(base) if base else None, 'version': 1,
            'updatedAt': int(time.time() * 1000)}
    with open(a.project, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    print(a.project)


def cmd_export(a):
    p = Project(a.project)
    if a.layers_dir:
        os.makedirs(a.layers_dir, exist_ok=True)
        for i, l in enumerate(p.layers):
            fn = os.path.join(a.layers_dir, '%02d_%s.png' % (i + 1, re.sub(r'[\\/:*?"<>|]', '_', l.name)))
            l.img.save(fn)
            print(fn)
    if a.out:
        p.composite().save(a.out)
        print(a.out)


def build_parser():
    ap = argparse.ArgumentParser(prog='pixelfixer.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument('project', help='專案檔 .pixelfixer.json')
        sp.add_argument('--json', action='store_true', help='輸出 JSON')
        sp.set_defaults(fn=fn)
        return sp

    def area(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument('--cell', help='格子 欄,列（從 0 起算）；座標刻度改成格子內座標')
        g.add_argument('--rect', help='整張圖座標 x,y,w,h')
        g.add_argument('--row', type=int, help='一整列格子')

    add('info', cmd_info, '專案資訊：大小、格子、圖層、提案')

    sp = add('dump', cmd_dump, '把一格印成文字（每個像素一個字元＋圖例），最適合讀精確座標')
    area(sp)
    sp.add_argument('--layer', help='圖層編號或名稱；不給＝合併後的樣子')
    sp.add_argument('--proposal', help='套用這個提案後再印')

    sp = add('render', cmd_render, '輸出放大加格線的 PNG，可並排參考圖與提案前後')
    area(sp)
    sp.add_argument('--layer', help='只看這一層；不給＝合併')
    sp.add_argument('--proposal', help='並排顯示提案後的樣子與改動的像素')
    sp.add_argument('--ref', help='並排參考：hi,base,prev,next 任選，逗號分隔')
    sp.add_argument('--scale', type=int, help='每個像素放大幾倍（預設自動）')
    sp.add_argument('-o', '--out', required=True, help='輸出 PNG 路徑')

    sp = add('propose', cmd_propose, '開一個新提案，印出提案 id')
    sp.add_argument('--note', help='提案說明（給使用者看）')
    sp.add_argument('--author', default='agent')

    sp = add('edit', cmd_edit, '在提案上執行繪圖操作（JSON，見 AGENT.md）')
    sp.add_argument('proposal')
    sp.add_argument('--layer', help='操作沒寫 layer 時用這一層')
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--ops', help='操作 JSON 字串')
    g.add_argument('--ops-file', help='操作 JSON 檔；都不給就讀 stdin')

    sp = add('show', cmd_show, '提案摘要：改了幾個像素、哪些格子、新顏色')
    sp.add_argument('proposal')

    add('list', cmd_list, '列出提案')

    sp = add('accept', cmd_accept, '把提案寫回專案（先存快照；原色已變的像素算衝突、不套用）')
    sp.add_argument('proposal')
    sp.add_argument('--cells', help='只接受這些格子，例：0,0;1,0')

    sp = add('reject', cmd_reject, '退回提案')
    sp.add_argument('proposal')

    add('history', cmd_history, '列出快照')
    sp = add('restore', cmd_restore, '用快照還原專案（還原前也會先存快照）')
    sp.add_argument('snapshot')

    sp = add('new', cmd_new, '用 PNG 建新專案')
    sp.add_argument('--pixel', required=True, help='像素圖 PNG')
    sp.add_argument('--hi', help='高解析參考圖')
    sp.add_argument('--base', help='人偶參考圖')
    sp.add_argument('--cell', type=int, default=32)
    sp.add_argument('--name')
    sp.add_argument('--force', action='store_true')

    sp = add('export', cmd_export, '匯出合併 PNG 或每層一張 PNG')
    sp.add_argument('-o', '--out', help='合併圖 PNG')
    sp.add_argument('--layers-dir', help='每層一張 PNG 的資料夾')
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, 'fn', None):
        ap.print_help()
        return 2
    try:
        a.fn(a)
    except ToolError as e:
        print('error: %s' % e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
