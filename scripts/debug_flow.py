#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec or spec.lower() == "all":
        return list(range(total))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a) - 1, int(b)))
        else:
            out.add(int(part) - 1)
    return sorted(i for i in out if 0 <= i < total)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render v4 block flow overlay for visual inspection")
    ap.add_argument("--source", required=True)
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("需要 PyMuPDF: pip install pymupdf")
        return 2

    src = fitz.open(Path(args.source).resolve())
    data = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
    by_page = {int(p["page"]): p.get("blocks", []) for p in data.get("pages", [])}
    selected = parse_pages(args.pages, src.page_count)

    out = fitz.open()
    for pno in selected:
        out.insert_pdf(src, from_page=pno, to_page=pno)
        page = out[-1]
        blocks = sorted(by_page.get(pno + 1, []), key=lambda b: b.get("flow_index", 10**9))
        for b in blocks:
            r = fitz.Rect(b["bbox"])
            col = b.get("column", "?")
            color = {"left": (1, 0, 0), "right": (0, 0, 1), "full": (0, 0.55, 0)}.get(col, (0.5, 0.2, 0.5))
            page.draw_rect(r, color=color, width=0.7, overlay=True)
            label = f"{b.get('flow_index','?')} {b.get('kind','?')} {col} {b.get('id','?')}"
            y = max(5, r.y0 - 1.5)
            page.insert_text((r.x0, y), label, fontsize=5.5, color=color, overlay=True)

    dst = Path(args.out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, garbage=4, deflate=True)
    out.close()
    src.close()
    print(f"flow overlay: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
