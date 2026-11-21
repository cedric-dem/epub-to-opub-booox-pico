"""Convert every EPUB in sample_library/epub into an OPUB in sample_library/recreated_opub.

The OPUB layout was reverse-engineered from a reference file produced by
"EPUB Tools e-booka.online" (see sample_library/opub). It is a FlatBuffers based
container ("BFPE" magic):

    0x000  root table (magic, version, file size, spans to the index sections), padded to 0x100
           chapters: for each spine item a block (records, paragraph table, record index)
                     followed by a chapter header buffer
           table of contents entries               -> index section B
           computed styles                         -> index section D
           images (8-bit gray / 1-bit dithered)    -> index section E
           metadata key/value table                -> section A
           (chapter, paragraph) list               -> section F
           chapter header index                    -> section C
"""

import hashlib
import io
import os
import posixpath
import re
import struct
import sys
import uuid
import zipfile
import zlib
from html import unescape
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import flatbuffers
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT, "sample_library", "epub")
DST_DIR = os.path.join(ROOT, "sample_library", "recreated_opub")
ASSET_EXTERNAL_COVER = os.path.join(ROOT, "assets", "external_cover.bin")

MAGIC = 0x45504642  # "BFPE"
SCREEN_W, SCREEN_H = 480, 800
THUMBS = [(2, 256, 384), (3, 176, 264), (4, 128, 192)]  # (level, w, h)
TEXT_CHUNK = 200  # max bytes of UTF-8 per text record

# ---------------------------------------------------------------------------
# FlatBuffers helpers (field order mimics flatc generated "CreateX" functions:
# larger fields first, reverse declaration order within a size class)
# ---------------------------------------------------------------------------
_SIZE = {"u8": 1, "u16": 2}


def fb_table(b, fields):
    fields = [f for f in fields if f is not None]
    order = sorted(fields, key=lambda f: (-(8 if f[1] == "u64" else _SIZE.get(f[1], 4)), -f[0]))
    b.StartObject(max(f[0] for f in fields) + 1)
    for idx, kind, val in order:
        if kind == "u8":
            b.PrependUint8(val)
        elif kind == "u16":
            b.PrependUint16(val)
        elif kind == "u32":
            b.PrependUint32(val)
        elif kind == "u64":
            b.PrependUint64(val)
        elif kind == "off":
            b.PrependUOffsetTRelative(val)
        elif kind == "len":  # struct { float value; uint8 unit; }
            b.Prep(4, 8)
            b.Pad(3)
            b.PrependUint8(val[1])
            b.PrependFloat32(val[0])
        elif kind == "span":  # struct { uint32 offset; uint32 size; }
            b.Prep(4, 8)
            b.PrependUint32(val[1])
            b.PrependUint32(val[0])
        else:
            raise ValueError(kind)
        b.Slot(idx)
    return b.EndObject()


def fb_finish(b, root):
    b.Finish(root)
    return bytes(b.Output())


def rec_wrap(kind, build_inner):
    b = flatbuffers.Builder(0)
    inner = build_inner(b)
    return fb_finish(b, fb_table(b, [(0, "u8", kind), (1, "off", inner)]))


def rec_text(text, style):
    def inner(b):
        s = b.CreateString(text)
        return fb_table(b, [(0, "off", s), (1, "u32", style)])
    return rec_wrap(1, inner)


def rec_image(index, style):
    return rec_wrap(2, lambda b: fb_table(b, [(0, "u32", index), (1, "u32", style)]))


def rec_break():
    return rec_wrap(3, lambda b: fb_table(b, [(0, "u32", 0)]))


def rec_link(href, text, style, external):
    def inner(b):
        t = b.CreateString(text)
        run = fb_table(b, [(0, "off", t), (1, "u32", style)])
        u = b.CreateString(href)
        return fb_table(b, [
            (0, "u8", 1), (1, "off", run), (2, "u8", 3 if external else 1),
            (3, "u16", 0 if external else 5), (4, "u16", 0), (5, "u16", 0),
            (6, "u16", 0), (7, "u16", 0), (8, "off", u)])
    return rec_wrap(4, inner)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
class Rule:
    def __init__(self, parts, spec, order, decls):
        self.parts, self.spec, self.order, self.decls = parts, spec, order, decls


def parse_decls(text):
    out = {}
    for item in text.split(";"):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        k = k.strip().lower()
        v = v.replace("!important", "").strip()
        if k and v:
            out[k] = v
    return out


def parse_compound(s):
    m = re.match(r"^([a-zA-Z0-9_\-\*]*)((?:[.#][a-zA-Z0-9_\-]+)*)$", s)
    if not m:
        return None
    tag = m.group(1).lower()
    if tag == "*":
        tag = ""
    classes = re.findall(r"\.([a-zA-Z0-9_\-]+)", m.group(2))
    ids = re.findall(r"#([a-zA-Z0-9_\-]+)", m.group(2))
    return tag, classes, ids


def parse_css(text, rules):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"@[^{;]*;", "", text)
    for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        decls = parse_decls(body)
        for sel in sel_text.split(","):
            sel = sel.strip()
            if not sel or ":" in sel or "[" in sel or sel.startswith("@"):
                continue
            sel = re.sub(r"\s*>\s*", " > ", sel)
            toks = sel.split()
            parts, ok, comb = [], True, " "
            for t in toks:
                if t == ">":
                    comb = ">"
                    continue
                c = parse_compound(t)
                if c is None:
                    ok = False
                    break
                parts.append((comb, c))
                comb = " "
            if not ok or not parts:
                continue
            a = sum(len(c[2]) for _, c in parts)
            bb = sum(len(c[1]) for _, c in parts)
            cc = sum(1 for _, c in parts if c[0])
            rules.append(Rule(parts, (a, bb, cc), len(rules), decls))


def compound_match(node, c):
    tag, classes, ids = c
    if tag and node.tag != tag:
        return False
    if any(k not in node.classes for k in classes):
        return False
    if any(node.attrs.get("id") != i for i in ids):
        return False
    return True


def selector_match(node, parts):
    comb, c = parts[-1]
    if not compound_match(node, c):
        return False
    if len(parts) == 1:
        return True
    rest = parts[:-1]
    p = node.parent
    if comb == ">":
        return p is not None and selector_match(p, rest)
    while p is not None:
        if selector_match(p, rest):
            return True
        p = p.parent
    return False


# ---------------------------------------------------------------------------
# Style model (48 field slots, see Style.fields)
# ---------------------------------------------------------------------------
UNIT_PX, UNIT_PT, UNIT_EM, UNIT_PCT = 1, 2, 3, 5
DEFAULT_LH = (1.4664, UNIT_EM)
ZERO = (0.0, UNIT_EM)
BOX = ["margin-top", "margin-right", "margin-bottom", "margin-left",
       "padding-top", "padding-right", "padding-bottom", "padding-left"]
INHERITED = ["color", "font-style", "line-height", "text-align", "text-indent", "font-size"]
BLOCK_TAGS = {"html", "body", "p", "div", "section", "article", "aside", "header", "footer", "nav",
              "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "figure",
              "figcaption", "table", "tr", "td", "th", "tbody", "thead", "pre", "hr", "dl", "dt",
              "dd", "main", "address", "center"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"head", "script", "style", "title", "meta", "link"}


def parse_length(v):
    v = v.strip().lower()
    m = re.match(r"^(-?[0-9]*\.?[0-9]+)\s*(em|%|px|pt|rem)?$", v)
    if not m:
        if v in ("0", "auto", "normal"):
            return None
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("em", "rem"):
        return (num, UNIT_EM)
    if unit == "%":
        return (num, UNIT_PCT)
    if unit == "px":
        return (num, UNIT_PX)
    if unit == "pt":
        return (num, UNIT_PT)
    if num == 0:
        return (0.0, UNIT_EM)
    return (num, UNIT_EM)  # unitless line-height etc.


NAMED_COLORS = {"black": "000000", "white": "ffffff", "red": "ff0000", "green": "008000",
                "blue": "0000ff", "gray": "808080", "grey": "808080"}


def parse_color(v):
    v = v.strip().lower()
    if v in NAMED_COLORS:
        v = "#" + NAMED_COLORS[v]
    m = re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return bytes.fromhex(h) + b"\xff"
    m = re.match(r"^rgba?\(([^)]*)\)$", v)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        try:
            rgb = bytes(int(float(x)) & 255 for x in p[:3])
            return rgb + b"\xff"
        except ValueError:
            return None
    return None


def expand_box(prop, value):
    vals = value.split()
    if not vals:
        return {}
    if len(vals) == 1:
        vals = vals * 4
    elif len(vals) == 2:
        vals = [vals[0], vals[1], vals[0], vals[1]]
    elif len(vals) == 3:
        vals = [vals[0], vals[1], vals[2], vals[1]]
    sides = ["top", "right", "bottom", "left"]
    return {"%s-%s" % (prop, s): vals[i] for i, s in enumerate(sides[:4])}


def normalize_decls(decls):
    out = {}
    for k, v in decls.items():
        if k in ("margin", "padding"):
            out.update(expand_box(k, v))
        else:
            out[k] = v
    return out


def style_key(fields):
    return tuple(sorted(fields.items()))


def make_fields(props, block, body=False, with_box=True):
    """Build the slot->value dict of a style record from resolved properties."""
    f = {}
    f[2] = 1
    f[3] = props.get("color", b"\x00\x00\x00\xff")
    f[4] = 1
    f[5] = props.get("font-size", (1.0, UNIT_EM))
    f[6] = 1
    f[7] = 1
    f[8] = 1
    f[9] = 2 if props.get("font-style") == "italic" else 1
    f[10] = 1
    f[11] = 116 if body else 100
    f[14] = 1
    f[15] = ZERO
    f[16] = 1
    f[17] = props.get("line-height", DEFAULT_LH)
    f[18] = 1
    f[19] = props.get("text-align", 1)
    f[20] = 1
    f[21] = props.get("text-indent", ZERO)
    for i, name in enumerate(BOX):
        f[22 + 2 * i] = 1
        f[23 + 2 * i] = props.get(name, ZERO) if with_box else ZERO
    if with_box and "width" in props:
        f[38] = 1
        f[39] = props["width"]
    if with_box and "height" in props:
        f[40] = 1
        f[41] = props["height"]
    if block:
        f[46] = 1
        f[47] = 3
    return f


def norm_em(ln):
    """Inherited font sizes are stored relative (percent -> em)."""
    if ln and ln[1] == UNIT_PCT:
        return (ln[0] / 100.0, UNIT_EM)
    return ln


def padding_fields(props):
    """Partial style holding only the padding box (used for section/page entries)."""
    f = {}
    for i, name in enumerate(BOX[4:]):
        f[30 + 2 * i] = 1
        f[31 + 2 * i] = props.get(name, ZERO)
    return f


def encode_style(fields):
    b = flatbuffers.Builder(0)
    if 2 in fields:
        # Full styles: the reference writer builds an (unreferenced) sub table first.
        b.StartObject(1)
        b.PrependUint8(1)
        b.Slot(0)
        b.EndObject()
    flist = []
    for k, v in fields.items():
        if k == 3:
            flist.append((k, "u32", int.from_bytes(v, "little")))
        elif k == 11:
            flist.append((k, "u32", v))
        elif isinstance(v, tuple):
            flist.append((k, "len", v))
        else:
            flist.append((k, "u8", v))
    return fb_finish(b, fb_table(b, flist))


class StyleRegistry:
    def __init__(self):
        self.ids = {}
        self.list = []

    def get(self, fields):
        k = style_key(fields)
        if k not in self.ids:
            self.list.append(fields)
            self.ids[k] = len(self.list)  # 1-based
        return self.ids[k]


# ---------------------------------------------------------------------------
# DOM
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.classes = set((attrs.get("class") or "").split())
        self.children = []  # Node or str
        self.specified = {}
        self.props = {}
        self.block = False


def localname(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def to_nodes(el, parent):
    attrs = {localname(k): v for k, v in el.attrib.items()}
    n = Node(localname(el.tag), attrs, parent)
    if el.text:
        n.children.append(el.text)
    for c in el:
        if isinstance(c.tag, str):
            n.children.append(to_nodes(c, n))
        if c.tail:
            n.children.append(c.tail)
    return n


def parse_xhtml(data):
    text = data.decode("utf-8", "replace")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # tolerate HTML named entities
        text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)([a-zA-Z]+);",
                      lambda m: unescape("&%s;" % m.group(2)), text)
        root = ET.fromstring(text.encode("utf-8"))
    return to_nodes(root, None)


# ---------------------------------------------------------------------------
# Chapter conversion
# ---------------------------------------------------------------------------
WS = re.compile(r"[ \t\n\r\f]+")


class ChapterBuilder:
    def __init__(self, book, path, spine_id, data):
        self.book = book
        self.path = path
        self.dir = posixpath.dirname(path)
        self.id = spine_id
        self.root = parse_xhtml(data)
        self.title = ""
        self.rules = []
        self.paragraphs = []  # (kind, style, section, [records])
        self.cur = None
        self.section_entries = []  # (page style id, first paragraph)
        self.section_depth = 0
        self._load_head()

    def _load_head(self):
        head = next((c for c in self.root.children if isinstance(c, Node) and c.tag == "head"), None)
        if head is None:
            return
        for c in head.children:
            if not isinstance(c, Node):
                continue
            if c.tag == "title":
                self.title = "".join(x for x in c.children if isinstance(x, str)).replace("\xa0", " ").strip()
            elif c.tag == "link" and "stylesheet" in (c.attrs.get("rel") or ""):
                href = posixpath.normpath(posixpath.join(self.dir, unquote(c.attrs.get("href", ""))))
                css = self.book.read_text(href)
                if css is not None:
                    parse_css(css, self.rules)
            elif c.tag == "style":
                parse_css("".join(x for x in c.children if isinstance(x, str)), self.rules)

    # -- style resolution -------------------------------------------------
    def compute(self, node):
        matched = [r for r in self.rules if selector_match(node, r.parts)]
        matched.sort(key=lambda r: (r.spec, r.order))
        spec = {}
        for r in matched:
            spec.update(normalize_decls(r.decls))
        if node.attrs.get("style"):
            spec.update(normalize_decls(parse_decls(node.attrs["style"])))
        node.specified = spec
        disp = spec.get("display", "").strip().lower()
        node.block = disp in ("block", "list-item") or (node.tag in BLOCK_TAGS and disp not in ("inline", "inline-block"))
        parent = node.parent
        props = {}
        if parent is not None and parent.tag != "body" and parent.tag != "html":
            for k in INHERITED:
                if k in parent.props and not (k == "font-size" and node.block):
                    props[k] = norm_em(parent.props[k]) if k == "font-size" else parent.props[k]
        if node.tag == "a" and node.attrs.get("href") is not None:
            props["color"] = b"\xff\x00\x00\xff"
        for k, v in spec.items():
            if k == "color":
                c = parse_color(v)
                if c:
                    props[k] = c
            elif k == "font-style":
                props[k] = "italic" if v.strip().lower() in ("italic", "oblique") else "normal"
            elif k == "text-align":
                props[k] = {"left": 1, "start": 1, "right": 2, "end": 2, "center": 3,
                            "justify": 4}.get(v.strip().lower(), props.get(k, 1))
            elif k in ("font-size", "line-height", "text-indent", "width", "height") or k in BOX:
                if v.strip().lower() == "inherit":
                    continue
                ln = parse_length(v)
                if ln is not None:
                    props[k] = ln
            elif k == "page-break-inside":
                props[k] = v.strip().lower()
        if node.tag == "body":
            for k in BOX[:4]:
                props.pop(k, None)
        node.props = props

    def block_fields(self, node):
        return make_fields(node.props, True, body=node.tag == "body" and bool(self.rules))

    def text_fields(self, node):
        if node.block:
            props = dict(node.props)
            if "font-size" in props:
                props["font-size"] = norm_em(props["font-size"])
            return make_fields(props, True, with_box=False)
        return make_fields(node.props, False, with_box=False)

    def inline_fields(self, node):
        return make_fields(node.props, False, with_box=False)

    # -- paragraph assembly ----------------------------------------------
    def flush(self):
        if self.cur is None:
            return
        kind, style, sect, segs = self.cur
        self.cur = None
        recs = finalize_segments(segs)
        if recs:
            self.paragraphs.append((kind, style, sect, recs))

    def ensure_para(self, block, style=None):
        if self.cur is None:
            kind = 2 if block.tag in HEADINGS else 1
            if style is None:
                style = self.book.styles.get(self.block_fields(block))
            self.cur = (kind, style, 1 if self.section_depth else 0, [])
        return self.cur[3]

    def build(self):
        body = next((c for c in self.root.children if isinstance(c, Node) and c.tag == "body"), None)
        if body is None:
            return
        self.walk(body, body)
        self.flush()

    def walk(self, node, block):
        self.compute(node)
        if node.tag in SKIP_TAGS:
            return
        if node.tag == "img" or node.tag == "image":
            self.flush()
            src = node.attrs.get("src") or node.attrs.get("href")
            idx = self.book.image_index(posixpath.normpath(posixpath.join(self.dir, unquote(src or ""))))
            # without a stylesheet the image paragraph takes the enclosing block's style
            pst = None if self.rules else self.book.styles.get(self.text_fields(block))
            st = self.book.styles.get(self.inline_fields(node))
            if idx is not None:
                pst = pst or st
                self.paragraphs.append((1, pst, 1 if self.section_depth else 0, [rec_image(idx, st)]))
            return
        if (node.attrs.get("type") or "").strip() == "pagebreak":
            return  # print page markers carry no content
        if node.tag == "br":
            if getattr(block, "container", False) and self.cur is None:
                # a break directly inside a block that holds other blocks opens an
                # anonymous block (field 47 = 2)
                anon = self.text_fields(block)
                anon[47] = 2
                self.ensure_para(block, self.book.styles.get(anon))
            self.ensure_para(block).append(("br",))
            return
        if node.block:
            self.flush()
            kids = [c for c in node.children if isinstance(c, Node)]
            for c in kids:
                self.compute(c)
            container = any(c.block or c.tag in ("img", "image") for c in kids)
            node.container = container
            if not container:
                # paragraph-like blocks register their style right away,
                # containers only once they receive direct text
                self.book.styles.get(self.block_fields(node))
            is_section = node.tag == "section"
            if is_section:
                self.section_depth += 1
                self.section_entries.append(
                    (self.book.styles.get(padding_fields(self.page_padding(node))), len(self.paragraphs)))
            text_style = None
            for c in node.children:
                if isinstance(c, str):
                    if node.tag == "body" and not self.rules:
                        continue
                    if not WS.sub("", c):
                        if self.cur is None:
                            self.book.styles.get(self.block_fields(node))
                            continue
                    elif text_style is None:
                        text_style = self.book.styles.get(self.text_fields(node))
                    self.ensure_para(node).append(("text", c, text_style))
                else:
                    self.walk(c, node)
            self.flush()
            if is_section:
                self.section_depth -= 1
            return
        # inline element
        if node.tag == "a" and node.attrs.get("href") is not None:
            self.ensure_para(block)
            st = self.book.styles.get(self.inline_fields(node))
            for c in node.children:
                if isinstance(c, Node):
                    self.compute_tree(c)
            txt = collect_text(node)
            href = node.attrs["href"]
            ext = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", href))
            self.ensure_para(block).append(("link", txt, st, href, ext))
            return
        st = self.book.styles.get(self.inline_fields(node))
        for c in node.children:
            if isinstance(c, str):
                if not WS.sub("", c) and self.cur is None:
                    continue
                self.ensure_para(block).append(("text", c, st))
            else:
                self.walk(c, block)

    def page_padding(self, node):
        """Padding of the first element (section, then its first block descendants)
        that defines a non-zero top padding; falls back to the section's own."""
        n = node
        while n is not None:
            pt = n.props.get("padding-top")
            if pt and pt[0] != 0:
                return n.props
            nxt = None
            for c in n.children:
                if isinstance(c, Node):
                    self.compute(c)
                    if c.block:
                        nxt = c
                        break
            n = nxt
        return node.props

    def compute_tree(self, node):
        self.compute(node)
        self.book.styles.get(self.inline_fields(node))
        for c in node.children:
            if isinstance(c, Node):
                self.compute_tree(c)


def collect_text(node):
    out = []
    for c in node.children:
        out.append(c if isinstance(c, str) else collect_text(c))
    return "".join(out)


def finalize_segments(segs):
    """Collapse whitespace like the reference writer: a space that ends a run is moved
    to the start of the following run; leading/trailing paragraph space is dropped."""
    items = []
    for s in segs:
        if s[0] in ("text", "link"):
            t = WS.sub(" ", s[1])
            items.append([s[0], t] + list(s[2:]))
        else:
            items.append(list(s))
    # whitespace moving
    pending_space = False
    at_line_start = True
    out = []
    for it in items:
        if it[0] in ("br",):
            pending_space = False
            at_line_start = True
            out.append(it)
            continue
        if it[0] == "img":
            out.append(it)
            continue
        t = it[1]
        if t.startswith(" "):
            pending_space = True
            t = t.lstrip(" ")
        trailing = t.endswith(" ")
        t = t.rstrip(" ")
        if not t:
            if it[0] == "link":
                pass
            continue
        if pending_space and not at_line_start:
            t = " " + t
        pending_space = trailing
        at_line_start = False
        it[1] = t
        out.append(it)
    # drop trailing breaks
    while out and out[-1][0] == "br":
        out.pop()
    recs = []
    for it in out:
        if it[0] == "text":
            for chunk in split_text(it[1]):
                recs.append(rec_text(chunk, it[2]))
        elif it[0] == "link":
            recs.append(rec_link(it[3], it[1], it[2], it[4]))
        elif it[0] == "br":
            recs.append(rec_break())
        elif it[0] == "img":
            recs.append(rec_image(it[1], it[2]))
    return recs


def split_text(t):
    """Greedily pack word / non-word tokens into chunks of at most TEXT_CHUNK UTF-8 bytes."""
    out, cur, n = [], [], 0
    for tok in re.findall(r"\w+|\W", t):
        tl = len(tok.encode("utf-8"))
        if cur and n + tl > TEXT_CHUNK:
            out.append("".join(cur))
            cur, n = [], 0
        while tl > TEXT_CHUNK:  # very long word: hard split on character boundary
            part = tok
            while len(part.encode("utf-8")) > TEXT_CHUNK:
                part = part[:-1]
            out.append(part)
            tok = tok[len(part):]
            tl = len(tok.encode("utf-8"))
        cur.append(tok)
        n += tl
    if cur:
        out.append("".join(cur))
    return out


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def gray_fit(im):
    """8-bit grayscale, shrunk (nearest neighbour, like the reference) to fit the screen.
    The reference also applies a per-image gamma whose selection rule is unknown; we keep 1.0."""
    im = im.convert("L")
    w, h = im.size
    scale = min(SCREEN_W / w, SCREEN_H / h, 1.0)
    if scale < 1.0:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.NEAREST)
    return im


def atkinson(im):
    """Atkinson error diffusion to 1 bit (closest match to the reference thumbnails)."""
    w, h = im.size
    px = [list(im.getdata())[y * w:(y + 1) * w] for y in range(h)]
    rows = [[float(v) for v in r] for r in px]
    out = Image.new("1", (w, h))
    op = out.load()
    for y in range(h):
        r0 = rows[y]
        r1 = rows[y + 1] if y + 1 < h else None
        r2 = rows[y + 2] if y + 2 < h else None
        for x in range(w):
            v = r0[x]
            nv = 255 if v >= 128 else 0
            op[x, y] = nv
            e = (v - nv) / 8.0
            if x + 1 < w:
                r0[x + 1] += e
            if x + 2 < w:
                r0[x + 2] += e
            if r1 is not None:
                if x > 0:
                    r1[x - 1] += e
                r1[x] += e
                if x + 1 < w:
                    r1[x + 1] += e
            if r2 is not None:
                r2[x] += e
    return out


def cover_thumb(gray, w, h):
    """Fill (w, h) with the grayscale cover, centre-crop, then dither to 1 bit."""
    sc = max(w / gray.width, h / gray.height)
    nw, nh = max(w, round(gray.width * sc)), max(h, round(gray.height * sc))
    r = gray.resize((nw, nh), Image.NEAREST)
    l, t = (nw - w) // 2, (nh - h) // 2
    return atkinson(r.crop((l, t, l + w, t + h)))


def encode_image(w, h, level, fmt, data):
    b = flatbuffers.Builder(0)
    v = b.CreateByteVector(data)
    inner = fb_table(b, [(0, "off", v)])
    return fb_finish(b, fb_table(b, [
        (0, "u8", 2), (1, "off", inner), (2, "u8", 1), (3, "u8", 1), (4, "u32", w),
        (5, "u32", h), (6, "u8", level), (7, "u8", fmt), (8, "u8", 1)]))


def gray_blob(im, level=1):
    return encode_image(im.width, im.height, level, 4, im.tobytes())


def mono_blob(im, level):
    return encode_image(im.width, im.height, level, 1, im.tobytes())


def external_cover_blobs():
    raw = zlib.decompress(open(ASSET_EXTERNAL_COVER, "rb").read())
    out, p = [], 0
    while p < len(raw):
        w, h, lvl, fmt, n = struct.unpack_from("<IIBBI", raw, p)
        p += 14
        out.append(encode_image(w, h, lvl, fmt, raw[p:p + n]))
        p += n
    return out


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------
OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


class Book:
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())
        self.styles = StyleRegistry()
        self.images = []  # zip paths in order of first use
        self._read_opf()

    def read(self, name):
        return self.zip.read(name) if name in self.names else None

    def read_text(self, name):
        d = self.read(name)
        return d.decode("utf-8", "replace") if d is not None else None

    def _read_opf(self):
        cont = ET.fromstring(self.zip.read("META-INF/container.xml"))
        rf = next(e for e in cont.iter() if localname(e.tag) == "rootfile")
        self.opf_path = rf.attrib["full-path"]
        self.opf_dir = posixpath.dirname(self.opf_path)
        opf = ET.fromstring(self.zip.read(self.opf_path))
        self.meta = {}
        self.manifest = {}
        self.spine = []
        cover_id = None
        for e in opf.iter():
            ln = localname(e.tag)
            if ln in ("title", "creator", "publisher", "language") and e.text and ln not in self.meta:
                self.meta[ln] = e.text.strip()
            elif ln == "identifier" and e.text and "isbn" in e.text.lower() and "isbn" not in self.meta:
                self.meta["isbn"] = e.text.strip()
            elif ln == "meta" and e.attrib.get("name") == "cover":
                cover_id = e.attrib.get("content")
            elif ln == "item":
                href = posixpath.normpath(posixpath.join(self.opf_dir, unquote(e.attrib["href"])))
                self.manifest[e.attrib["id"]] = (href, e.attrib.get("media-type", ""), e.attrib.get("properties", ""))
            elif ln == "itemref":
                if e.attrib.get("linear", "yes") != "no" or True:
                    self.spine.append(e.attrib["idref"])
        self.cover_image = None
        if cover_id and cover_id in self.manifest:
            self.cover_image = self.manifest[cover_id][0]
        for k, (h, mt, props) in self.manifest.items():
            if "cover-image" in props:
                self.cover_image = h
        self.nav_path = next((h for h, mt, p in self.manifest.values() if "nav" in p.split()), None)

    def image_index(self, path):
        if path not in self.names:
            return None
        if path not in self.images:
            self.images.append(path)
        return self.images.index(path)

    # -- table of contents -------------------------------------------------
    def toc_entries(self, spine_paths):
        if not self.nav_path:
            return []
        root = parse_xhtml(self.read(self.nav_path))
        nav_dir = posixpath.dirname(self.nav_path)
        entries = []
        navs = []

        def find(n):
            if isinstance(n, Node):
                if n.tag == "nav":
                    navs.append(n)
                for c in n.children:
                    find(c)
        find(root)
        nav = next((n for n in navs if "toc" in (n.attrs.get("type") or "")), navs[0] if navs else None)
        if nav is None:
            return []
        heading = None

        def first_heading(n):
            for c in n.children:
                if isinstance(c, Node):
                    if c.tag in HEADINGS:
                        return WS.sub(" ", collect_text(c)).strip()
                    r = first_heading(c) if c.tag not in ("ol", "ul") else None
                    if r:
                        return r
            return None
        heading = first_heading(nav) or "Table of Contents"
        entries.append((heading, posixpath.relpath(self.nav_path, self.opf_dir), None, None))

        def walk_list(n, level):
            for li in n.children:
                if not isinstance(li, Node) or li.tag != "li":
                    continue
                a = next((c for c in li.children if isinstance(c, Node) and c.tag in ("a", "span")), None)
                if a is not None:
                    href = a.attrs.get("href", "")
                    title = WS.sub(" ", collect_text(a).replace("\xa0", " ")).strip()
                    target = posixpath.normpath(posixpath.join(nav_dir, unquote(href.split("#")[0]))) if href else None
                    ch = spine_paths.index(target) if target in spine_paths else None
                    entries.append((title, href, level, ch))
                for c in li.children:
                    if isinstance(c, Node) and c.tag in ("ol", "ul"):
                        walk_list(c, level + 1)

        for c in nav.children:
            if isinstance(c, Node) and c.tag in ("ol", "ul"):
                walk_list(c, 1)
        return entries


def encode_toc(title, href, level, ch):
    b = flatbuffers.Builder(0)
    t = b.CreateString(title)
    h = b.CreateString(href)
    f = [(0, "off", t), (1, "off", h)]
    if level is not None:
        f.append((2, "u16", level))
    if ch is not None:
        f.append((3, "u16", ch))
    return fb_finish(b, fb_table(b, f))


def encode_chapter_header(cid, title, spans, npara, nrec, nsect):
    b = flatbuffers.Builder(0)
    i = b.CreateString(cid)
    t = b.CreateString(title)
    f = [(0, "off", i), (1, "off", t)] + [(3 + k, "span", s) for k, s in enumerate(spans)]
    f += [(8, "u32", npara), (9, "u32", nrec)]
    if nsect:
        f.append((11, "u32", nsect))
    return fb_finish(b, fb_table(b, f))


def encode_metadata(pairs):
    b = flatbuffers.Builder(0)
    offs = []
    for k, v in pairs:
        ko = b.CreateString(k)
        vo = b.CreateString(v)
        offs.append(fb_table(b, [(0, "off", ko), (1, "off", vo)]))
    b.StartVector(4, len(offs), 4)
    for o in reversed(offs):
        b.PrependUOffsetTRelative(o)
    vec = b.EndVector()
    return fb_finish(b, fb_table(b, [(0, "off", vec)]))


def encode_root(spans, file_size):
    A, B, C, D, E, F = spans
    b = flatbuffers.Builder(0)
    root = fb_table(b, [(0, "u32", MAGIC), (1, "u16", 0x300), (2, "u64", file_size), (3, "u8", 1),
                        (4, "span", A), (5, "span", B), (6, "span", C), (7, "span", D),
                        (8, "span", E), (11, "span", F)])
    out = fb_finish(b, root)
    return out


class Writer:
    def __init__(self):
        self.buf = bytearray()

    def align(self, n):
        while len(self.buf) % n:
            self.buf.append(0)

    def put(self, data, align=8):
        self.align(align)
        off = len(self.buf)
        self.buf += data
        return off


def u32pairs(pairs):
    return b"".join(struct.pack("<II", a, b) for a, b in pairs)


def convert(epub_path, out_path):
    book = Book(epub_path)
    w = Writer()
    w.buf += b"\x00" * 0x100

    spine_paths = [book.manifest[i][0] for i in book.spine if i in book.manifest]
    chapters_hdr = []
    para_index = []
    for ci, sid in enumerate(book.spine):
        if sid not in book.manifest:
            continue
        path = book.manifest[sid][0]
        data = book.read(path)
        if data is None:
            continue
        ch = ChapterBuilder(book, path, sid, data)
        ch.build()
        # --- block ---
        block = bytearray()
        rec_pairs = []
        paras = []
        for kind, style, sect, recs in ch.paragraphs:
            first = len(rec_pairs)
            for r in recs:
                while len(block) % 8:
                    block.append(0)
                rec_pairs.append((len(block), len(r)))
                block += r
            paras.append((kind, style, sect, first, len(recs)))
        recs_end = len(block)
        paras_off = len(block)
        for p in paras:
            block += struct.pack("<5I", *p)
        f6 = (len(block), 0)
        while len(block) % 8:
            block.append(0)
        idx_off = len(block)
        idx_pairs = rec_pairs + ch.section_entries
        block += u32pairs(idx_pairs)
        block_off = w.put(bytes(block))
        spans = [(block_off, len(block)), (paras_off, 20 * len(paras)), (0, recs_end), f6,
                 (idx_off, 8 * len(idx_pairs))]
        hdr = encode_chapter_header(sid, ch.title, spans, len(paras), len(rec_pairs), len(ch.section_entries))
        hoff = w.put(hdr, 4)
        chapters_hdr.append((hoff, len(hdr)))
        para_index += [(len(chapters_hdr) - 1, k) for k in range(len(paras))]

    # --- table of contents ---
    toc = []
    for e in book.toc_entries(spine_paths):
        enc = encode_toc(*e)
        toc.append((w.put(enc), len(enc)))
    w.align(8)
    B = (w.put(u32pairs(toc)), 8 * len(toc))

    # --- styles ---
    st = []
    for f in book.styles.list:
        enc = encode_style(f)
        st.append((w.put(enc), len(enc)))
    w.align(8)
    D = (w.put(u32pairs(st)), 8 * len(st))

    # --- images ---
    blobs = []
    for p in book.images:
        im = Image.open(io.BytesIO(book.read(p)))
        im.load()
        blobs.append(gray_blob(gray_fit(im)))
    meta_imgs = []
    if book.cover_image and book.read(book.cover_image) is not None:
        cim = Image.open(io.BytesIO(book.read(book.cover_image)))
        cim.load()
        base = len(blobs)
        cgray = gray_fit(cim)
        blobs.append(gray_blob(cgray))
        for lvl, tw, th in THUMBS:
            blobs.append(mono_blob(cover_thumb(cgray, tw, th), lvl))
        meta_imgs = [("cover-origin", base), ("cover-middle", base + 1), ("cover-small", base + 2),
                     ("cover-tiny", base + 3)]
    base = len(blobs)
    blobs += external_cover_blobs()
    meta_imgs += [("external-cover-origin", base), ("external-cover-middle", base + 1),
                  ("external-cover-small", base + 2), ("external-cover-tiny", base + 3)]
    epairs = []
    for i, blob in enumerate(blobs):
        name = ("img_%04d" % (i + 1)).encode()
        w.put(struct.pack("<I", len(name)) + name + b"\x00" * 4, 8)
        epairs.append((w.put(blob, 8), len(blob)))
    E = (w.put(u32pairs(epairs), 4), 8 * len(epairs))

    # --- metadata ---
    md5 = hashlib.md5(open(epub_path, "rb").read()).hexdigest()
    pairs = [("bookId", "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, md5)), ("md5sum", md5)]
    for k in ("isbn", "creator", "publisher", "title", "language"):
        if k in book.meta:
            pairs.append((k, book.meta[k]))
    pairs += [(k, str(v)) for k, v in meta_imgs]
    meta = encode_metadata(pairs)
    A = (w.put(meta, 4), len(meta))

    F = (w.put(u32pairs(para_index), 4), 8 * len(para_index))
    C = (w.put(u32pairs(chapters_hdr), 4), 8 * len(chapters_hdr))

    root = encode_root((A, B, C, D, E, F), len(w.buf))
    assert len(root) <= 0x100
    w.buf[0:len(root)] = root
    with open(out_path, "wb") as fh:
        fh.write(w.buf)
    return len(w.buf)


def main():
    os.makedirs(DST_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(SRC_DIR) if f.lower().endswith(".epub"))
    if not files:
        print("no epub files in", SRC_DIR)
    for f in files:
        src = os.path.join(SRC_DIR, f)
        dst = os.path.join(DST_DIR, os.path.splitext(f)[0] + ".opub")
        try:
            n = convert(src, dst)
            print("converted %s -> %s (%d bytes)" % (f, os.path.relpath(dst, ROOT), n))
        except Exception as e:  # keep going with the other books
            print("FAILED %s: %s" % (f, e), file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
