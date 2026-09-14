#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / build_dual.py

用「原文页 + 译文页」拼出左右双栏对照 PDF。
译文页 = 原页面抹掉待译文本块的字形 -> 原位插入中文（字号自适应缩小）。

用法:
  python build_dual.py --source src.pdf --blocks blocks.json --translations trans.json \
      --output dual.pdf [--pages 1-3] [--preview preview.png]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import config as PDT  # 统一的可配置路径层（可迁移的关键）
except Exception:          # 单独把脚本拷出去用也不能崩
    PDT = None

# 候选 CJK 字体（按优先级）
# ⚠️ 必须用"文字能正确提取"的字体：Noto Serif/Sans SC、SourceHanSerif 系列经
#    PyMuPDF 嵌入后 ToUnicode 会指向 CJK 兼容区码位（器→U+FA38、力→U+F98A），
#    导致生成的 PDF 无法搜索/复制。需靠 verify_font() 自检拦住。
#    实测正常：华文宋体/中宋、simsun、Deng、msyh、simhei。
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\STSONG.TTF",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\STKAITI.TTF",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]

# 粗体/标题用字体（中文排版惯例：正文宋体、标题中宋/黑体）
CJK_BOLD_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\STZHONGS.TTF",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\Dengb.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/Supplemental/Heiti.ttc",
]

if PDT is not None:
    CJK_FONT_CANDIDATES = [str(p) for p in PDT.FONT_CANDIDATES] + CJK_FONT_CANDIDATES
    CJK_BOLD_FONT_CANDIDATES = [str(p) for p in PDT.FONT_BOLD_CANDIDATES] + CJK_BOLD_FONT_CANDIDATES

MIN_FONT_SIZE = 4.5
LINE_HEIGHT_RATIO = 1.28

# 首行缩进（中文排版习惯）：段落首行空两格。
# 判定以上下文续句信号为主：同列/跨列/跨页的上一个已译块若句未完 → 段中接续不缩；
# 标题、列表项、图表题注不缩；标题后的块必是新段落（缩进）。
_LIST_START = re.compile(r"^\s*(?:[0-9]{1,2}[.、）)]|[a-zA-Z][.、)]|[（(][0-9a-zA-Z]{1,2}[）)]|[-•·▪–]\s)")
_CAPTION_START = re.compile(r"^\s*(?:图|表|式|算法|[Ff]ig|[Tt]able|[Aa]lgorithm|[Ee]q)")
_TERM_TAIL = ("。", "！", "？", "…", "。”", ".”", ".")
_INDENT = "\u3000\u3000"


def indent_prefix(zh: str, prev_tail: str | None) -> str:
    """返回段首缩进（两个全角空格）或不缩进的空串。"""
    if len(zh) < 30:
        return ""                                  # 短块（单行/题注）不缩
    if _LIST_START.match(zh) or _CAPTION_START.match(zh):
        return ""                                  # 列表项、图表题注不缩
    if prev_tail is not None and not prev_tail.rstrip().endswith(_TERM_TAIL):
        return ""                                  # 上一块句未完：段中接续不缩
    return _INDENT


def _carry_from_page(blks: list, trans: dict, page_width: float) -> str | None:
    """按阅读序取某页最后一个已译非标题块的译文（页尾是标题则 None）。

    --pages 子集重建时上一页不在本轮处理范围，主循环的 carry_tail 断档，
    用它从上一页的已译块补算「续接信号」，避免跨页续段的首块被误加首行缩进。
    """
    cand = [b for b in blks
            if (trans.get(b["id"]) or {}).get("zh", "").strip()
            and not b.get("nested_in")]
    if not cand:
        return None
    cand.sort(key=lambda b: (b.get("flow_index", 10**9),
                              b["bbox"][0] > page_width / 2, b["bbox"][1], b["bbox"][0]))
    tail: str | None = None
    ended_head = False
    for b in cand:
        if b.get("heading") or b.get("bold"):
            ended_head = True
        else:
            ended_head = False
            tail = trans[b["id"]]["zh"]
    return None if ended_head else tail

# 字体自检探针：坏字体下这些字会被映射到 CJK 兼容区码位
_FONT_PROBE = "安全过滤器 潜力 远离 车辆 策略 换道 匝道 合流"
_FONT_PROBE_CHARS = "器力远"


def verify_font(path) -> bool:
    """插入探针文本再读回，确认 ToUnicode 正常（否则生成的 PDF 不可搜索）。"""
    if not path or not Path(path).is_file():
        return False
    try:
        import fitz
    except ImportError:
        return False
    try:
        doc = fitz.open()
        page = doc.new_page(width=520, height=60)
        page.insert_textbox(fitz.Rect(5, 5, 515, 55), _FONT_PROBE,
                            fontsize=9, fontname="probe", fontfile=str(path))
        got = page.get_text("text") or ""
        doc.close()
        return all(c in got for c in _FONT_PROBE_CHARS)
    except Exception:
        return False


def find_cjk_font(explicit: str | None = None, bold: bool = False, verify: bool = True) -> Path | None:
    cands = CJK_BOLD_FONT_CANDIDATES if bold else CJK_FONT_CANDIDATES
    if explicit and Path(explicit).is_file():
        if not verify or verify_font(explicit):
            return Path(explicit)
        print(f"⚠️ 指定字体文字提取异常（PDF 将不可搜索），已忽略: {explicit}")
    for c in cands:
        if Path(c).is_file() and (not verify or verify_font(c)):
            return Path(c)
    for c in cands:
        if Path(c).is_file():
            print(f"⚠️ 没有通过自检的字体，兜底使用（PDF 文字可能无法搜索）: {c}")
            return Path(c)
    return None


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec or spec.lower() == "all":
        return list(range(total))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a) - 1, int(b)))
        elif part:
            out.add(int(part) - 1)
    return sorted(i for i in out if 0 <= i < total)


def int_to_rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


def cell_font_size(page, rect) -> float | None:
    """量出某个矩形区域内最主要的字号（按字符数加权）。"""
    sizes: dict[float, int] = {}
    try:
        d = page.get_text("dict", clip=rect)
    except Exception:
        return None
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                n = len(s.get("text", ""))
                if n:
                    k = round(float(s.get("size", 0)), 1)
                    sizes[k] = sizes.get(k, 0) + n
    if not sizes:
        return None
    return max(sizes.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------- 公式排版
# 译文里的数学记号（ASCII 字母 / 希腊字母）本身能被 STSong 显示，但样式是正体，
# 与原文公式的"数学斜体"不一致。做法：渲染时把这些记号包进 <span class="m">，
# 由 CSS 换成数学字体 + 斜体。**不改动字符本身**，所以复制/搜索仍然正确。

_MATH_LETTERS = "SAPRDEFQLMNK"
_MATH_ALNUM_CLASS = "A-Za-z0-9_"


# 数学记号模式：需要真排版（下标/上标）的 ASCII 记号
_MATH_MARKUP = re.compile(r"[A-Za-zΑ-Ωα-ω]_[A-Za-z0-9]|[A-Za-z0-9)]\^")
_MATH_SUB = re.compile(r"([A-Za-zΑ-Ωα-ω])_([A-Za-z0-9]{1,15}(?:,[A-Za-z0-9]{1,3})?)")
_MATH_SUP = re.compile(r"([A-Za-z0-9Α-Ωα-ω])\^\(?([A-Za-z0-9+\-−]{1,6})\)?")


def mark_math(text: str) -> str:
    """把数学记号转成真排版的 html：下标/上标用 <sub>/<sup>，斜体用 .m span。

    例：N_p -> N(斜体)+下标 p；x^2 -> x²；a_f,t -> a 下标 f,t；U_cooperative 同理。
    只处理“上下文明确是数学符号”的情况，避免误伤：
    - 单独的大写数学字母，但排除紧跟在 （ ( 之后的（那是图的子图标签 （A）（B）…）
      以及被更长单词包含的（DuSA 里的 S、RL、DQN 等）
    - 希腊字母
    """
    out = _MATH_SUB.sub(
        lambda m: f'<span class="m">{m.group(1)}</span><sub>{m.group(2)}</sub>', text)
    out = _MATH_SUP.sub(
        lambda m: f'<span class="m">{m.group(1)}</span><sup>{m.group(2)}</sup>', out)
    # 希腊字母斜体
    out = re.sub(r"([Α-Ωα-ω])", r'<span class="m">\1</span>', out)
    # 单独的大写数学字母（排除标签边界与子图标签 （A）（B）…）
    out = re.sub(rf"(?<![{_MATH_ALNUM_CLASS}（(<])([{_MATH_LETTERS}])(?![a-z0-9A-Z>(])",
                 r'<span class="m">\1</span>', out)
    return out


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CJK_OR_PUNCT = r"㐀-鿿，。；：！？、（）《》【】"


def normalize_translation_text(text: str) -> str:
    """构建前移除译文中的硬换行，让每个文本块按 bbox 自然回流。"""
    t = re.sub(r"[ 	\r\n\f\v]+", " ", str(text or "")).strip()
    t = re.sub(rf"(?<=[{_CJK_OR_PUNCT}]) +", "", t)
    t = re.sub(rf" +(?=[{_CJK_OR_PUNCT}])", "", t)
    return t


def needs_fallback(text: str, base_font) -> bool:
    """基础字体盖不住的字符（数学字形等）需要走 html 渲染做字体回退。"""
    if base_font is None:
        return False
    try:
        return any(not base_font.has_glyph(ord(c)) for c in set(text) if not c.isspace())
    except Exception:
        return False


def build_css(font_basename: str, size: float, align: int, line_height: float,
              math_basename: str | None = None) -> str:
    al = ("left", "center", "right", "justify")[align]
    css = (f"@font-face{{font-family:base;src:url({font_basename})}}"
           f"body{{font-family:base;font-size:{size}pt;line-height:{line_height};"
           f"text-align:{al};margin:0}}"
           f".m{{font-style:italic}}")
    if math_basename:
        css += (f"@font-face{{font-family:math;src:url({math_basename})}}"
                f".m{{font-family:math;font-style:italic}}")
    return css


def insert_html_fitted(page, rect, text, arch, font_basename, math_basename,
                       color, start_size, align, line_height):
    """用 insert_htmlbox 排版：自动做字体回退 + 逐级缩小直到放得下。

    arch 是已挂好正文/数学字体所在目录的 fitz.Archive（跨目录 @font-face 靠它解析）。
    返回**实际渲染字号**：scale_low=0.8 允许 htmlbox 把内容整体缩到 0.8× 以塞进
    矩形，此时按 size × scale 汇报，字号统计与兜底判定才不失真。
    """
    body = mark_math(html_escape(text))
    size = float(start_size)
    while size >= MIN_FONT_SIZE:
        css = build_css(font_basename, size, align, line_height, math_basename)
        try:
            rc = page.insert_htmlbox(rect, body, css=css,
                                     archive=arch, scale_low=0.8)
        except Exception:
            rc = None
        ok = False
        scale = 1.0
        if isinstance(rc, (tuple, list)):
            if len(rc) >= 1 and rc[0] >= 0:
                ok = True
            if len(rc) >= 2:
                try:
                    scale = min(1.0, max(1e-3, float(rc[1])))
                except Exception:
                    scale = 1.0
        elif isinstance(rc, (int, float)) and rc >= 0:
            ok = True                # 旧版 API 只返回 spare_height，无法得知缩放
        if ok:
            return size * scale
        size -= 0.3
    return None


def insert_fitted(page, rect, text, fontname, fontfile, color, start_size, align=0):
    """在 rect 内插入文本，自动缩小字号直到放得下。返回实际字号或 None。

    insert_textbox 放不下时不会写出任何内容并返回负数，所以循环是安全的。
    """
    size = float(start_size)
    while size >= MIN_FONT_SIZE:
        rc = page.insert_textbox(
            rect, text,
            fontsize=size, fontname=fontname, fontfile=str(fontfile),
            color=color, align=align, lineheight=LINE_HEIGHT_RATIO,
            overlay=True,
        )
        if rc >= 0:
            return size
        size -= 0.3
    return None


def validate_translation_hashes(blocks_data: dict, trans: dict):
    expected = {b["id"]: b.get("source_hash")
                for p in blocks_data.get("pages", []) for b in p.get("blocks", [])}
    mismatch = []
    unverified = 0
    for bid, value in (trans or {}).items():
        if not isinstance(value, dict):
            continue
        if not ((value.get("zh") or "").strip() or value.get("skip") or value.get("blank")):
            continue
        want = expected.get(bid)
        got = value.get("source_hash")
        if want and got and want != got:
            mismatch.append((bid, want, got))
        elif want and not got:
            unverified += 1
    return mismatch, unverified


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("build_dual")
    ap.add_argument("--source", required=True, help="英文原文 PDF")
    ap.add_argument("--blocks", required=True, help="extract_blocks.py 产出的 blocks.json")
    ap.add_argument("--translations", required=True, help="译文 JSON: {block_id: {\"zh\": ...} | {\"skip\": true}}")
    ap.add_argument("--output", required=True, help="输出双栏 PDF")
    ap.add_argument("--pages", default="all", help="只处理这些页（1-based）；默认全部")
    ap.add_argument("--font", default=None, help="CJK 字体路径")
    ap.add_argument("--preview", default=None, help="导出预览 PNG")
    ap.add_argument("--preview-pages", default="1", help="预览哪些页（译文的页号，1-based）")
    ap.add_argument("--preview-zoom", type=float, default=1.6)
    ap.add_argument("--align", type=int, default=0, help="0=左对齐 1=居中 3=两端")
    ap.add_argument("--no-bold", action="store_true", help="粗体块也用正文字体")
    ap.add_argument("--tables", default=None, help="extract_tables.py 产出的 tables.json")
    ap.add_argument("--table-dict", default=None,
                    help='表格单元格译文，格式 {"原单元格文本": "中文"}（同名文本共用一条）')
    ap.add_argument("--no-table", action="store_true", help="跳过表格翻译")
    ap.add_argument("--table-align", type=int, default=None,
                    help="表格单元格对齐：0=左 1=居中；默认按格子宽度自动决定")
    ap.add_argument("--allow-rotated", action="store_true",
                    help="源含旋转页时不再提示归一化（不推荐）")
    ap.add_argument("--font-scale", type=float, default=1.2,
                    help="中文起始字号 = 原英文块字号 × 该系数（默认 1.2）。"
                         "构建时从该值向下搜索最大可放入字号，"
                         "即版面允许时自动放大、不够时自动缩小，两者兼顾")
    ap.add_argument("--no-indent", action="store_true",
                    help="关闭段落首行缩进（默认按中文排版习惯段首空两格）")
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF")
        return 2

    src_path = Path(args.source).resolve()
    doc = fitz.open(src_path)
    blocks_data = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
    trans = json.loads(Path(args.translations).read_text(encoding="utf-8"))

    hash_mismatch, hash_unverified = validate_translation_hashes(blocks_data, trans)
    if hash_mismatch:
        print(f"❌ 发现 {len(hash_mismatch)} 条译文 source_hash 与当前 blocks.json 不一致；"
              "这通常表示旧译文被错配到重新抽取后的 block。", file=sys.stderr)
        for bid, want, got in hash_mismatch[:12]:
            print(f"   {bid}: blocks={want[:12]}… translations={got[:12]}…", file=sys.stderr)
        doc.close()
        return 2
    if hash_unverified:
        print(f"⚠️ {hash_unverified} 条译文没有 source_hash（旧版/手工译文），"
              "无法验证与当前文本块的来源一致性。")

    # 旋转页防护：未归一化的旋转页会导致 insert 坐标系错位（块矩形被裁空/叠印）
    rot_pages = [i + 1 for i in range(doc.page_count) if doc[i].rotation != 0]
    if rot_pages and not args.allow_rotated:
        print(f"⚠️ 源 PDF 有旋转页 {rot_pages}，直接构建可能坐标错位。")
        print("   建议先归一化: python normalize_pdf.py --input <源> --output <归一化.pdf>")
        print("   然后对归一化 PDF 重新抽取并构建（或加 --allow-rotated 强行继续）")

    # 字体解析优先级：CLI --font > PDT_FONT/PDT_FONT_BOLD（环境变量/config.json，
    # 走 config.py 统一配置层）> 本文件候选列表。显式指定的字体同样要过 verify_font 自检，
    # 保证 config.py doctor() 报告的字体与实际构建用的一致。
    pdt_font = PDT.find_font() if PDT is not None else None
    pdt_bold = PDT.find_font(bold=True) if PDT is not None else None
    font_path = find_cjk_font(args.font or (str(pdt_font) if pdt_font else None))
    bold_font_path = find_cjk_font((str(pdt_bold) if pdt_bold else None), bold=True) or font_path
    if not font_path:
        print("❌ 找不到 CJK 字体，用 --font 指定")
        return 2
    print(f"CJK 字体(正文): {font_path}")
    print(f"CJK 字体(标题): {bold_font_path}")

    # html 渲染（含公式的块要做字体回退）需要的字体目录与文件名。
    # 数学字体走统一配置层（PDT_MATH_FONT 环境变量 / config.json math_font /
    # config.py 的跨平台候选），找不到再兜底探测正文字体同目录。
    # 候选里不放 NotoSerifSC-VF —— 禁用字体（ToUnicode 异常，见文件顶部说明）。
    font_dir = font_path.parent
    font_basename = font_path.name
    math_path = PDT.find_math_font() if PDT is not None else None
    math_basename = None
    if math_path and math_path.name != font_basename:
        math_basename = math_path.name
    else:
        math_path = None
        for cand in ("cambria.ttc", "STIXTwoMath-Regular.otf"):
            if (font_dir / cand).is_file() and cand != font_basename:
                math_basename = cand
                math_path = font_dir / cand
                break
    # htmlbox 的 archive：正文与数学字体所在目录都要挂上，跨目录 @font-face 才能解析
    html_arch = fitz.Archive(str(font_dir))
    if math_path is not None and math_path.parent != font_dir:
        html_arch.add(str(math_path.parent))
    try:
        base_font = fitz.Font(fontfile=str(font_path))
    except Exception:
        base_font = None
    print(f"数学字体: {math_basename or '(走内置回退)'}"
          + (f"  ({math_path})" if math_path else ""))

    # 表格译文（可选）
    tables_data = None
    table_dict = {}
    if not args.no_table and args.tables and args.table_dict:
        tables_data = json.loads(Path(args.tables).read_text(encoding="utf-8"))
        table_dict = json.loads(Path(args.table_dict).read_text(encoding="utf-8"))
        n = sum(len(t["cells"]) for p in tables_data.get("pages", []) for t in p["tables"])
        print(f"表格: {len(tables_data.get('pages', []))} 页 / {n} 个单元格，"
              f"译文词条 {len(table_dict)} 条")

    # 参与处理的页（0-based）
    pages_spec = parse_pages(args.pages, doc.page_count)
    blocks_by_page = {p["page"]: p["blocks"] for p in blocks_data["pages"]}

    # --- 1) 在临时文档里做出"译文页" ---
    tmp = fitz.open()
    tmp.insert_pdf(doc, from_page=0, to_page=doc.page_count - 1)

    stats = {"pages": 0, "translated": 0, "lost": 0, "skipped": 0, "blanked": 0,
             "table_cells": 0, "table_lost": 0, "shrunk": []}
    carry_tail: str | None = None   # 上一页最后一个已译块的句尾（跨页缩进判定用）

    pages_set = set(pages_spec)
    for pno in pages_spec:
        # --pages 子集重建：上一页不在本轮 → 从其已译块补算句尾，
        # 跨页续段的首块不因 carry 断档而被误加首行缩进（整本顺序构建不受影响）
        if not args.no_indent and pno > 0 and (pno - 1) not in pages_set:
            carry_tail = _carry_from_page(blocks_by_page.get(pno, []), trans,
                                          doc[pno - 1].rect.width)
        page = tmp[pno]
        blks = blocks_by_page.get(pno + 1, [])
        to_redact = []
        inserts = []
        for b in blks:
            t = trans.get(b["id"])
            if b.get("nested_in") and not (t or {}).get("zh"):
                # bbox 嵌套簇的碎片：外层块已扩 bbox 覆盖此处，不重复处理
                stats["skipped"] += 1
                continue
            if not t:
                stats["skipped"] += 1
                continue
            if t.get("skip"):
                stats["skipped"] += 1
                continue
            if t.get("blank"):
                # 只抹掉原文、不插新字（用于把跨行片段并到上一块时清掉残留）
                to_redact.append(fitz.Rect(b["bbox"]))
                stats["blanked"] = stats.get("blanked", 0) + 1
                continue
            if not (t.get("zh") or "").strip():
                stats["skipped"] += 1
                continue
            to_redact.append(fitz.Rect(b["bbox"]))
            zh_clean = normalize_translation_text(t["zh"])
            if zh_clean != t["zh"].strip():
                stats["normalized_breaks"] = stats.get("normalized_breaks", 0) + 1
            inserts.append((b, zh_clean))

        # 首行缩进决策：按阅读序（列内自上而下、左列先于右列、上一页先于本页）
        # 回溯「上一个非标题已译块」的句尾。紧邻的前一块是标题 → 必是新段落（缩进）；
        # 前一块句未完（公式槽位/逗号结尾等）→ 段中接续（不缩）。
        if not args.no_indent and inserts:
            ordered = sorted(
                inserts,
                key=lambda it: (
                    it[0].get("flow_index", 10**9),
                    it[0]["bbox"][0] > page.rect.width / 2,
                    it[0]["bbox"][1],
                    it[0]["bbox"][0],
                ),
            )
            final: dict[int, str] = {}             # id(block) -> 最终 zh
            last_tail: str | None = None           # 本页最近一个非标题块的句尾
            prev_head = False                      # 上一处理块是否为标题
            for b, zh in ordered:
                is_head = bool(b.get("heading") or b.get("bold"))
                if is_head:
                    final[id(b)] = zh              # 标题顶格
                    prev_head = True
                else:
                    if prev_head:
                        prev_tail = None           # 标题之后必是新段落
                    elif last_tail is not None:
                        prev_tail = last_tail      # 阅读序上一块
                    else:
                        prev_tail = carry_tail     # 页首：沿用上一页末块句尾
                    pref = indent_prefix(zh, prev_tail)
                    if pref:
                        stats["indented"] = stats.get("indented", 0) + 1
                    final[id(b)] = pref + zh
                    last_tail = zh
                    prev_head = False
            # 本页以标题收尾时，下一页首块视为新段落（carry 置空）
            carry_tail = None if prev_head else last_tail
            inserts = [(b, final.get(id(b), zh)) for b, zh in inserts]

        if to_redact:
            for r in to_redact:
                page.add_redact_annot(r, fill=False)
            try:
                page.apply_redactions(
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                )
            except TypeError:
                print("❌ 当前 PyMuPDF 版本不支持安全 redaction 参数。请升级 PyMuPDF；"
                      "为避免破坏图像/矢量线条，本次拒绝降级执行。", file=sys.stderr)
                tmp.close()
                doc.close()
                return 2

        for b, zh in inserts:
            rect = fitz.Rect(b["bbox"])
            use_bold = bool(b.get("heading") or b.get("bold")) and not args.no_bold
            fpath = bold_font_path if use_bold else font_path
            # 给底部留一点余量，避免中文行高被截断
            rect.y1 = min(page.rect.height - 2, rect.y1 + max(1.0, b["size"] * 0.25))
            base = b["size"]
            color = int_to_rgb(b.get("color", 0))
            # 含数学记号（X_sub / X^sup）或基础字体盖不住的字符 → 走 html 渲染，
            # 才能得到真下标/上标与字体回退
            use_html = needs_fallback(zh, base_font) or bool(_MATH_MARKUP.search(zh))
            # 阶段 1（自动放大）：起始字号 = 原字号 × font_scale，只在**块自身矩形内**
            # 向下搜索最大可放入字号。中文比英文紧凑，版面宽松时会命中比原字号大的
            # 字号；矩形不越界 → 绝不与相邻块叠印。
            size = None
            if args.font_scale > 1.0:
                start = base * args.font_scale
                if use_html:
                    size = insert_html_fitted(page, rect, zh, html_arch, font_basename,
                                              math_basename, color, start, args.align,
                                              LINE_HEIGHT_RATIO)
                if size is None:
                    # html 失败（内容过密需缩小到下限以下）时回退纯文本插入，保内容优先
                    size = insert_fitted(page, rect, zh, "cjk", fpath, color,
                                         start_size=start, align=args.align)
            # 阶段 2（兜底）：仅当阶段 1 完全失败（一个字都没写进页面）时才放宽
            # 矩形底部重试。阶段 1 成功后文字已落墨，再插一遍同文本会叠印；
            # 字号偏小的块交给「缩小字号」统计与质检暴露，而不是覆盖重写。
            if size is None:
                rect2 = fitz.Rect(rect)
                rect2.y1 = min(page.rect.height - 2, rect2.y1 + 12)
                start = base * 1.06
                size2 = None
                if use_html:
                    size2 = insert_html_fitted(page, rect2, zh, html_arch, font_basename,
                                               math_basename, color, start, args.align,
                                               LINE_HEIGHT_RATIO)
                if size2 is None:
                    size2 = insert_fitted(page, rect2, zh, "cjk", fpath, color,
                                          start_size=start, align=args.align)
                if size2 is not None and (size is None or size2 > size):
                    size = size2
            if size is None:
                stats["lost"] += 1
                print(f"  ⚠️ {b['id']} 放不下，已留空（{zh[:30]}...）")
            else:
                stats["translated"] += 1
                if size > b["size"] * 1.05:
                    stats.setdefault("enlarged", []).append((b["id"], round(b["size"], 1), round(size, 1)))
                if size < b["size"] * 0.92:
                    stats["shrunk"].append((b["id"], round(b["size"], 1), round(size, 1)))
        stats["pages"] += 1

        # --- 表格单元格：逐格替换（保留矢量网格与书签） ---
        if tables_data:
            tp = next((p for p in tables_data["pages"] if p["page"] == pno + 1), None)
            if tp:
                cells = []
                for t in tp["tables"]:
                    tw = max(1.0, t["region"][2] - t["region"][0])
                    for c in t["cells"]:
                        zh = normalize_translation_text(table_dict.get(c["text"]) or "")
                        if not zh:
                            continue
                        r = fitz.Rect(c["bbox"])
                        # 字号必须在 redaction 之前量，抹掉文字后就量不到了
                        cells.append((r, zh, tw, cell_font_size(page, r) or 7.0))
                if cells:
                    for r, _, _, _ in cells:
                        page.add_redact_annot(r, fill=False)
                    try:
                        page.apply_redactions(
                            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                            images=fitz.PDF_REDACT_IMAGE_NONE,
                        )
                    except TypeError:
                        print("❌ 当前 PyMuPDF 版本不支持安全 redaction 参数。请升级 PyMuPDF；"
                              "为避免破坏图像/矢量线条，本次拒绝降级执行。", file=sys.stderr)
                        tmp.close()
                        doc.close()
                        return 2

                    for r, zh, tw, sz in cells:
                        # 宽格（题注）左对齐，窄格居中
                        al = args.table_align
                        if al is None:
                            al = 0 if r.width > 0.55 * tw else 1
                        rr = fitz.Rect(r)
                        rr.y1 += max(1.0, sz * 0.3)
                        s2 = insert_fitted(page, rr, zh, "cjk", font_path,
                                           (0, 0, 0), start_size=sz * 1.05, align=al)
                        if s2 is None:
                            rr.y1 += 10
                            s2 = insert_fitted(page, rr, zh, "cjk", font_path,
                                               (0, 0, 0), start_size=sz * 1.05, align=al)
                        if s2 is None:
                            stats["table_lost"] += 1
                            print(f"  ⚠️ 表格格放不下: {zh[:26]}")
                        else:
                            stats["table_cells"] += 1

    # --- 2) 拼双栏（左英右中）---
    out = fitz.open()
    for pno in range(doc.page_count):
        sp = doc[pno]
        W, H = sp.rect.width, sp.rect.height
        dp = out.new_page(width=W * 2, height=H)
        dp.show_pdf_page(fitz.Rect(0, 0, W, H), doc, pno)
        dp.show_pdf_page(fitz.Rect(W, 0, W * 2, H), tmp, pno)
        dp.set_rotation(sp.rotation)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 保留原文书签/目录（BabelDOC 也是这么做的）
    try:
        toc = doc.get_toc()
        if toc:
            out.set_toc(toc)
    except Exception as e:
        print(f"⚠️ TOC 复制失败: {e}")
    out.save(str(out_path), garbage=4, deflate=True)
    print(f"\n输出: {out_path.resolve()}  ({out_path.stat().st_size/1024/1024:.2f} MB)")
    print(f"页数: {out.page_count}")
    print(f"统计: 处理 {stats['pages']} 页 / 翻译 {stats['translated']} 块 / "
          f"跳过 {stats['skipped']} 块 / 放不下 {stats['lost']} 块")
    if stats["table_cells"] or stats["table_lost"]:
        print(f"      表格单元格: 替换 {stats['table_cells']} 格 / 放不下 {stats['table_lost']} 格")
    if stats["shrunk"]:
        print(f"缩小字号 {len(stats['shrunk'])} 处（前 15）: {stats['shrunk'][:15]}")
    if stats.get("enlarged"):
        eg = stats["enlarged"]
        print(f"放大字号 {len(eg)} 处（前 15）: {eg[:15]}")
    if stats.get("indented"):
        print(f"首行缩进 {stats['indented']} 块（段首空两格）")
    if stats.get("normalized_breaks"):
        print(f"译文硬换行回流 {stats['normalized_breaks']} 块（按自然段重新排版）")

    # --- 3) 预览 ---
    if args.preview:
        pv = Path(args.preview)
        pv.parent.mkdir(parents=True, exist_ok=True)
        sel = parse_pages(args.preview_pages, out.page_count)
        for i in sel:
            pix = out[i].get_pixmap(matrix=fitz.Matrix(args.preview_zoom, args.preview_zoom))
            f = pv if len(sel) == 1 else pv.with_name(f"{pv.stem}_p{i+1}{pv.suffix}")
            pix.save(str(f))
            print(f"预览: {f}")

    out.close()
    tmp.close()
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
