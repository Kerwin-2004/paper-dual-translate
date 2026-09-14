#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / render.py

把 PDF 页面渲染成图片，供 **具备视觉能力的 agent 直接"看"**，做人工级质检。
这不是可有可无的调试工具 —— 有些问题（文字压到图片上、行重叠、字被裁掉、
表格错位）只有看图才能发现，纯文本提取是查不出来的。

子命令:
  page     渲染指定页（可只渲某个矩形区域）
  compare  把"原文页"与"译文页"并排渲染成一张图，供视觉对照
  strip    渲染横向长条（用于逐段细看）
  contact  多页缩略图拼成一张 contact sheet，快速扫全篇
  probe    渲染一个坐标网格 + 标注，便于把视觉观察定位回 PDF 坐标

用法示例:
  python render.py page    --pdf out.pdf --page 12 --out p12.png --zoom 2
  python render.py compare --source src.pdf --translated out.pdf --page 12 --out cmp12.png
  python render.py contact --pdf out.pdf --out all.png --cols 4
  python render.py probe   --pdf out.pdf --page 7 --out grid7.png
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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


def parse_rect(spec: str | None):
    if not spec:
        return None
    vals = [float(x) for x in re.split(r"[,\s]+", spec.strip()) if x]
    if len(vals) != 4:
        raise SystemExit("--rect 需要 4 个数字: x0,y0,x1,y1")
    import fitz
    return fitz.Rect(*vals)


def _half(page, which: str):
    import fitz
    w, h = page.rect.width, page.rect.height
    if which == "left":
        return fitz.Rect(0, 0, w / 2, h)
    if which == "right":
        return fitz.Rect(w / 2, 0, w, h)
    return fitz.Rect(0, 0, w, h)


def _compose(panels, out_png: Path, zoom: float, gap: float = 8.0,
             cols: int = 1, label: str = "") -> tuple[str, int, int]:
    """把若干「(文档, 页号, 裁剪框)」按网格拼成一页再渲染成 PNG。

    不用 Pixmap.copy 手工拼位图（实测放置不可靠、容易错位），
    而是用 show_pdf_page 在一张新 PDF 页上排版，再整体渲染。
    """
    import fitz
    if not panels:
        raise SystemExit("没有可渲染的面板")
    cols = max(1, cols)
    rows = (len(panels) + cols - 1) // cols

    cell_w = max((p[2].width for p in panels))
    cell_h = max((p[2].height for p in panels))
    W = cols * cell_w + (cols + 1) * gap
    H = rows * cell_h + (rows + 1) * gap

    out = fitz.open()
    page = out.new_page(width=W, height=H)
    for k, (doc, pno, clip) in enumerate(panels):
        r, c = divmod(k, cols)
        x = gap + c * (cell_w + gap)
        y = gap + r * (cell_h + gap)
        # 按比例缩放居中放进格子
        sc = min(cell_w / clip.width, cell_h / clip.height)
        w, h = clip.width * sc, clip.height * sc
        dx = x + (cell_w - w) / 2
        dy = y + (cell_h - h) / 2
        page.show_pdf_page(fitz.Rect(dx, dy, dx + w, dy + h), doc, pno, clip=clip)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_png))
    out.close()
    if label:
        print(f"{label}: {out_png}  ({pix.width}x{pix.height}px)")
    return str(out_png), pix.width, pix.height


def cmd_page(a) -> int:
    import fitz
    doc = fitz.open(a.pdf)
    pages = parse_pages(a.pages, doc.page_count)
    rect = parse_rect(a.rect)
    made = []
    for i in pages:
        page = doc[i]
        clip = rect or _half(page, a.half)
        out = Path(a.out)
        if len(pages) > 1:
            out = out.with_name(f"{out.stem}_p{i+1}{out.suffix}")
        _compose([(doc, i, clip)], out, a.zoom, label=f"渲染 页{i+1}")
        made.append(str(out))
    doc.close()
    for m in made:
        print("  ->", m)
    return 0


def cmd_compare(a) -> int:
    """原文页 vs 译文页（中文侧）并排。"""
    import fitz
    src = fitz.open(a.source)
    ref = fitz.open(a.translated)
    pages = parse_pages(a.pages, min(src.page_count, ref.page_count))
    out = Path(a.out)
    for i in pages:
        sp, rp = src[i], ref[i]
        panels = [
            (src, i, fitz.Rect(0, 0, sp.rect.width, sp.rect.height)),
            (ref, i, fitz.Rect(rp.rect.width / 2, 0, rp.rect.width, rp.rect.height)),
        ]
        f = out if len(pages) == 1 else out.with_name(f"{out.stem}_p{i+1}{out.suffix}")
        _compose(panels, f, a.zoom, cols=2, label=f"对照图 页{i+1}（左=原文 右=中文）")
    src.close()
    ref.close()
    return 0


def cmd_strip(a) -> int:
    """渲染横向长条，用于逐段细看某一块区域。"""
    import fitz
    doc = fitz.open(a.pdf)
    pages = parse_pages(a.page, doc.page_count)
    i = pages[0]
    page = doc[i]
    top = float(a.top)
    bot = float(a.bottom) if a.bottom else page.rect.height
    x0, x1 = (float(a.x0), float(a.x1)) if a.x0 else (0.0, page.rect.width)
    if a.half == "left":
        x1 = min(x1, page.rect.width / 2)
    elif a.half == "right":
        x0 = max(x0, page.rect.width / 2)
    _compose([(doc, i, fitz.Rect(x0, top, x1, bot))], Path(a.out), a.zoom,
             label=f"长条 页{i+1}")
    doc.close()
    return 0


def cmd_contact(a) -> int:
    """多页缩略图拼 contact sheet，快速扫全篇找异常页。"""
    import fitz
    doc = fitz.open(a.pdf)
    pages = parse_pages(a.pages, doc.page_count)
    panels = [(doc, i, fitz.Rect(0, 0, doc[i].rect.width, doc[i].rect.height)) for i in pages]
    _compose(panels, Path(a.out), a.zoom, cols=max(1, a.cols),
             label=f"contact sheet（{len(pages)} 页, {max(1,a.cols)} 列）")
    doc.close()
    return 0


def cmd_probe(a) -> int:
    """渲染页面并叠加坐标网格，便于把视觉观察定位回 PDF 坐标（写 bbox 用）。"""
    import fitz
    doc = fitz.open(a.pdf)
    pages = parse_pages(a.page, doc.page_count)
    i = pages[0]
    page = doc[i]
    pix = page.get_pixmap(matrix=fitz.Matrix(a.zoom, a.zoom))
    # 在像素图上画网格
    step = int(a.step * a.zoom)
    for x in range(0, pix.width, step):
        for y in range(pix.height):
            pix.set_pixel(x, y, (255, 120, 120))
    for y in range(0, pix.height, step):
        for x in range(pix.width):
            pix.set_pixel(x, y, (255, 120, 120))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out))
    print(f"网格图: {out}  ({pix.width}x{pix.height}px)  每格 {a.step}pt")
    doc.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("render", description="把 PDF 渲染成图片供视觉质检")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("page", help="渲染整页或区域")
    p.add_argument("--pdf", required=True)
    p.add_argument("--pages", default="1")
    p.add_argument("--out", required=True)
    p.add_argument("--zoom", type=float, default=2.0)
    p.add_argument("--rect", default=None, help="x0,y0,x1,y1（PDF 坐标）")
    p.add_argument("--half", default="all", choices=["all", "left", "right"])
    p.set_defaults(func=cmd_page)

    p = sub.add_parser("compare", help="原文页 vs 译文页（中文侧）并排")
    p.add_argument("--source", required=True)
    p.add_argument("--translated", required=True)
    p.add_argument("--pages", default="1")
    p.add_argument("--out", required=True)
    p.add_argument("--zoom", type=float, default=1.4)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("strip", help="渲染横向长条")
    p.add_argument("--pdf", required=True)
    p.add_argument("--page", default="1")
    p.add_argument("--out", required=True)
    p.add_argument("--top", default="0")
    p.add_argument("--bottom", default=None)
    p.add_argument("--x0", default=None)
    p.add_argument("--x1", default=None)
    p.add_argument("--half", default="all", choices=["all", "left", "right"])
    p.add_argument("--zoom", type=float, default=3.0)
    p.set_defaults(func=cmd_strip)

    p = sub.add_parser("contact", help="多页缩略图 contact sheet")
    p.add_argument("--pdf", required=True)
    p.add_argument("--pages", default="all")
    p.add_argument("--out", required=True)
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--zoom", type=float, default=0.45)
    p.set_defaults(func=cmd_contact)

    p = sub.add_parser("probe", help="带坐标网格的页面图")
    p.add_argument("--pdf", required=True)
    p.add_argument("--page", default="1")
    p.add_argument("--out", required=True)
    p.add_argument("--zoom", type=float, default=2.0)
    p.add_argument("--step", type=float, default=50, help="网格间距(pt)")
    p.set_defaults(func=cmd_probe)

    a = ap.parse_args(argv)

    try:
        import fitz  # noqa: F401
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
