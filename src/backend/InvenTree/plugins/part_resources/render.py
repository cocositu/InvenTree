"""KiCad 文件渲染为位图（供缩略图缓存与按需高清预览使用）。

设计取舍
--------
不做 SVG 中转，直接用 Pillow 绘制：
  - 少一层依赖（不需要 cairosvg / cairo 绑定）
  - 输出直接是 PNG，正好匹配 InvenTree ``Attachment.thumbnail`` 这个 ImageField

调用方约定：
  - ``render_footprint(text, size)`` / ``render_symbol(text, size)`` 返回 PIL.Image
  - 不认识的文件类型返回 None，由调用方决定回退策略
"""

import json
import math
import re
import struct

from PIL import Image, ImageDraw, ImageFont

# 默认配色（贴近 KiCad 深色主题，但在浅色背景下也清晰）
BG = (250, 250, 250)
COPPER = (200, 52, 52)  # 焊盘 / 铜层
SILK = (240, 240, 240)
SILK_LIGHT = (170, 170, 170)
EDGE = (90, 90, 90)
DRILL = (250, 250, 250)  # 通孔
BODY = (255, 245, 200)  # 符号外框填充
PIN = (140, 60, 60)


# ----------------------------------------------------------------------
# S-expression 解析
# ----------------------------------------------------------------------
TOKEN_RE = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse_sexpr(text: str):
    """把 KiCad 的 S-expression 解析成嵌套 list。

    返回形如 ['module', 'LQFP-48', ['layer', 'F.Cu'], ...] 的结构。
    解析失败时返回 None（调用方应回退，而不是抛异常）。
    """
    try:
        tokens = TOKEN_RE.findall(text)
    except Exception:
        return None

    stack = []
    current = []

    for tok in tokens:
        if tok == '(':
            stack.append(current)
            current = []
        elif tok == ')':
            if not stack:
                return None
            parent = stack.pop()
            parent.append(current)
            current = parent
        else:
            if tok.startswith('"') and tok.endswith('"'):
                tok = tok[1:-1]
            current.append(tok)

    # 最外层
    while stack:
        parent = stack.pop()
        parent.append(current)
        current = parent

    for item in current:
        if isinstance(item, list):
            return item
    return None


def find_all(node, key):
    """取出所有形如 (key ...) 的子节点。"""
    out = []
    if not isinstance(node, list):
        return out
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            out.append(child)
    return out


def find_one(node, key):
    got = find_all(node, key)
    return got[0] if got else None


def find_all_deep(node, key):
    """递归取出所有形如 (key ...) 的节点（包含嵌套子节点）。"""
    out = []
    if not isinstance(node, list) or not node:
        return out
    if node[0] == key:
        out.append(node)
    for child in node:
        if isinstance(child, list):
            out.extend(find_all_deep(child, key))
    return out


_FONT_CACHE = {}


_FONT_CANDIDATES = (
    r'C:\Windows\Fonts\arialbd.ttf',
    r'C:\Windows\Fonts\segoeuib.ttf',
    r'C:\Windows\Fonts\consolab.ttf',
    'DejaVuSans-Bold.ttf',
    'DejaVuSans.ttf',
)


def _load_font(px):
    """按像素大小加载有衬线/无衬线字体；失败则回退 Pillow 默认字体。"""
    px = max(7, int(px))
    if px in _FONT_CACHE:
        return _FONT_CACHE[px]

    font = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, px)
            break
        except Exception:
            continue

    if font is None:
        try:
            font = ImageFont.load_default(size=px)
        except Exception:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None

    _FONT_CACHE[px] = font
    return font


def _draw_text(draw, xy, content, font, fill, anchor='mm', halo=None):
    """带容错的文字绘制（Pillow 版本差异较大）。"""
    if not content or font is None:
        return
    try:
        if halo:
            draw.text(xy, content, font=font, fill=fill, anchor=anchor,
                      stroke_width=1, stroke_fill=halo)
        else:
            draw.text(xy, content, font=font, fill=fill, anchor=anchor)
    except Exception:
        try:
            x, y = xy
            box = draw.textbbox((0, 0), content, font=font)
            x -= (box[2] - box[0]) / 2
            y -= (box[3] - box[1]) / 2
            draw.text((x, y), content, font=font, fill=fill)
        except Exception:
            try:
                draw.text(xy, content, font=font, fill=fill)
            except Exception:
                pass


def _node_layers(node):
    """读取 KiCad 节点上的 layer / layers 字段。"""
    layers = []
    if not isinstance(node, list) or not node:
        return layers
    layer = find_one(node, 'layer')
    if layer and len(layer) > 1 and isinstance(layer[1], str):
        layers.append(layer[1])
    layers_node = find_one(node, 'layers')
    if layers_node:
        for value in layers_node[1:]:
            if isinstance(value, str):
                layers.append(value)
    return layers


_LAYER_ORDER = [
    'F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu',
    'F.Paste', 'B.Paste', 'F.SilkS', 'B.SilkS', 'F.Mask', 'B.Mask',
    'F.Fab', 'B.Fab', 'F.CrtYd', 'B.CrtYd', 'Edge.Cuts',
    'F.Adhes', 'B.Adhes', 'F.Adhesive', 'B.Adhesive',
    'Dwgs.User', 'Cmts.User', 'Eco1.User', 'Eco2.User', 'Margin',
]
_LAYER_RANK = {name: index for index, name in enumerate(_LAYER_ORDER)}


def _layer_sort_key(name):
    return (_LAYER_RANK.get(name, len(_LAYER_ORDER)), name)


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def xy_of(node):
    """从 (at x y [rot]) 或 (center x y) 里取出坐标。"""
    if not isinstance(node, list) or len(node) < 3:
        return 0.0, 0.0
    return num(node[1]), num(node[2])


# ----------------------------------------------------------------------
# 通用：坐标变换与绘制
# ----------------------------------------------------------------------
class Viewport:
    """把 KiCad 坐标（Y 轴向上）映射到图像坐标（Y 轴向下），并自动缩放居中。"""

    def __init__(self, bbox, size, margin=0.08):
        x0, y0, x1, y1 = bbox
        w = max(x1 - x0, 1e-6)
        h = max(y1 - y0, 1e-6)

        pad = max(w, h) * margin
        x0 -= pad
        y0 -= pad
        x1 += pad
        y1 += pad
        w, h = x1 - x0, y1 - y0

        self.size = size
        self.scale = min(size / w, size / h)
        self.cx = (x0 + x1) / 2
        self.cy = (y0 + y1) / 2

    def to_px(self, x, y):
        px = self.size / 2 + (x - self.cx) * self.scale
        py = self.size / 2 - (y - self.cy) * self.scale  # Y 轴翻转
        return px, py


def bbox_of_points(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def ensure_bbox(bbox, fallback=(-5, -5, 5, 5)):
    x0, y0, x1, y1 = bbox
    if not all(math.isfinite(v) for v in bbox) or (x1 - x0) < 1e-6 or (y1 - y0) < 1e-6:
        return fallback
    return bbox


# ----------------------------------------------------------------------
# 封装（.kicad_mod）
# ----------------------------------------------------------------------
def _footprint_points(root):
    """收集用于计算包围盒的所有坐标点。"""
    pts = []
    for child in root:
        if not isinstance(child, list) or not child:
            continue
        tag = child[0]

        if tag == 'pad':
            at = find_one(child, 'at')
            size = find_one(child, 'size')
            if at:
                x, y = xy_of(at)
                pts.append((x, y))
                if size:
                    sx, sy = num(size[1]), num(size[2])
                    pts += [(x - sx / 2, y - sy / 2), (x + sx / 2, y + sy / 2)]
        elif tag == 'fp_line':
            s, e = find_one(child, 'start'), find_one(child, 'end')
            if s and e:
                pts.append(xy_of(s))
                pts.append(xy_of(e))
        elif tag == 'fp_rect':
            s, e = find_one(child, 'start'), find_one(child, 'end')
            if s and e:
                pts.append(xy_of(s))
                pts.append(xy_of(e))
        elif tag == 'fp_circle':
            c, e = find_one(child, 'center'), find_one(child, 'end')
            if c and e:
                x, y = xy_of(c)
                ex, ey = xy_of(e)
                r = math.hypot(ex - x, ey - y)
                pts += [(x - r, y - r), (x + r, y + r)]
        elif tag == 'fp_arc':
            s, m, e = (
                find_one(child, 'start'),
                find_one(child, 'mid'),
                find_one(child, 'end'),
            )
            for node in (s, m, e):
                if node:
                    pts.append(xy_of(node))

    return pts or [(-5, -5), (5, 5)]


def render_footprint(text: str, size: int = 256, layers=None):
    """把 .kicad_mod 渲染成 PIL.Image。

    Args:
        layers: 可选图层集合，例如 {'F.Cu', 'F.SilkS'}；None 表示全部图层。
    """
    root = parse_sexpr(text)
    if not root or root[0] != 'module':
        return None

    pts = _footprint_points(root)
    vp = Viewport(ensure_bbox(bbox_of_points(pts)), size)

    selected_layers = {str(name) for name in layers} if layers else None

    def visible(node):
        if selected_layers is None:
            return True
        return bool(set(_node_layers(node)) & selected_layers)

    img = Image.new('RGB', (size, size), BG)
    draw = ImageDraw.Draw(img)

    def line_width(node, default=0.12):
        width = find_one(node, 'width')
        return max(1, int(num(width[1], default) * vp.scale)) if width else 2

    # 1) 先画丝印、外形、Fab 等图形层
    for child in root:
        if not isinstance(child, list) or not child:
            continue
        tag = child[0]

        if tag not in ('fp_line', 'fp_rect', 'fp_circle', 'fp_arc'):
            continue
        if not visible(child):
            continue

        if tag == 'fp_line':
            s, e = find_one(child, 'start'), find_one(child, 'end')
            if s and e:
                draw.line(
                    [vp.to_px(*xy_of(s)), vp.to_px(*xy_of(e))],
                    fill=SILK_LIGHT,
                    width=line_width(child),
                )
        elif tag == 'fp_rect':
            s, e = find_one(child, 'start'), find_one(child, 'end')
            if s and e:
                x0, y0 = xy_of(s)
                x1, y1 = xy_of(e)
                p0, p1 = vp.to_px(x0, y0), vp.to_px(x1, y1)
                draw.rectangle(
                    [min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])],
                    outline=SILK_LIGHT,
                    width=line_width(child),
                )
        elif tag == 'fp_circle':
            c, e = find_one(child, 'center'), find_one(child, 'end')
            if c and e:
                x, y = xy_of(c)
                ex, ey = xy_of(e)
                r = math.hypot(ex - x, ey - y) * vp.scale
                px, py = vp.to_px(x, y)
                draw.ellipse(
                    [px - r, py - r, px + r, py + r],
                    outline=SILK_LIGHT,
                    width=line_width(child),
                )

    # 2) 再画焊盘，并在焊盘中心写引脚编号
    font = _load_font(max(8, min(30, int(size / 22))))

    for pad in find_all(root, 'pad'):
        if not visible(pad):
            continue

        at = find_one(pad, 'at')
        size_node = find_one(pad, 'size')
        if not at or not size_node:
            continue

        x, y = xy_of(at)
        w = num(size_node[1], 1.0)
        h = num(size_node[2], 1.0)
        shape = pad[3] if len(pad) > 3 else 'rect'
        ptype = pad[2] if len(pad) > 2 else 'smd'

        px, py = vp.to_px(x, y)
        pw, ph = w * vp.scale, h * vp.scale

        box = [px - pw / 2, py - ph / 2, px + pw / 2, py + ph / 2]

        if shape == 'circle' or shape == 'oval':
            draw.ellipse(box, fill=COPPER)
        else:
            draw.rectangle(box, fill=COPPER)

        if ptype == 'thru_hole':
            drill = find_one(pad, 'drill')
            d = 0.0
            if drill:
                for token in drill[1:]:
                    if not isinstance(token, list):
                        d = num(token, d)
                        break
            if d > 0:
                r = d * vp.scale / 2
                draw.ellipse([px - r, py - r, px + r, py + r], fill=DRILL)

        # 引脚编号 / 焊盘名：KiCad 的 pad 节点第二项就是编号
        if len(pad) > 1 and isinstance(pad[1], str) and pad[1].strip():
            _draw_text(draw, (px, py), pad[1].strip(), font, (255, 255, 255),
                       halo=(90, 40, 40))

    return img


# ----------------------------------------------------------------------
# 原理图符号（.kicad_sym）
# ----------------------------------------------------------------------
def _symbol_root(root, ref: str = None):
    """在符号库里挑出一个具体符号。

    库文件里可能有多个 symbol；优先返回名称匹配 ref 的，否则返回第一个。
    """
    if not root or root[0] != 'kicad_symbol_lib':
        # 也可能是单个 symbol
        return root if root and root[0] == 'symbol' else None

    syms = find_all(root, 'symbol')
    if not syms:
        return None

    if ref:
        for s in syms:
            if len(s) > 1 and str(s[1]).split(':')[-1] == ref:
                return s

    # 顶层符号通常包含子 symbol（_0_1 / _1_1）
    return syms[0]


def _symbol_points(sym):
    pts = []

    def walk(node):
        if not isinstance(node, list) or not node:
            return
        tag = node[0]
        if tag in ('rectangle', 'polyline', 'circle', 'arc'):
            for sub in node[1:]:
                if isinstance(sub, list):
                    if sub[0] in ('start', 'end', 'center', 'mid', 'xy') and len(sub) >= 3:
                        pts.append((num(sub[1]), num(sub[2])))
                    else:
                        walk(sub)
        elif tag == 'pin':
            at = find_one(node, 'at')
            length_node = find_one(node, 'length')
            if at:
                x, y = xy_of(at)
                ang = num(at[3], 0.0) if len(at) > 3 else 0.0
                ln = num(length_node[1], 2.54) if length_node else 2.54
                rad = math.radians(ang)
                pts.append((x, y))
                pts.append((x + ln * math.cos(rad), y + ln * math.sin(rad)))
        for child in node:
            if isinstance(child, list):
                walk(child)

    walk(sym)
    return pts or [(-10, -10), (10, 10)]


def render_symbol(text: str, size: int = 256, ref: str = None, show_labels=True):
    """把 .kicad_sym 渲染成 PIL.Image，并绘制引脚编号 / 名称。"""
    root = parse_sexpr(text)
    sym = _symbol_root(root, ref)
    if not sym:
        return None

    pins = find_all_deep(sym, 'pin')
    pts = _symbol_points(sym)
    vp = Viewport(ensure_bbox(bbox_of_points(pts)), size, margin=0.12)

    img = Image.new('RGB', (size, size), BG)
    draw = ImageDraw.Draw(img)

    # 递归收集所有绘图节点（含子 symbol 单元）
    draw_nodes = []

    def collect(node):
        if not isinstance(node, list) or not node:
            return
        for child in node:
            if isinstance(child, list) and child:
                if child[0] in ('rectangle', 'polyline', 'circle', 'arc'):
                    draw_nodes.append(child)
                collect(child)

    collect(sym)

    def stroke_width(node, default=0.2):
        stroke = find_one(node, 'stroke')
        if stroke:
            w = find_one(stroke, 'width')
            if w:
                return max(1, int(num(w[1], default) * vp.scale))
        return max(1, int(default * vp.scale))

    # 图形
    for node in draw_nodes:
        tag = node[0]

        if tag == 'rectangle':
            s, e = find_one(node, 'start'), find_one(node, 'end')
            if s and e:
                p0, p1 = vp.to_px(*xy_of(s)), vp.to_px(*xy_of(e))
                box = [
                    min(p0[0], p1[0]), min(p0[1], p1[1]),
                    max(p0[0], p1[0]), max(p0[1], p1[1]),
                ]
                draw.rectangle(box, fill=BODY, outline=PIN, width=stroke_width(node, 0.254))
        elif tag == 'polyline':
            pts_node = find_one(node, 'pts')
            if pts_node:
                coords = [vp.to_px(num(p[1]), num(p[2])) for p in find_all(pts_node, 'xy') if len(p) >= 3]
                if len(coords) >= 2:
                    draw.line(coords, fill=PIN, width=stroke_width(node, 0.254))
        elif tag == 'circle':
            c, r_node = find_one(node, 'center'), find_one(node, 'radius')
            if c and r_node:
                x, y = xy_of(c)
                r = num(r_node[1], 1.0) * vp.scale
                px, py = vp.to_px(x, y)
                draw.ellipse([px - r, py - r, px + r, py + r], outline=PIN, width=stroke_width(node, 0.254))

    # 引脚 + 编号 / 名称
    font = _load_font(max(8, min(28, int(size / 30))))

    for pin in pins:
        at = find_one(pin, 'at')
        length_node = find_one(pin, 'length')
        if not at:
            continue

        x, y = xy_of(at)
        ang = num(at[3], 0.0) if len(at) > 3 else 0.0
        ln = num(length_node[1], 2.54) if length_node else 2.54
        rad = math.radians(ang)

        p0 = vp.to_px(x, y)
        p1 = vp.to_px(x + ln * math.cos(rad), y + ln * math.sin(rad))

        draw.line([p0, p1], fill=PIN, width=max(1, int(0.15 * vp.scale)))

        if not show_labels or size < 192:
            continue

        name_node = find_one(pin, 'name')
        number_node = find_one(pin, 'number')
        pin_name = str(name_node[1]) if name_node and len(name_node) > 1 else ''
        pin_number = str(number_node[1]) if number_node and len(number_node) > 1 else ''

        vx, vy = p1[0] - p0[0], p1[1] - p0[1]
        length = math.hypot(vx, vy)
        if length < 1e-6:
            ux, uy = 1.0, 0.0
        else:
            ux, uy = vx / length, vy / length
        px, py = -uy, ux

        anchor_x = (p0[0] + p1[0]) / 2.0
        anchor_y = (p0[1] + p1[1]) / 2.0
        scale_font = max(8, min(28, int(size / 30)))

        if pin_number:
            pos = (anchor_x + px * scale_font * 0.8, anchor_y + py * scale_font * 0.8)
            _draw_text(draw, pos, pin_number, font, (55, 55, 55), halo=BG)

        if pin_name:
            pos = (
                p1[0] + ux * scale_font * 0.8 + px * scale_font * 0.4,
                p1[1] + uy * scale_font * 0.8 + py * scale_font * 0.4,
            )
            _draw_text(draw, pos, pin_name, font, (88, 88, 88), halo=BG)

    return img


# ----------------------------------------------------------------------
# STEP / IGES 渲染：cascadio -> GLB -> Pillow 等轴测着色
# ----------------------------------------------------------------------
GLB_MAGIC = b'glTF'
GLB_CHUNK_JSON = 0x4E4F534A
GLB_CHUNK_BIN = 0x004E4942

_COMPONENT_FORMAT = {
    5120: ('b', 1),
    5121: ('B', 1),
    5122: ('h', 2),
    5123: ('H', 2),
    5125: ('I', 4),
    5126: ('f', 4),
}
_TYPE_COMPONENTS = {
    'SCALAR': 1,
    'VEC2': 2,
    'VEC3': 3,
    'VEC4': 4,
    'MAT2': 4,
    'MAT3': 9,
    'MAT4': 16,
}


class _TooManyTriangles(Exception):
    """模型三角面过多，调用方应用更粗的网格精度重试。"""


def step_renderer_available() -> bool:
    """本环境是否安装了 cascadio（STEP / IGES -> GLB 转换器）。"""
    try:
        import cascadio  # noqa: F401
    except Exception:
        return False
    return True


def _read_glb(data):
    """解析 GLB 二进制，返回 (gltf dict, BIN chunk bytes)。失败返回 (None, None)。"""
    if not data or len(data) < 12 or data[:4] != GLB_MAGIC:
        return None, None

    try:
        _version, total = struct.unpack_from('<II', data, 4)
    except struct.error:
        return None, None

    total = min(int(total), len(data))
    offset = 12
    json_bytes = None
    bin_bytes = None

    while offset + 8 <= total:
        try:
            length, kind = struct.unpack_from('<II', data, offset)
        except struct.error:
            break
        offset += 8
        chunk = data[offset:offset + length]
        offset += length

        if kind == GLB_CHUNK_JSON:
            json_bytes = chunk
        elif kind == GLB_CHUNK_BIN:
            bin_bytes = chunk

    if json_bytes is None:
        return None, None

    try:
        gltf = json.loads(json_bytes.rstrip(b'\x00 \t\r\n').decode('utf-8'))
    except Exception:
        return None, None

    return gltf, bin_bytes


def _read_accessor(gltf, bin_data, accessor_index):
    """读取 glTF accessor，返回 Python list（标量或 tuple）。"""
    if bin_data is None:
        raise _TooManyTriangles

    try:
        accessor = gltf['accessors'][accessor_index]
        view = gltf['bufferViews'][accessor['bufferView']]
    except Exception:
        return []

    component_type = accessor.get('componentType')
    format_info = _COMPONENT_FORMAT.get(component_type)
    component_count = _TYPE_COMPONENTS.get(accessor.get('type'))

    if not format_info or not component_count:
        return []

    fmt, component_size = format_info
    count = int(accessor.get('count', 0))

    # 安全上限：单个 accessor 超过 110 万条时放弃，提示调用方降精度
    if count > 1_100_000:
        raise _TooManyTriangles

    base = int(view.get('byteOffset', 0)) + int(accessor.get('byteOffset', 0))
    stride = int(view.get('byteStride') or (component_size * component_count))
    needed = base + (count - 1) * stride + component_size * component_count

    if count <= 0 or needed > len(bin_data):
        return []

    values = []
    unpack = struct.unpack_from

    for index in range(count):
        item = unpack('<' + fmt * component_count, bin_data, base + index * stride)
        values.append(item[0] if component_count == 1 else item)

    return values


# ---- 4x4 矩阵（行主序） -------------------------------------------------

def _mat_identity():
    return [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def _mat_mul(a, b):
    return [
        [sum(a[row][k] * b[k][col] for k in range(4)) for col in range(4)]
        for row in range(4)
    ]


def _node_matrix(node):
    """把 glTF node 的 matrix 或 TRS 转成 4x4 行主序矩阵。"""
    if 'matrix' in node:
        m = node['matrix']
        return [
            [m[0], m[4], m[8], m[12]],
            [m[1], m[5], m[9], m[13]],
            [m[2], m[6], m[10], m[14]],
            [m[3], m[7], m[11], m[15]],
        ]

    tx, ty, tz = node.get('translation', [0.0, 0.0, 0.0])
    qx, qy, qz, qw = node.get('rotation', [0.0, 0.0, 0.0, 1.0])
    sx, sy, sz = node.get('scale', [1.0, 1.0, 1.0])

    # 四元数 -> 旋转矩阵
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz

    rotation = [
        [1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy), 0.0],
        [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx), 0.0],
        [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy), 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    scale = [
        [sx, 0.0, 0.0, 0.0],
        [0.0, sy, 0.0, 0.0],
        [0.0, 0.0, sz, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    translation = [
        [1.0, 0.0, 0.0, tx],
        [0.0, 1.0, 0.0, ty],
        [0.0, 0.0, 1.0, tz],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return _mat_mul(translation, _mat_mul(rotation, scale))


def _apply_matrix(matrix, point):
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )


def _material_color(gltf, material_index):
    default = (0.38, 0.55, 0.72)
    if material_index is None:
        return default

    try:
        material = gltf.get('materials', [])[material_index]
        pbr = material.get('pbrMetallicRoughness') or {}
        factor = pbr.get('baseColorFactor')
        if factor and len(factor) >= 3:
            return tuple(max(0.0, min(1.0, float(v))) for v in factor[:3])
    except Exception:
        pass

    return default


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalize(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _triangles_from_gltf(gltf, bin_data, limit=0):
    """遍历 glTF 场景，返回 [(p0, p1, p2, base_color), ...]。

    超过 ``limit`` 时抛 _TooManyTriangles，由调用方降低网格精度重试。
    """
    meshes = gltf.get('meshes') or []
    nodes = gltf.get('nodes') or []
    scenes = gltf.get('scenes') or []
    triangles = []

    def add_triangle(p0, p1, p2, color):
        if limit and len(triangles) >= limit:
            raise _TooManyTriangles
        triangles.append((p0, p1, p2, color))

    def visit(node_index, parent_matrix):
        try:
            node = nodes[node_index]
        except Exception:
            return

        world = _mat_mul(parent_matrix, _node_matrix(node))

        if 'mesh' in node:
            try:
                mesh = meshes[node['mesh']]
            except Exception:
                mesh = None

            if mesh:
                for primitive in mesh.get('primitives', []):
                    if primitive.get('mode', 4) != 4:
                        continue

                    attributes = primitive.get('attributes') or {}
                    position_index = attributes.get('POSITION')
                    if position_index is None:
                        continue

                    positions = _read_accessor(gltf, bin_data, position_index)
                    if not positions:
                        continue

                    index_index = primitive.get('indices')
                    if index_index is None:
                        indices = list(range(len(positions)))
                    else:
                        indices = _read_accessor(gltf, bin_data, index_index)
                    if not indices:
                        continue

                    color = _material_color(gltf, primitive.get('material'))

                    for i in range(0, len(indices) - 2, 3):
                        try:
                            a, b, c = int(indices[i]), int(indices[i + 1]), int(indices[i + 2])
                            p0 = _apply_matrix(world, positions[a])
                            p1 = _apply_matrix(world, positions[b])
                            p2 = _apply_matrix(world, positions[c])
                        except Exception:
                            continue
                        add_triangle(p0, p1, p2, color)

        for child in node.get('children', []) or []:
            visit(child, world)

    scene_nodes = []
    if scenes:
        scene_nodes = scenes[0].get('nodes') or []
    if not scene_nodes:
        scene_nodes = list(range(len(nodes)))

    for root in scene_nodes:
        visit(root, _mat_identity())

    return triangles


def render_brep(data: bytes, size: int = 256, file_type: str = 'step'):
    """把 STEP / IGES 模型渲染成带简单光照的等轴测 PNG。

    cascadio 是可选依赖：没装时返回 None，插件会安全降级为普通附件下载。
    """
    if not data:
        return None

    try:
        import cascadio
    except Exception:
        return None

    max_triangles = 18000 if size <= 320 else 55000
    tolerance = 0.35 if size <= 320 else 0.12
    triangles = []

    for _attempt in range(4):
        try:
            glb = cascadio.load(
                data,
                file_type=file_type,
                tol_linear=tolerance,
                tol_angular=0.6,
                merge_primitives=True,
                use_parallel=True,
            )
        except Exception:
            return None

        if not glb:
            return None

        gltf, bin_data = _read_glb(glb)
        if not gltf:
            return None

        try:
            triangles = _triangles_from_gltf(gltf, bin_data, limit=max_triangles)
            break
        except _TooManyTriangles:
            tolerance *= 2.5

    if not triangles:
        return None

    return _render_triangles(triangles, size)


def _render_triangles(triangles, size, sample=2):
    """把三角形列表画成等轴测着色图。"""
    canvas = max(128, int(size) * max(1, int(sample)))
    margin = canvas * 0.08

    inv_sqrt2 = 1.0 / math.sqrt(2.0)
    inv_sqrt6 = 1.0 / math.sqrt(6.0)

    def project(point):
        x, y, z = point
        return (
            (x - y) * inv_sqrt2,
            (x + y - 2.0 * z) * inv_sqrt6,
        )

    projected = []
    for p0, p1, p2, color in triangles:
        projected.append((project(p0), project(p1), project(p2), color))

    xs = [p[0] for triangle in projected for p in triangle[:3]]
    ys = [p[1] for triangle in projected for p in triangle[:3]]
    if not xs or not ys:
        return None

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    scale = min((canvas - 2.0 * margin) / span_x, (canvas - 2.0 * margin) / span_y)
    center_x = (min_x + max_x) * 0.5
    center_y = (min_y + max_y) * 0.5

    def to_pixel(point):
        u, v = point
        return (
            canvas * 0.5 + (u - center_x) * scale,
            canvas * 0.5 + (v - center_y) * scale,
        )

    # 光源方向固定，正面用兼容正负法线的方式着色
    light = _normalize((-0.38, -0.52, 0.76))

    # 画家算法：先画远端
    draw_items = []
    for p0, p1, p2, color in triangles:
        depth = (p0[0] + p0[1] + p0[2] + p1[0] + p1[1] + p1[2] + p2[0] + p2[1] + p2[2]) / 3.0
        draw_items.append((depth, p0, p1, p2, color))
    draw_items.sort(key=lambda item: item[0])

    img = Image.new('RGB', (canvas, canvas), BG)
    draw = ImageDraw.Draw(img)

    for _depth, p0, p1, p2, color in draw_items:
        normal = _normalize(_cross(
            (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]),
            (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]),
        ))
        if normal == (0.0, 0.0, 0.0):
            continue

        lambert = abs(normal[0] * light[0] + normal[1] * light[1] + normal[2] * light[2])
        intensity = 0.34 + 0.66 * lambert
        rgb = tuple(
            max(0, min(255, int(255.0 * (0.08 + 0.92 * channel * intensity))))
            for channel in color
        )

        points = [to_pixel(project(p0)), to_pixel(project(p1)), to_pixel(project(p2))]
        draw.polygon(points, fill=rgb)

    if canvas != size:
        img = img.resize((int(size), int(size)), Image.LANCZOS)

    return img



def describe_attachment(filename: str, data: bytes):
    """返回附件的可交互元数据（可用图层、引脚数量等）。"""
    name = (filename or '').lower()

    if name.endswith(('.kicad_mod', '.pretty')):
        try:
            root = parse_sexpr(data.decode('utf-8', 'replace'))
        except Exception:
            return {'kind': 'footprint', 'layers': [], 'pin_count': 0}
        if not root:
            return {'kind': 'footprint', 'layers': [], 'pin_count': 0}

        layers = set()

        def walk(node):
            if not isinstance(node, list):
                return
            for layer in _node_layers(node):
                layers.add(layer)
            for child in node:
                walk(child)

        walk(root)
        pads = [
            pad for pad in find_all(root, 'pad')
            if len(pad) > 1 and isinstance(pad[1], str) and pad[1].strip()
        ]
        return {
            'kind': 'footprint',
            'layers': sorted(layers, key=_layer_sort_key),
            'pin_count': len(pads),
        }

    if name.endswith('.kicad_sym'):
        try:
            root = parse_sexpr(data.decode('utf-8', 'replace'))
        except Exception:
            return {'kind': 'symbol', 'pin_count': 0}
        sym = _symbol_root(root)
        pins = find_all_deep(sym, 'pin') if sym else []
        return {'kind': 'symbol', 'pin_count': len(pins)}

    if name.endswith(('.step', '.stp', '.iges', '.igs')):
        return {'kind': 'model3d'}

    return {'kind': 'other'}


def mesh_from_brep(data: bytes, file_type: str = 'step', max_triangles: int = 30000):
    """把 STEP / IGES 转成适合前端 canvas 绘制的轻量 JSON 网格。"""
    if not data:
        return None

    try:
        import cascadio
    except Exception:
        return None

    tolerance = 0.2
    triangles = []

    for _attempt in range(4):
        try:
            glb = cascadio.load(
                data,
                file_type=file_type,
                tol_linear=tolerance,
                tol_angular=0.6,
                merge_primitives=True,
                use_parallel=True,
            )
        except Exception:
            return None

        if not glb:
            return None

        gltf, bin_data = _read_glb(glb)
        if not gltf:
            return None

        try:
            triangles = _triangles_from_gltf(gltf, bin_data, limit=max_triangles)
            break
        except _TooManyTriangles:
            tolerance *= 2.5

    if not triangles:
        return None

    vertex_map = {}
    vertices = []
    indices = []
    colors = []

    for p0, p1, p2, color in triangles:
        tri_indices = []
        for point in (p0, p1, p2):
            key = tuple(round(float(value), 5) for value in point)
            index = vertex_map.get(key)
            if index is None:
                index = len(vertices)
                vertex_map[key] = index
                vertices.append(list(key))
            tri_indices.append(index)
        indices.extend(tri_indices)
        colors.append([round(float(value), 4) for value in color])

    return {
        'vertices': vertices,
        'indices': indices,
        'colors': colors,
        'triangle_count': len(triangles),
    }


# ----------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------
def render_attachment_preview(filename: str, data: bytes, size: int = 256, layers=None):
    """按扩展名分派渲染器。不认识 / 依赖缺失时返回 None。

    layers 仅对 .kicad_mod 封装生效，用于切换 F.Cu / F.SilkS / F.Mask 等图层。
    """
    name = (filename or '').lower()

    # 3D CAD：STEP / IGES 走 cascadio（可选依赖）
    if name.endswith(('.step', '.stp')):
        return render_brep(data, size, file_type='step')

    if name.endswith(('.iges', '.igs')):
        return render_brep(data, size, file_type='iges')

    try:
        text = data.decode('utf-8', 'replace')
    except Exception:
        return None

    if name.endswith('.kicad_mod') or name.endswith('.pretty'):
        return render_footprint(text, size, layers=layers)

    if name.endswith('.kicad_sym'):
        # 尝试用文件名推断符号名（去掉扩展名）
        ref = filename.rsplit('.', 1)[0]
        img = render_symbol(text, size, ref)
        return img or render_symbol(text, size)

    return None
