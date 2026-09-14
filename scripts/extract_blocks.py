#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / extract_blocks.py

从英文原文 PDF 抽取可翻译的文本块，产出 blocks.json，供翻译与重建使用。

用法:
  python extract_blocks.py --input src.pdf --output blocks.json [--pages 1-3] [--min-chars 2]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

MATH_FONT_HINTS = ("stix", "lmroman", "cmmi", "cmsy", "cmr", "math", "symbol", "mtmi", "euclid")
CJK = re.compile(r"[\u4e00-\u9fff]")


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


def block_text(block: dict) -> str:
    """把 block 的 lines/spans 拼成段落文本。"""
    lines_out = []
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            parts.append(span.get("text", ""))
        t = "".join(parts).rstrip()
        if t:
            lines_out.append(t)
    text = "\n".join(lines_out)
    # 连字符断行合并： "informa-\ntion" -> "information"
    text = re.sub(r"-\n(?=[a-z])", "", text)
    # 单换行视为空格（同一段落的折行）
    text = re.sub(r"(?<![.\n])\n(?!\n)", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("extract_blocks")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--min-chars", type=int, default=2, help="少于该字符数的块跳过")
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    doc = fitz.open(Path(args.input).resolve())
    pages = parse_pages(args.pages, doc.page_count)

    result = {
        "source": str(Path(args.input).resolve()),
        "page_count": doc.page_count,
        "pages": [],
    }

    for i in pages:
        page = doc[i]
        d = page.get_text("dict")
        blocks = []
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            text = block_text(b)
            if len(text) < args.min_chars:
                continue

            spans = [s for l in b.get("lines", []) for s in l.get("spans", [])]
            if not spans:
                continue
            # 主字号：按字符数加权
            size_w: dict[float, int] = {}
            font_w: dict[str, int] = {}
            color = spans[0].get("color", 0)
            for s in spans:
                n = len(s.get("text", ""))
                size_w[round(s.get("size", 0), 1)] = size_w.get(round(s.get("size", 0), 1), 0) + n
                font_w[s.get("font", "")] = font_w.get(s.get("font", ""), 0) + n
            main_size = max(size_w.items(), key=lambda kv: kv[1])[0] if size_w else 9.0
            main_font = max(font_w.items(), key=lambda kv: kv[1])[0] if font_w else ""

            is_math = any(h in main_font.lower() for h in MATH_FONT_HINTS)
            any_math_font = any(
                any(h in (s.get("font") or "").lower() for h in MATH_FONT_HINTS) for s in spans
            )
            # 粗体判定：按字符数加权（只用 spans[0] 会把 "Fig. 3." 这种
            # 前缀加粗的图注整段误判为粗体）
            bold_chars = 0
            all_chars = 0
            for s in spans:
                n = len(s.get("text", ""))
                all_chars += n
                fl = s.get("flags", 0)
                if (fl & 2 ** 4) or "bold" in (s.get("font") or "").lower():
                    bold_chars += n
            bold = all_chars > 0 and (bold_chars / all_chars) >= 0.6
            # 标题判定：Elsevier 的节标题常为斜体短句（"2.1. Rule-based ..."），
            # 中文排版里用黑体呈现更合适
            heading = bold or (
                "italic" in main_font.lower()
                and len(text) < 90
                and not text.rstrip().endswith((".", "。", ";", "；", ",", "，", ":", "："))
            )

            x0, y0, x1, y1 = b["bbox"]
            blocks.append({
                "id": f"p{i+1}b{len(blocks)}",
                "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                "size": main_size,
                "font": main_font,
                "bold": bold,
                "heading": heading,
                "color": color,
                "math_only": is_math,
                "has_math": any_math_font,
                "chars": len(text),
                "text": text,
            })
        result["pages"].append({
            "page": i + 1,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "blocks": blocks,
        })

    doc.close()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    total_blocks = sum(len(p["blocks"]) for p in result["pages"])
    total_chars = sum(b["chars"] for p in result["pages"] for b in p["blocks"])
    print(f"抽取完成: {len(result['pages'])} 页, {total_blocks} 个文本块, {total_chars} 字符")
    for p in result["pages"]:
        print(f"  page {p['page']}: {len(p['blocks'])} 块  "
              f"{sum(b['chars'] for b in p['blocks'])} 字符")
    print(f"输出: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
