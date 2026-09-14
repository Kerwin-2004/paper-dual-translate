#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / extract_blocks.py

v4 column-aware paragraph extractor:
raw PDF blocks -> table isolation -> left/right/full classification
-> column-local paragraph merge -> explicit flow_index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import provenance as PROV
except Exception:
    PROV = None

MATH_FONT_HINTS = ("stix", "lmroman", "cmmi", "cmsy", "cmr", "math", "symbol", "mtmi", "euclid")
TERMINAL = tuple(".!?;:。！？；：")
CAPTION_TABLE = re.compile(r"^\s*(?:Table|Tab\.)\s*\d+\b", re.I)
CAPTION_FIG = re.compile(r"^\s*(?:Fig\.?|Figure)\s*\d+\b", re.I)
REF_HEAD = re.compile(r"^\s*(?:references|bibliography|reference list)\s*[:.]?\s*$", re.I)
PAGE_NO = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdm]{1,8})\s*$", re.I)
META = re.compile(
    r"(doi\s*:|https?://|www\.|copyright|©|all rights reserved|received\s|accepted\s|"
    r"journal homepage|available online|corresponding author|preprint|under review)",
    re.I,
)
LOWER_START = re.compile(r"^[a-z]")
LIST_START = re.compile(r"^\s*(?:\d{1,2}[.)、]|[a-zA-Z][.)、]|[-•·▪–]\s)")


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec or spec.lower() == "all":
        return list(range(total))
    out: set[int] = set()
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


def clean_text(text: str) -> str:
    text = (text or "").replace(" ", " ")
    text = re.sub(r"-\s*\n\s*(?=[a-z])", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ 	]{2,}", " ", text)
    return text.strip()


def block_text(block: dict) -> str:
    lines = []
    for line in block.get("lines", []):
        s = "".join(span.get("text", "") for span in line.get("spans", []))
        if s.strip():
            lines.append(s.rstrip())
    return clean_text("\n".join(lines))


def source_hash(page: int, bbox: list[float], text: str) -> str:
    # 与 v2/v3 provenance 契约保持一致：hash 绑定页码 + 规范化源文本。
    # bbox/flow 变化不会无谓使译文失效，但文本变化一定会触发重新翻译。
    if PROV is not None:
        return PROV.block_source_hash(page, text)
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha256(f"{int(page)}\0{norm}".encode("utf-8")).hexdigest()[:24]


def overlap_ratio(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    aa = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return (ix * iy) / aa


def load_table_regions(path: str | None) -> dict[int, list[list[float]]]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for page in data.get("pages", []):
        regs = []
        for table in page.get("tables", []):
            region = table.get("region")
            if region and len(region) == 4:
                regs.append([float(v) for v in region])
        if regs:
            out[int(page["page"])] = regs
    return out


def main_metrics(block: dict):
    spans = [s for ln in block.get("lines", []) for s in ln.get("spans", [])]
    if not spans:
        return 9.0, "", False, False, 0, False
    size_w, font_w = {}, {}
    bold_chars = all_chars = 0
    color = spans[0].get("color", 0)
    for s in spans:
        text = s.get("text", "")
        n = max(1, len(text))
        size = round(float(s.get("size", 0) or 0), 1)
        font = s.get("font", "") or ""
        size_w[size] = size_w.get(size, 0) + n
        font_w[font] = font_w.get(font, 0) + n
        all_chars += n
        flags = int(s.get("flags", 0) or 0)
        if (flags & 2 ** 4) or "bold" in font.lower():
            bold_chars += n
    main_size = max(size_w.items(), key=lambda kv: kv[1])[0]
    main_font = max(font_w.items(), key=lambda kv: kv[1])[0]
    bold = all_chars > 0 and bold_chars / all_chars >= 0.6
    has_math = any(any(h in (s.get("font", "") or "").lower() for h in MATH_FONT_HINTS) for s in spans)
    math_only = any(h in main_font.lower() for h in MATH_FONT_HINTS)
    return main_size, main_font, bold, has_math, color, math_only


def classify_kind(text, bbox, page_w, page_h, bold, main_font, math_only, table_regions):
    if any(overlap_ratio(bbox, r) >= 0.25 for r in table_regions):
        return "table"
    if PAGE_NO.match(text):
        return "meta"
    if len(text) < 160 and META.search(text):
        return "meta"
    if REF_HEAD.match(text):
        return "reference_heading"
    if CAPTION_TABLE.match(text):
        return "table_caption"
    if CAPTION_FIG.match(text):
        return "figure_caption"
    if math_only:
        return "math_only"
    short = len(text) <= 130
    italic = "italic" in (main_font or "").lower()
    if bold or (italic and short and not text.endswith(TERMINAL)):
        return "heading"
    if len(text) < 100 and (bbox[1] < 0.055 * page_h or bbox[3] > 0.955 * page_h):
        return "meta"
    return "body"


def classify_column(bbox, page_w: float) -> str:
    x0, _, x1, _ = bbox
    mid = page_w / 2
    width = x1 - x0
    if width >= page_w * 0.68 or (x0 < mid < x1 and width >= page_w * 0.42):
        return "full"
    return "left" if (x0 + x1) / 2 < mid else "right"


def split_raw_block(block: dict, page_w: float) -> list[dict]:
    """Split a PyMuPDF text block when it contains lines from both columns.

    Some PDFs expose simultaneous left/right lines as one full-width raw block.
    Classifying only the outer block bbox would concatenate both columns before
    flow ordering. Grouping its physical lines by column restores independent
    text units while retaining genuinely full-width lines as barriers.
    """
    lines = [line for line in block.get("lines", []) if line.get("bbox")]
    if len(lines) < 2 or classify_column(block.get("bbox", (0, 0, 0, 0)), page_w) != "full":
        return [block]

    by_column: dict[str, list[dict]] = {"left": [], "right": [], "full": []}
    for line in lines:
        by_column[classify_column(line["bbox"], page_w)].append(line)
    if not by_column["left"] or not by_column["right"]:
        return [block]

    fragments = []
    for column in ("left", "right", "full"):
        selected = by_column[column]
        if not selected:
            continue
        x0 = min(float(line["bbox"][0]) for line in selected)
        y0 = min(float(line["bbox"][1]) for line in selected)
        x1 = max(float(line["bbox"][2]) for line in selected)
        y1 = max(float(line["bbox"][3]) for line in selected)
        fragment = dict(block)
        fragment["bbox"] = (x0, y0, x1, y1)
        fragment["lines"] = selected
        fragments.append(fragment)
    return fragments


def horizontal_overlap(a, b) -> float:
    inter = max(0.0, min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0]))
    denom = max(1e-6, min(a["bbox"][2] - a["bbox"][0], b["bbox"][2] - b["bbox"][0]))
    return inter / denom


def can_merge(a: dict, b: dict) -> bool:
    if a["kind"] != "body" or b["kind"] != "body":
        return False
    if a["column"] != b["column"] or a["column"] == "full":
        return False
    if abs(float(a["size"]) - float(b["size"])) > 1.0:
        return False
    if horizontal_overlap(a, b) < 0.28:
        return False

    gap = float(b["bbox"][1]) - float(a["bbox"][3])
    size = max(5.0, min(float(a["size"]), float(b["size"])))
    if gap < -0.35 * size or gap > 1.25 * size:
        return False

    ta = a["text"].rstrip()
    tb = b["text"].lstrip()

    if gap <= 0.48 * size:
        return True
    if ta and not ta.endswith(TERMINAL):
        return True
    if LOWER_START.match(tb):
        return True

    indent_delta = float(b["bbox"][0]) - float(a["bbox"][0])
    if indent_delta >= 8.0 and ta.endswith(TERMINAL):
        return False
    if gap <= 0.72 * size and abs(indent_delta) <= 4.0 and not LIST_START.match(tb):
        return True
    return False


def merge_pair(a: dict, b: dict) -> dict:
    ta, tb = a["text"].rstrip(), b["text"].lstrip()
    text = ta[:-1] + tb if ta.endswith("-") and tb[:1].islower() else f"{ta} {tb}".strip()
    bbox = [
        min(a["bbox"][0], b["bbox"][0]), min(a["bbox"][1], b["bbox"][1]),
        max(a["bbox"][2], b["bbox"][2]), max(a["bbox"][3], b["bbox"][3]),
    ]
    out = dict(a)
    out["bbox"] = [round(float(v), 2) for v in bbox]
    out["text"] = clean_text(text)
    out["chars"] = len(out["text"])
    out["merged_from"] = list(a.get("merged_from", [a["id"]])) + list(b.get("merged_from", [b["id"]]))
    out["source_hash"] = source_hash(out["_page"], out["bbox"], out["text"])
    return out


def merge_column_blocks(blocks: list[dict]) -> list[dict]:
    result = []
    for col in ("left", "right"):
        arr = sorted([b for b in blocks if b["column"] == col], key=lambda b: (b["bbox"][1], b["bbox"][0]))
        cur = None
        for b in arr:
            if cur is None:
                cur = b
            elif can_merge(cur, b):
                cur = merge_pair(cur, b)
            else:
                result.append(cur)
                cur = b
        if cur is not None:
            result.append(cur)
    result.extend(b for b in blocks if b["column"] == "full")
    return result


def assign_flow(blocks: list[dict], table_regions: list[list[float]], page_w: float) -> list[dict]:
    """Wide objects form horizontal barriers; inside each zone read left column then right."""
    full = [b for b in blocks if b["column"] == "full"]
    barriers = [(float(b["bbox"][1]), float(b["bbox"][3]), ("block", b)) for b in full]
    for r in table_regions:
        if (r[2] - r[0]) >= page_w * 0.55:
            barriers.append((float(r[1]), float(r[3]), ("table", r)))
    barriers.sort(key=lambda x: (x[0], x[1]))

    non_full = [b for b in blocks if b["column"] != "full"]
    ordered, used = [], set()
    cursor_y = -1e9

    def emit_zone(y0, y1):
        zone = [
            b for b in non_full if id(b) not in used
            and y0 <= ((b["bbox"][1] + b["bbox"][3]) / 2) < y1
        ]
        left = sorted((b for b in zone if b["column"] == "left"), key=lambda b: (b["bbox"][1], b["bbox"][0]))
        right = sorted((b for b in zone if b["column"] == "right"), key=lambda b: (b["bbox"][1], b["bbox"][0]))
        for b in left + right:
            ordered.append(b)
            used.add(id(b))

    for y0, y1, payload in barriers:
        emit_zone(cursor_y, y0)
        typ, obj = payload
        if typ == "block" and id(obj) not in used:
            ordered.append(obj)
            used.add(id(obj))
        cursor_y = max(cursor_y, y1)

    emit_zone(cursor_y, 1e9)
    leftovers = [b for b in blocks if id(b) not in used]
    leftovers.sort(key=lambda b: (b["bbox"][1], 0 if b["column"] == "left" else 1, b["bbox"][0]))
    ordered.extend(leftovers)

    for idx, b in enumerate(ordered):
        b["flow_index"] = idx
    return ordered


def mark_page_continuations(pages):
    for i in range(len(pages) - 1):
        left = [b for b in pages[i]["blocks"] if b.get("kind") == "body"]
        right = [b for b in pages[i + 1]["blocks"] if b.get("kind") == "body"]
        if not left or not right:
            continue
        last = max(left, key=lambda x: x.get("flow_index", -1))
        first = min(right, key=lambda x: x.get("flow_index", 10**9))
        lt, ft = last["text"].rstrip(), first["text"].lstrip()
        if (lt and not lt.endswith(TERMINAL)) or LOWER_START.match(ft):
            last["continues_to_next"] = True
            first["continues_from_prev"] = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("extract_blocks", description="列感知自然段文本抽取")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--tables", default=None)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--min-chars", type=int, default=2)
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    src = Path(args.input).resolve()
    doc = fitz.open(src)
    selected = parse_pages(args.pages, doc.page_count)
    table_map = load_table_regions(args.tables)

    result = {
        "schema_version": 4,
        "extractor": "column-aware-paragraph-v4",
        "source": str(src),
        "page_count": doc.page_count,
        "pages": [],
    }

    for pno in selected:
        page = doc[pno]
        page_no = pno + 1
        raw = []
        d = page.get_text("dict")
        for raw_index, source_block in enumerate(d.get("blocks", [])):
            if source_block.get("type") != 0:
                continue
            for fragment_index, rb in enumerate(split_raw_block(source_block, page.rect.width)):
                text = block_text(rb)
                if len(text) < args.min_chars:
                    continue
                size, font, bold, has_math, color, math_only = main_metrics(rb)
                bbox = [round(float(v), 2) for v in rb["bbox"]]
                kind = classify_kind(text, bbox, page.rect.width, page.rect.height,
                                     bold, font, math_only, table_map.get(page_no, []))
                column = classify_column(bbox, page.rect.width)
                rec = {
                    "id": f"p{page_no}r{raw_index}s{fragment_index}",
                    "_page": page_no,
                    "bbox": bbox,
                    "size": size,
                    "font": font,
                    "bold": bool(bold),
                    "heading": kind == "heading",
                    "color": color,
                    "math_only": bool(math_only),
                    "has_math": bool(has_math),
                    "chars": len(text),
                    "text": text,
                    "kind": kind,
                    "column": column,
                }
                rec["source_hash"] = source_hash(page_no, bbox, text)
                raw.append(rec)

        mergeable = [b for b in raw if b["kind"] != "table"]
        table_blocks = [b for b in raw if b["kind"] == "table"]
        merged = merge_column_blocks(mergeable)
        ordered = assign_flow(merged + table_blocks, table_map.get(page_no, []), page.rect.width)

        for idx, b in enumerate(ordered):
            b["id"] = f"p{page_no}b{idx}"
            b.pop("_page", None)

        result["pages"].append({
            "page": page_no,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "blocks": ordered,
        })

    mark_page_continuations(result["pages"])
    doc.close()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(len(p["blocks"]) for p in result["pages"])
    body = sum(1 for p in result["pages"] for b in p["blocks"] if b["kind"] == "body")
    print(f"v4 抽取完成: {len(result['pages'])} 页 / {total} 块 / 正文自然段 {body}")
    for p in result["pages"]:
        kinds = {}
        for b in p["blocks"]:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        print(f"  page {p['page']}: {len(p['blocks'])} 块  {kinds}")
    print(f"输出: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
