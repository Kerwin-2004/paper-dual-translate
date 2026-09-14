#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / extract_tables.py

把 PDF 里的表格拆成"单元格级"文本块，产出 tables.json。

为什么不用 page.find_tables()：
  Elsevier 的表格是 booktabs 风格（只有横线、没有竖线），默认 lines 策略检不到；
  改成 text 策略又会把多面板表格切得乱七八糟（连标题区都误判成表格）。
所以这里自己解析：
  1) 从矢量横线聚类出表格区域（横线给了表域和部分行边界）
  2) 表格区域内按文字基线聚成"行"
  3) 每行内按文字 x 间隙切出"列"
这样得到的单元格 bbox 精确，替换时不会碰到旁边的矢量网格。

为什么必须逐格替换：表格文字块在普通抽取里是一整行扁平化的 blob，
整块 redaction 会把表格网格一起抹掉，版式全毁。

用法:
  python extract_tables.py --input src.pdf --output tables.json [--pages all]
"""
from __future__ import annotations

import argparse
import json
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


def clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").replace("\u00a0", " ")).strip()


def horizontal_rules(page):
    """取出所有"横向细线"：返回 [(y, x0, x1), ...]"""
    rules = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 0.9 and abs(p1.x - p2.x) > 15:
                    rules.append((round((p1.y + p2.y) / 2, 1),
                                  round(min(p1.x, p2.x), 1), round(max(p1.x, p2.x), 1)))
            elif it[0] == "re":
                r = it[1]
                if r.height < 1.4 and r.width > 15:
                    rules.append((round(r.y0, 1), round(r.x0, 1), round(r.x1, 1)))
    return sorted(set(rules))


def _overlap(a0, a1, b0, b1) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter / max(1e-6, min(a1 - a0, b1 - b0))


def cluster_rules(rules, y_tol=30.0, x_overlap=0.5):
    """把横线聚成一张张表：y 间距够近且 x 区间有重叠就算同一张表。

    仅在没有题注可用时作为兜底方案 —— booktabs 表格中间不画线时，
    单靠 y 间距会把同一张表拆开、把相邻两张表并起来。
    """
    groups = []
    for y, x0, x1 in sorted(rules):
        for g in groups:
            if (y - g["y1"]) <= y_tol and _overlap(x0, x1, g["x0"], g["x1"]) >= x_overlap:
                g["ys"].append(y)
                g["y1"] = max(g["y1"], y)
                g["x0"] = min(g["x0"], x0)
                g["x1"] = max(g["x1"], x1)
                break
        else:
            groups.append({"ys": [y], "y0": y, "y1": y, "x0": x0, "x1": x1})
    return groups


def caption_lines(page):
    """找出所有 "Table N" 题注行。"""
    caps = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            txt = clean("".join(s["text"] for s in ln.get("spans", [])))
            m = re.match(r"Table\s+(\d+)\b", txt)
            if m:
                x0, y0, x1, y1 = ln["bbox"]
                caps.append({"num": int(m.group(1)), "text": txt,
                             "y": y0, "y1": y1, "x0": x0, "x1": x1,
                             "xc": (x0 + x1) / 2})
    return sorted(caps, key=lambda c: (c["y"], c["x0"]))


def group_by_caption(rules, caps, page_mid):
    """题注锚定：每条横线归属到"它上方最近的那个题注"。

    细节：
    - 题注必须在横线**上方**（只留 2pt 余量）。否则下一张表的题注会把
      上一张表的底边抢走，导致两张表都被切坏。
    - 并排两张表题注 y 相同时（page12 的表 3 与表 4），按横线中心与题注中心
      是否在同一半页来裁决。
    """
    groups = []
    for y, x0, x1 in rules:
        xc = (x0 + x1) / 2
        cands = [c for c in caps if c["y"] <= y + 2]
        if not cands:
            continue
        best_dy = min(y - c["y"] for c in cands)
        tied = [c for c in cands if (y - c["y"]) <= best_dy + 8]
        if len(tied) == 1:
            best = tied[0]
        else:
            same_side = [c for c in tied if (c["xc"] < page_mid) == (xc < page_mid)]
            pool = same_side or tied
            best = min(pool, key=lambda c: abs(xc - c["xc"]))
        g = next((x for x in groups if x["cap"] is best), None)
        if g is None:
            g = {"cap": best, "ys": [], "x0": x0, "x1": x1}
            groups.append(g)
        g["ys"].append(y)
        g["x0"] = min(g["x0"], x0)
        g["x1"] = max(g["x1"], x1)
    for g in groups:
        g["ys"] = sorted(set(g["ys"]))
        g["y0"] = g["cap"]["y"]
        g["y1"] = max(g["ys"])
        g["title"] = g["cap"]["text"]
    return sorted(groups, key=lambda g: (g["y0"], g["x0"]))


def line_rows(page, x0, x1, y0, y1):
    """把区域内的文字行按 y 分组：返回 [(y_center, [word,...]), ...]"""
    words = [w for w in page.get_text("words")
             if x0 - 3 <= w[0] and w[2] <= x1 + 3
             and y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1]
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in words:
        cy = (w[1] + w[3]) / 2
        if rows and abs(cy - rows[-1][0]) <= 3.2:
            rows[-1][1].append(w)
            n = len(rows[-1][1])
            rows[-1][0] = (rows[-1][0] * (n - 1) + cy) / n
        else:
            rows.append([cy, [w]])
    return rows


def split_cells(words, gap_tol):
    """行内按 x 间隙切列。"""
    words = sorted(words, key=lambda w: w[0])
    cells = [[words[0]]]
    for w in words[1:]:
        if w[0] - cells[-1][-1][2] > gap_tol:
            cells.append([w])
        else:
            cells[-1].append(w)
    return cells


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("extract_tables")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--gap-tol", type=float, default=6.0,
                    help="列间隙阈值(pt)。同一格内的词间距小于它，列与列之间大于它")
    ap.add_argument("--include-caption", action="store_true", default=True)
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    doc = fitz.open(Path(args.input).resolve())
    pages = parse_pages(args.pages, doc.page_count)

    result = {"source": str(Path(args.input).resolve()), "pages": []}

    for pno in pages:
        page = doc[pno]
        rules = horizontal_rules(page)
        if not rules:
            continue
        caps = caption_lines(page)
        if caps:
            groups = group_by_caption(rules, caps, page.rect.width / 2)
            mode = "题注锚定"
        else:
            groups = cluster_rules(rules)
            # 无题注兜底：滤掉装饰性下划线/图框（那些格子极少）
            groups = [g for g in groups if len(g["ys"]) >= 2]
            mode = "横线聚类"
        if not groups:
            continue
        page_rec = {"page": pno + 1, "tables": []}

        for gi, g in enumerate(groups):
            x0, x1 = g["x0"], g["x1"]
            y_top, y_bot = g["y0"], g["y1"]
            y_cap = g["cap"]["y"] if "cap" in g else y_top
            # 题注可能比表体更宽/更左，把表域扩到能容下题注
            if "cap" in g:
                x0 = min(x0, g["cap"]["x0"])
                x1 = max(x1, g["cap"]["x1"])

            rows = line_rows(page, x0, x1, y_cap, y_bot + 2)
            cells = []
            for ri, (cy, ws) in enumerate(rows):
                for ci, cw in enumerate(split_cells(ws, args.gap_tol)):
                    bx0 = min(w[0] for w in cw)
                    by0 = min(w[1] for w in cw)
                    bx1 = max(w[2] for w in cw)
                    by1 = max(w[3] for w in cw)
                    txt = clean(" ".join(w[4] for w in cw))
                    if not txt:
                        continue
                    cells.append({
                        "id": f"p{pno+1}T{gi}R{ri}C{ci}",
                        "row": ri, "col": ci,
                        "bbox": [round(bx0, 2), round(by0, 2), round(bx1, 2), round(by1, 2)],
                        "text": txt,
                    })
            if cells and (mode == "题注锚定" or len(cells) >= 4):
                page_rec["tables"].append({
                    "index": gi,
                    "region": [round(x0, 2), round(y_cap, 2), round(x1, 2), round(y_bot, 2)],
                    "nrows": len(rows),
                    "cells": cells,
                })
        if page_rec["tables"]:
            result["pages"].append(page_rec)

    doc.close()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(len(t["cells"]) for p in result["pages"] for t in p["tables"])
    print(f"解析完成: {len(result['pages'])} 页含表格, {total} 个单元格")
    for p in result["pages"]:
        for t in p["tables"]:
            print(f"  page {p['page']} 表{t['index']}: {t['nrows']} 行 / {len(t['cells'])} 格  区域={t['region']}")
    print(f"输出: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
