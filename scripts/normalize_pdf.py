#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / normalize_pdf.py

把带旋转页（rotation != 0）的 PDF 归一化为 rotation=0：旋转页的内容被
烘焙成与"显示方向"一致的普通页面。归一化后 get_text 坐标与显示坐标一致，
extract_blocks / build_dual 才能正常工作（否则 insert_textbox 会因矩形
落在可视区域外而失败或叠印）。

用法:
  python normalize_pdf.py --check --input src.pdf          # 只报告哪些页旋转
  python normalize_pdf.py --input src.pdf --output norm.pdf # 归一化（原地覆盖用 --output 指回同一路径也可）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("normalize_pdf", description="旋转页归一化")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", default=None, help="默认与 --input 同路径（原地替换）")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    ap.add_argument("--backup-suffix", default=".orig.pdf",
                    help="原地替换时原文件的备份后缀（默认 .orig.pdf）")
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF")
        return 2

    src = Path(args.input).resolve()
    if not src.is_file():
        print(f"❌ 文件不存在: {src}")
        return 2

    doc = fitz.open(src)
    rotated = [i + 1 for i in range(doc.page_count) if doc[i].rotation != 0]
    if not rotated:
        print(f"✅ 无旋转页（{doc.page_count} 页全部 rotation=0），无需归一化")
        doc.close()
        return 0
    print(f"检测到旋转页: {rotated}（共 {doc.page_count} 页）")

    if args.check:
        for i in rotated:
            p = doc[i - 1]
            print(f"  page {i}: rotation={p.rotation} rect={p.rect}")
        doc.close()
        return 0

    out = fitz.open()
    for p in doc:
        if p.rotation == 0:
            out.insert_pdf(doc, from_page=p.number, to_page=p.number)
        else:
            # p.rect 是显示方向（已含旋转）的尺寸；show_pdf_page 会自动反旋内容
            W, H = p.rect.width, p.rect.height
            np_ = out.new_page(width=W, height=H)
            np_.show_pdf_page(fitz.Rect(0, 0, W, H), doc, p.number)
    out.set_toc(doc.get_toc() or [])

    dst = Path(args.output).resolve() if args.output else src
    if dst == src:
        bak = src.with_name(src.stem + args.backup_suffix)
        if not bak.exists():
            doc.save(str(bak), garbage=4, deflate=True)
            print(f"备份原文件: {bak}")
    out.save(str(dst), garbage=4, deflate=True)
    print(f"归一化完成: {dst}（{out.page_count} 页，其中 {len(rotated)} 页已烘焙为 rotation=0）")

    # 自检
    chk = fitz.open(str(dst))
    left = [i + 1 for i in range(chk.page_count) if chk[i].rotation != 0]
    print("自检:", "✅ 全部 rotation=0" if not left else f"❌ 仍有旋转页 {left}")
    chk.close()
    out.close()
    doc.close()
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
