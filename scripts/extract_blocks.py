#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / extract_blocks.py

Column-aware paragraph extractor:
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


def layout_uid(page: int, column: str, bbox: list[float], text: str) -> str:
    if PROV is not None:
        return PROV.block_layout_uid(page, column, bbox, text)
    norm = re.sub(r"\s+", " ", (text or "").strip())
    coords = ",".join(f"{round(float(v) * 2) / 2:.1f}" for v in bbox)
    return hashlib.sha256(f"{int(page)}\0{column}\0{coords}\0{norm}".encode("utf-8")).hexdigest()[:24]


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
    # Captions remain independent translation units even when a loose table
    # detector includes part of the caption in its region.
    if CAPTION_TABLE.match(text):
        return "table_caption"
    if CAPTION_FIG.match(text):
        return "figure_caption"
    if any(overlap_ratio(bbox, r) >= 0.25 for r in table_regions):
        return "table"
    if PAGE_NO.match(text):
        return "meta"
    if len(text) < 160 and META.search(text):
        return "meta"
    if REF_HEAD.match(text):
        return "reference_heading"
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


def _line_text(line: dict) -> str:
    return clean_text("".join(span.get("text", "") for span in line.get("spans", [])))


def _line_size(line: dict) -> float:
    sizes = [(float(span.get("size", 0) or 0), max(1, len(span.get("text", ""))))
             for span in line.get("spans", [])]
    if not sizes:
        return 9.0
    totals: dict[float, int] = {}
    for size, weight in sizes:
        totals[round(size, 1)] = totals.get(round(size, 1), 0) + weight
    return max(totals.items(), key=lambda item: item[1])[0]


def split_line_runs(lines: list[dict]) -> list[list[dict]]:
    """Split same-column physical lines into paragraph-like runs."""
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    runs: list[list[dict]] = []
    for line in ordered:
        if not runs:
            runs.append([line])
            continue
        prev = runs[-1][-1]
        prev_size, size = _line_size(prev), _line_size(line)
        gap = float(line["bbox"][1]) - float(prev["bbox"][3])
        indent = float(line["bbox"][0]) - float(prev["bbox"][0])
        prev_text, text = _line_text(prev).rstrip(), _line_text(line).lstrip()
        starts_structure = bool(LIST_START.match(text))
        new_indented_paragraph = (
            indent >= max(7.0, 0.7 * min(prev_size, size))
            and prev_text.endswith(TERMINAL)
        )
        if (abs(prev_size - size) > 1.0 or gap > 1.1 * max(5.0, min(prev_size, size))
                or starts_structure or new_indented_paragraph):
            runs.append([line])
        else:
            runs[-1].append(line)
    return runs


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
    has_columns = bool(by_column["left"] and by_column["right"])
    has_full_and_column = bool(by_column["full"] and (by_column["left"] or by_column["right"]))
    if not (has_columns or has_full_and_column):
        return [block]

    fragments = []
    for column in ("left", "right", "full"):
        selected = by_column[column]
        if not selected:
            continue
        for run in split_line_runs(selected):
            x0 = min(float(line["bbox"][0]) for line in run)
            y0 = min(float(line["bbox"][1]) for line in run)
            x1 = max(float(line["bbox"][2]) for line in run)
            y1 = max(float(line["bbox"][3]) for line in run)
            fragment = dict(block)
            fragment["bbox"] = (x0, y0, x1, y1)
            fragment["lines"] = run
            fragments.append(fragment)
    return sorted(fragments, key=lambda fragment: (fragment["bbox"][1], fragment["bbox"][0]))


def split_at_table_boundaries(block: dict, table_regions: list[list[float]],
                              min_x_overlap=0.25) -> list[dict]:
    """Split at table edges only when the fragment overlaps that table in x."""
    lines = [line for line in block.get("lines", []) if line.get("bbox")]
    bx0, by0, bx1, by1 = [float(value) for value in block["bbox"]]
    cuts = []
    for rx0, ry0, rx1, ry1 in table_regions:
        overlap = max(0.0, min(bx1, rx1) - max(bx0, rx0))
        ratio = overlap / max(1e-6, min(bx1 - bx0, rx1 - rx0))
        if ratio < min_x_overlap:
            continue
        cuts.extend(edge for edge in (float(ry0), float(ry1)) if by0 < edge < by1)
    cuts = sorted(set(cuts))
    if len(lines) < 2 or not cuts:
        return [block]
    groups: dict[int, list[dict]] = {}
    for line in lines:
        center = (float(line["bbox"][1]) + float(line["bbox"][3])) / 2
        band = sum(center >= cut for cut in cuts)
        groups.setdefault(band, []).append(line)
    if len(groups) < 2:
        return [block]
    fragments = []
    for band in sorted(groups):
        selected = groups[band]
        fragment = dict(block)
        fragment["lines"] = selected
        fragment["bbox"] = (
            min(float(line["bbox"][0]) for line in selected),
            min(float(line["bbox"][1]) for line in selected),
            max(float(line["bbox"][2]) for line in selected),
            max(float(line["bbox"][3]) for line in selected),
        )
        fragments.append(fragment)
    return fragments


def cluster_object_rects(rects: list[list[float]], padding: float = 24.0) -> list[list[float]]:
    """Union nearby drawing/image rectangles into composite figure regions."""
    groups: list[list[float]] = []
    for rect in rects:
        x0, y0, x1, y1 = [float(v) for v in rect]
        if x1 - x0 <= 2.0 and y1 - y0 <= 2.0:
            continue
        merged = [x0, y0, x1, y1]
        changed = True
        while changed:
            changed = False
            remaining = []
            for group in groups:
                touches = not (
                    merged[2] + padding < group[0] or group[2] + padding < merged[0]
                    or merged[3] + padding < group[1] or group[3] + padding < merged[1]
                )
                if touches:
                    merged = [min(merged[0], group[0]), min(merged[1], group[1]),
                              max(merged[2], group[2]), max(merged[3], group[3])]
                    changed = True
                else:
                    remaining.append(group)
            groups = remaining
        groups.append(merged)
    return groups


def detect_layout_barriers(page, table_regions: list[list[float]]) -> list[dict]:
    """Find wide non-text objects that split upper and lower reading zones."""
    page_w, page_h = float(page.rect.width), float(page.rect.height)
    candidates: list[dict] = [
        {"kind": "table", "bbox": [float(v) for v in region]}
        for region in table_regions
        if (float(region[2]) - float(region[0])) >= page_w * 0.55
    ]
    image_rects = []
    try:
        for info in page.get_image_info():
            bbox = [float(v) for v in info.get("bbox", ())]
            if len(bbox) == 4:
                image_rects.append(bbox)
    except (AttributeError, RuntimeError, ValueError):
        pass
    candidates.extend({"kind": "image", "bbox": bbox}
                      for bbox in cluster_object_rects(image_rects))
    drawing_rects = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is not None:
                drawing_rects.append([rect.x0, rect.y0, rect.x1, rect.y1])
    except (AttributeError, RuntimeError, ValueError):
        pass
    candidates.extend({"kind": "vector", "bbox": bbox}
                      for bbox in cluster_object_rects(drawing_rects))

    out = []
    for candidate in candidates:
        x0, y0, x1, y1 = candidate["bbox"]
        width, height = x1 - x0, y1 - y0
        area_ratio = max(0.0, width * height) / max(1.0, page_w * page_h)
        if width < page_w * 0.55 or height < 10.0 or area_ratio > 0.72:
            continue
        bbox = [round(v, 2) for v in (x0, y0, x1, y1)]
        if any(existing["kind"] == candidate["kind"]
               and all(abs(a - b) <= 1.0 for a, b in zip(existing["bbox"], bbox))
               for existing in out):
            continue
        out.append({"kind": candidate["kind"], "bbox": bbox})
    return sorted(out, key=lambda item: (item["bbox"][1], item["bbox"][0]))


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

    indent_delta = float(b["bbox"][0]) - float(a["bbox"][0])
    if LIST_START.match(tb):
        return False
    if indent_delta >= 8.0 and ta.endswith(TERMINAL):
        return False

    if gap <= 0.48 * size:
        return True
    if ta and not ta.endswith(TERMINAL):
        return True
    if LOWER_START.match(tb):
        return True

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


def assign_flow(blocks: list[dict], layout_barriers: list[dict], page_w: float) -> list[dict]:
    """Wide objects form horizontal barriers; inside each zone read left column then right."""
    full = [b for b in blocks if b["column"] == "full"]
    barriers = [(float(b["bbox"][1]), float(b["bbox"][3]), ("block", b)) for b in full]
    for barrier in layout_barriers:
        r = barrier["bbox"]
        barriers.append((float(r[1]), float(r[3]), (barrier.get("kind", "layout"), r)))
    barriers.sort(key=lambda x: (x[0], x[1]))

    non_full = [b for b in blocks if b["column"] != "full"]
    ordered, used = [], set()
    cursor_y = -1e9

    def emit_zone(y0, y1, break_before=False):
        zone = [
            b for b in non_full if id(b) not in used
            and y0 <= ((b["bbox"][1] + b["bbox"][3]) / 2) < y1
        ]
        left = sorted((b for b in zone if b["column"] == "left"), key=lambda b: (b["bbox"][1], b["bbox"][0]))
        right = sorted((b for b in zone if b["column"] == "right"), key=lambda b: (b["bbox"][1], b["bbox"][0]))
        sequence = left + right
        if break_before and sequence and ordered:
            sequence[0]["flow_break"] = True
        for b in sequence:
            ordered.append(b)
            used.add(id(b))

    pending_break = False
    for y0, y1, payload in barriers:
        emit_zone(cursor_y, y0, pending_break)
        typ, obj = payload
        if typ == "block" and id(obj) not in used:
            obj["flow_break"] = True
            ordered.append(obj)
            used.add(id(obj))
        pending_break = True
        cursor_y = max(cursor_y, y1)

    emit_zone(cursor_y, 1e9, pending_break)
    leftovers = [b for b in blocks if id(b) not in used]
    leftovers.sort(key=lambda b: (b["bbox"][1], 0 if b["column"] == "left" else 1, b["bbox"][0]))
    ordered.extend(leftovers)

    for idx, b in enumerate(ordered):
        b["flow_index"] = idx
        if b.get("kind") in {"heading", "table", "table_caption", "figure_caption"}:
            b["flow_break"] = True
    return ordered


def mark_page_continuations(pages):
    for page in pages:
        for block in page.get("blocks", []):
            block.pop("continues_to_next", None)
            block.pop("continues_from_prev", None)

    def substantive(page):
        return [
            block for block in sorted(
                page.get("blocks", []), key=lambda item: item.get("flow_index", 10**9))
            if block.get("kind") != "meta"
        ]

    for i in range(len(pages) - 1):
        if int(pages[i + 1].get("page", 0)) != int(pages[i].get("page", 0)) + 1:
            continue
        left = substantive(pages[i])
        right = substantive(pages[i + 1])
        if not left or not right:
            continue
        last, first = left[-1], right[0]
        if last.get("kind") != "body" or first.get("kind") != "body":
            continue
        if first.get("flow_break"):
            continue
        last_bottom = float(last.get("bbox", (0, 0, 0, 0))[3])
        first_top = float(first.get("bbox", (0, 0, 0, 0))[1])
        barrier_after_last = any(
            float(item.get("bbox", (0, 0, 0, 0))[1]) >= last_bottom - 1
            for item in pages[i].get("layout_barriers", []))
        barrier_before_first = any(
            float(item.get("bbox", (0, 0, 0, 0))[3]) <= first_top + 1
            for item in pages[i + 1].get("layout_barriers", []))
        if barrier_after_last or barrier_before_first:
            continue
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
        "extractor": "column-aware-paragraph-layout-barriers",
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
            fragments = []
            for column_fragment in split_raw_block(source_block, page.rect.width):
                fragments.extend(split_at_table_boundaries(
                    column_fragment, table_map.get(page_no, [])))
            for fragment_index, rb in enumerate(fragments):
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
        layout_barriers = detect_layout_barriers(page, table_map.get(page_no, []))
        ordered = assign_flow(merged + table_blocks, layout_barriers, page.rect.width)

        for idx, b in enumerate(ordered):
            b["id"] = f"p{page_no}b{idx}"
            b.pop("_page", None)
            b["source_hash"] = source_hash(page_no, b["bbox"], b["text"])
            b["layout_uid"] = layout_uid(page_no, b["column"], b["bbox"], b["text"])

        result["pages"].append({
            "page": page_no,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "layout_barriers": layout_barriers,
            "blocks": ordered,
        })

    mark_page_continuations(result["pages"])
    doc.close()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(len(p["blocks"]) for p in result["pages"])
    body = sum(1 for p in result["pages"] for b in p["blocks"] if b["kind"] == "body")
    print(f"抽取完成: {len(result['pages'])} 页 / {total} 块 / 正文自然段 {body}")
    for p in result["pages"]:
        kinds = {}
        for b in p["blocks"]:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        print(f"  page {p['page']}: {len(p['blocks'])} 块  {kinds}")
    print(f"输出: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
