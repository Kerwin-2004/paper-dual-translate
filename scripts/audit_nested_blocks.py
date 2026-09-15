#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / audit_nested_blocks.py

审计 blocks.json 里的「bbox 嵌套簇」：一个段落块的 bbox 包住了夹在行间的
碎片块（行内公式、列表残行等）。这种簇直接翻译会导致：
  1) redaction 抹掉段落时把碎片原文一并删除（公式丢失）；
  2) 碎片与相邻译文块互相叠印。

处理方案（--apply 自动执行）：
  - 簇内所有块合并为**单个插入块**：外层块 bbox 扩到并集；
  - 碎片块标记 "nested_in": <外层块id>，build_dual 会自动跳过它们；
  - 外层块记录有序 inline_fragments，供自动翻译以不可丢失标记注入。

用法:
  python audit_nested_blocks.py --blocks blocks.json                 # 只报告
  python audit_nested_blocks.py --blocks blocks.json --apply         # 修补 blocks.json
  python audit_nested_blocks.py --blocks blocks.json --skeleton sk.json  # 生成翻译骨架
可选: --translations translations.json（骨架只为尚无译文的簇生成）
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


def rect_of(b: dict) -> tuple[float, float, float, float]:
    return tuple(b["bbox"])


def inter_area(a, c) -> float:
    x0, y0 = max(a[0], c[0]), max(a[1], c[1])
    x1, y1 = min(a[2], c[2]), min(a[3], c[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def area(a) -> float:
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


def _nearest_text_boundary(text: str, offset: int) -> int:
    offset = max(0, min(len(text), int(offset)))
    boundaries = {0, len(text)}
    for index, char in enumerate(text):
        if char.isspace() or char in ",.;:!?()[]{}，。；：！？（）【】":
            boundaries.update((index, index + 1))
    return min(boundaries, key=lambda value: (abs(value - offset), value))


def fragment_anchor_offset(outer: dict, fragment: dict) -> int:
    """Estimate the fragment's semantic character position from PDF geometry."""
    text = str(outer.get("text", ""))
    if not text:
        return 0
    fb = fragment.get("bbox") or (0, 0, 0, 0)
    fx = (float(fb[0]) + float(fb[2])) / 2
    fy = (float(fb[1]) + float(fb[3])) / 2
    lines = [line for line in outer.get("source_lines", [])
             if line.get("bbox") and len(line["bbox"]) == 4]
    if lines:
        lines.sort(key=lambda line: (float(line["bbox"][1]), float(line["bbox"][0])))
        line_texts = [re.sub(r"\s+", " ", str(line.get("text", ""))).strip()
                      for line in lines]
        nominal_total = max(1, sum(len(value) + 1 for value in line_texts) - 1)
        search_cursor = 0
        line_starts = []
        for index, value in enumerate(line_texts):
            found = text.find(value, search_cursor) if value else -1
            if found >= 0:
                line_starts.append(found)
                search_cursor = found + len(value)
            else:
                before = sum(len(item) + 1 for item in line_texts[:index])
                line_starts.append(round(before / nominal_total * len(text)))

        fragment_y0, fragment_y1 = float(fb[1]), float(fb[3])
        overlaps = []
        for index, line in enumerate(lines):
            line_y0, line_y1 = float(line["bbox"][1]), float(line["bbox"][3])
            overlap = max(0.0, min(fragment_y1, line_y1) - max(fragment_y0, line_y0))
            if overlap > 0:
                overlaps.append((overlap, index))
        if not overlaps:
            if fragment_y1 <= float(lines[0]["bbox"][1]):
                return 0
            if fragment_y0 >= float(lines[-1]["bbox"][3]):
                return len(text)
            preceding = [index for index, line in enumerate(lines)
                         if float(line["bbox"][3]) <= fy]
            if not preceding:
                return 0
            index = preceding[-1]
            return _nearest_text_boundary(
                text, line_starts[index] + len(line_texts[index]))

        target_index = max(overlaps)[1]
        target = lines[target_index]
        line_text = line_texts[target_index]
        line_start = line_starts[target_index]

        spans = sorted(
            (span for span in target.get("spans", [])
             if span.get("bbox") and len(span["bbox"]) == 4),
            key=lambda span: float(span["bbox"][0]),
        )
        local = 0
        for span in spans:
            span_text = str(span.get("text", ""))
            x0, x1 = float(span["bbox"][0]), float(span["bbox"][2])
            if fx <= x0:
                break
            if x0 < fx < x1 and x1 > x0:
                local += round(len(span_text) * (fx - x0) / (x1 - x0))
                break
            local += len(span_text)
        return _nearest_text_boundary(text, line_start + min(local, len(line_text)))

    x0, y0, x1, y1 = [float(value) for value in outer.get("bbox", (0, 0, 1, 1))]
    line_height = max(1.0, float(outer.get("size", 9.0) or 9.0) * 1.25)
    line_count = max(1, round(max(line_height, y1 - y0) / line_height))
    row = max(0, min(line_count - 1, int((fy - y0) / line_height)))
    x_ratio = max(0.0, min(1.0, (fx - x0) / max(1.0, x1 - x0)))
    estimate = round(((row + x_ratio) / line_count) * len(text))
    return _nearest_text_boundary(text, estimate)


def find_clusters(pages: list[dict], min_ratio: float) -> dict[str, dict]:
    """返回 {外层块id: {"outer": block, "frags": [block, ...]}}。

    判定：交集面积 > min_ratio × min(两块面积)，且 B 面积 < A 面积 → B 是 A 的碎片。
    碎片的碎片会向上追溯到最外层。
    """
    frag_of: dict[str, str] = {}  # frag_id -> outer_id
    clusters: dict[str, dict] = {}

    for p in pages:
        blks = p.get("blocks", [])
        for a in blks:
            ra = rect_of(a)
            for b in blks:
                if b["id"] == a["id"]:
                    continue
                rb = rect_of(b)
                ia = inter_area(ra, rb)
                if ia <= 0:
                    continue
                if ia > min_ratio * min(area(ra), area(rb)) and area(rb) < area(ra):
                    # b 嵌在 a 里：若 b 已认了更大的外层则保留更大者
                    cur = frag_of.get(b["id"])
                    if cur is None or area(rect_of(next(x for x in blks if x["id"] == cur))) < area(ra):
                        frag_of[b["id"]] = a["id"]

    by_id = {b["id"]: b for p in pages for b in p.get("blocks", [])}
    # 追溯到最外层 + 压平链
    for fid, oid in frag_of.items():
        seen = {fid}
        while oid in frag_of and oid not in seen:
            seen.add(oid)
            oid = frag_of[oid]
        cl = clusters.setdefault(oid, {"outer": by_id[oid], "frags": []})
        cl["frags"].append(by_id[fid])

    for cl in clusters.values():
        cl["frags"].sort(key=lambda b: b["bbox"][1])
    return clusters


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("audit_nested_blocks", description="bbox 嵌套簇审计/修补")
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="修补 blocks.json（扩 bbox、标 nested_in、计算行内锚点）")
    ap.add_argument("--skeleton", default=None, help="生成翻译骨架 JSON 路径")
    ap.add_argument("--translations", default=None, help="translations.json（骨架只补缺）")
    ap.add_argument("--min-ratio", type=float, default=0.3,
                    help="交集/min(面积) 阈值，默认 0.3")
    args = ap.parse_args(argv)

    f = Path(args.blocks).resolve()
    data = json.loads(f.read_text(encoding="utf-8"))
    block_pages = {
        block["id"]: int(page.get("page", 0))
        for page in data.get("pages", []) for block in page.get("blocks", [])
    }
    trans = {}
    if args.translations and Path(args.translations).is_file():
        trans = json.loads(Path(args.translations).read_text(encoding="utf-8"))

    clusters = find_clusters(data.get("pages", []), args.min_ratio)
    if not clusters:
        print("✅ 未发现 bbox 嵌套簇")
        return 0

    # 外层是纯公式块且无译文 → 公式内部结构，无需合并（噪音）
    def actionable(cl) -> bool:
        o = cl["outer"]
        return not o.get("math_only") or bool((trans.get(o["id"], {}) or {}).get("zh"))

    n_act = 0
    print(f"发现 {len(clusters)} 个嵌套簇:")
    for oid, cl in sorted(clusters.items()):
        o = cl["outer"]
        t = trans.get(oid, {})
        if o.get("math_only") and not (t.get("zh") or "").strip():
            print(f"  (公式内部，忽略) {oid} {o['text'][:40]!r}（{len(cl['frags'])} 碎片）")
            continue
        n_act += 1
        status = "已有译文" if (t.get("zh") or "").strip() else "未翻译"
        print(f"  [{status}] {oid} bbox={[round(v, 1) for v in o['bbox']]}")
        print(f"        原文: {o['text'][:70]!r}")
        for fr in cl["frags"]:
            ft = trans.get(fr["id"], {})
            fs = "有译文" if (ft.get("zh") or "").strip() else ("已 skip" if ft.get("skip") else "无条目")
            math = "(math)" if fr.get("math_only") or fr.get("has_math") else ""
            print(f"        碎片 {fr['id']} {math} [{fs}] {fr['text'][:50]!r}")

    if args.apply:
        n_bbox = n_mark = n_inline = 0
        for oid, cl in clusters.items():
            if not actionable(cl):
                continue  # 纯公式内部结构，不动 blocks.json
            xs0 = min([cl["outer"]["bbox"][0]] + [x["bbox"][0] for x in cl["frags"]])
            ys0 = min([cl["outer"]["bbox"][1]] + [x["bbox"][1] for x in cl["frags"]])
            xs1 = max([cl["outer"]["bbox"][2]] + [x["bbox"][2] for x in cl["frags"]])
            ys1 = max([cl["outer"]["bbox"][3]] + [x["bbox"][3] for x in cl["frags"]])
            if [round(v, 2) for v in cl["outer"]["bbox"]] != [round(v, 2) for v in (xs0, ys0, xs1, ys1)]:
                cl["outer"]["bbox"] = [round(xs0, 2), round(ys0, 2), round(xs1, 2), round(ys1, 2)]
                n_bbox += 1
            for fr in cl["frags"]:
                if not fr.get("nested_in"):
                    fr["nested_in"] = oid
                    n_mark += 1
            inline_fragments = [{
                "id": fr["id"],
                "text": fr.get("text", ""),
                "bbox": [round(float(v), 2) for v in fr.get("bbox", ())],
                "kind": fr.get("kind"),
                "math_only": bool(fr.get("math_only")),
                "has_math": bool(fr.get("has_math")),
                "anchor_offset": fragment_anchor_offset(cl["outer"], fr),
                "page": block_pages.get(fr["id"]),
            } for fr in sorted(
                cl["frags"], key=lambda item: (
                    float(item["bbox"][1]), float(item["bbox"][0]), item["id"]))]
            if cl["outer"].get("inline_fragments") != inline_fragments:
                cl["outer"]["inline_fragments"] = inline_fragments
                n_inline += len(inline_fragments)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已修补 {f}: 扩 bbox {n_bbox} 处、标记碎片 {n_mark} 个、"
              f"挂接 inline fragment {n_inline} 个")

    if args.skeleton:
        sk = {}
        for oid, cl in clusters.items():
            if not actionable(cl):
                continue
            if not (trans.get(oid, {}).get("zh") or "").strip():
                sk[oid] = {"zh": ""}
            for fr in cl["frags"]:
                if not trans.get(fr["id"]):
                    sk[fr["id"]] = {"skip": True}
        Path(args.skeleton).resolve().write_text(
            json.dumps(sk, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"骨架: {Path(args.skeleton).resolve()}（{len(sk)} 条，填 zh 后随 trans_p*.json 一起合并）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
