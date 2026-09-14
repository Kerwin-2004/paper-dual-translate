#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / merge_paragraphs.py

段落级断句：把「被切碎的段落块」合并回整段，让译文按段落而不是按英文行
切片来写。合并后译文一个块对应一个完整段落，中文行文更连贯，
build_dual 也有更大的排版空间（配合 --font-scale 自动放大中文字号）。

判定（同页、同列、自上而下的相邻块 A、B）：
  - 几何：垂直间距 gap = B.y0 - A.y1 ≤ min_ratio × min(字号)；
    实测 Elsevier 双栏论文里段落续块 gap/size ≤ 0.48、真段落分隔 ≥ 0.88，
    默认 0.6 留有余量。水平方向需同列（x 重叠 > 0.5×min 宽度）。
  - 文本（续句信号，二者取一）：A 不以句末标点结尾（. ! ? : ;)，
    或 B 以小写字母开头。两信号皆无 → 视为真正的段落起始，不合并。
  - 排除：heading/bold 块、math_only 块、nested_in 碎片、字号差 > 0.6pt。

跨行间公式的段落（A 文本 → 公式块 → B 文本）**不**机械合并——
合并 bbox 会让 redaction 抹掉公式。这类段落按整段翻译后拆回两个槽位，
公式用 ASCII 记号并入中文流（见 SKILL.md「断句与段落」）。

用法:
  python merge_paragraphs.py --blocks blocks.json                 # 报告将合并哪些
  python merge_paragraphs.py --blocks blocks.json --apply         # 执行合并
  # 已有译文时加 --translations translations.json：已有 zh 的成员跳过合并（不破坏现有翻译）
  # --force 可强行合并已有译文的块（需自行重写受影响条目）
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

TERMINAL = tuple(".!?;:")  # 英文句末标点（含冒号引出列表的情形不合并）
LOWER_START = re.compile(r"^[a-z]")


def same_column(a: dict, b: dict) -> bool:
    ov = max(0.0, min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0]))
    w = min(a["bbox"][2] - a["bbox"][0], b["bbox"][2] - b["bbox"][0])
    return w > 0 and ov > 0.5 * w


def continuable(a: dict, b: dict, min_ratio: float) -> bool:
    """几何 + 文本续句信号综合判定 a、b 是同一段落被切碎的两半。"""
    if a.get("heading") or b.get("heading") or a.get("bold") or b.get("bold"):
        return False
    if a.get("math_only") or b.get("math_only"):
        return False
    if a.get("nested_in") or b.get("nested_in"):
        return False
    if abs(a["size"] - b["size"]) > 0.6:
        return False
    if not same_column(a, b):
        return False
    gap = b["bbox"][1] - a["bbox"][3]
    sz = min(a["size"], b["size"])
    if gap > min_ratio * sz:
        return False
    ta = a["text"].rstrip()
    tb = b["text"].lstrip()
    if not ta.endswith(TERMINAL):
        return True          # 上半句没写完 → 必然续段
    if LOWER_START.match(tb):
        return True          # 下一块小写开头 → 是句中接续
    return False             # 两者皆无 → 新段落起始


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("merge_paragraphs", description="段落块合并（段落级断句）")
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--apply", action="store_true", help="执行合并（写回 blocks.json）")
    ap.add_argument("--translations", default=None, help="已有译文的成员默认跳过合并")
    ap.add_argument("--tables", default=None,
                    help="tables.json；落在表格区域内的块不参与合并")
    ap.add_argument("--force", action="store_true", help="强行合并已有译文的块")
    ap.add_argument("--min-ratio", type=float, default=0.6,
                    help="合并阈值：gap ≤ 该值 × min(字号)，默认 0.6")
    args = ap.parse_args(argv)

    f = Path(args.blocks).resolve()
    data = json.loads(f.read_text(encoding="utf-8"))
    trans = {}
    if args.translations and Path(args.translations).is_file():
        trans = json.loads(Path(args.translations).read_text(encoding="utf-8"))

    # 表格区域（带小边距）：区域内块不参与段落合并（表格数据行不是段落）
    regions: dict[int, list[tuple[float, float, float, float]]] = {}
    if args.tables and Path(args.tables).is_file():
        td = json.loads(Path(args.tables).read_text(encoding="utf-8"))
        M = 6.0
        for p in td.get("pages", []):
            regs = []
            for t in p.get("tables", []):
                r = t.get("region")
                if r:
                    regs.append((r[0] - M, r[1] - M, r[2] + M, r[3] + M))
            regions[p["page"]] = regs

    def in_table(b: dict, page: int) -> bool:
        bx = b["bbox"]
        for r in regions.get(page, []):
            if bx[0] < r[2] and bx[2] > r[0] and bx[1] < r[3] and bx[3] > r[1]:
                return True
        return False

    n_merge = n_skip_trans = 0
    merged_blocks: dict[str, dict] = {}
    drop_ids: set[str] = set()

    for p in data.get("pages", []):
        blks = [x for x in p.get("blocks", [])
                if not x.get("nested_in") and not in_table(x, p["page"])]
        # 列锚点聚类：x0 相差 ≤ 40pt 视为同一列（容忍段首缩进）；链在列内自上而下建立
        cols: list[list[dict]] = []
        for b in sorted(blks, key=lambda x: x["bbox"][0]):
            for col in cols:
                if abs(b["bbox"][0] - col[0]["bbox"][0]) <= 40:
                    col.append(b)
                    break
            else:
                cols.append([b])
        runs: list[list[dict]] = []
        for col in cols:
            col_runs: list[list[dict]] = []
            for b in sorted(col, key=lambda x: x["bbox"][1]):
                if col_runs:
                    tail = col_runs[-1][-1]
                    gap = b["bbox"][1] - tail["bbox"][3]
                    sz = min(tail["size"], b["size"])
                    if gap <= args.min_ratio * sz and continuable(tail, b, args.min_ratio):
                        col_runs[-1].append(b)
                        continue
                col_runs.append([b])
            runs.extend(r for r in col_runs if len(r) >= 2)

        for run in runs:
            if len(run) < 2:
                continue
            head, members = run[0], run[1:]
            if not args.force and any((trans.get(m["id"], {}) or {}).get("zh") for m in members):
                n_skip_trans += 1
                continue
            n_merge += 1
            print(f"  合并 {' + '.join(m['id'] for m in members)} -> {head['id']}"
                  f"  (p{p['page']})")
            print(f"    {head['text'][-40:]!r} || {members[0]['text'][:40]!r} ...")
            x0 = min([head["bbox"][0]] + [m["bbox"][0] for m in members])
            y0 = min([head["bbox"][1]] + [m["bbox"][1] for m in members])
            x1 = max([head["bbox"][2]] + [m["bbox"][2] for m in members])
            y1 = max([head["bbox"][3]] + [m["bbox"][3] for m in members])
            parts = [head["text"].rstrip()]
            for m in members:
                mt = m["text"].rstrip()
                if parts and parts[-1].endswith("-") and mt[:1].islower():
                    parts[-1] = parts[-1][:-1]      # 连字符断行合并
                else:
                    parts.append(mt)
            head["text"] = " ".join(parts)
            head["bbox"] = [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]
            head["chars"] = len(head["text"])
            merged_blocks[head["id"]] = head
            for m in members:
                m["nested_in"] = head["id"]
                drop_ids.add(m["id"])

    if not args.apply:
        print(f"\n报告模式：共 {n_merge} 处可合并"
              + (f"，{n_skip_trans} 处因已有译文跳过" if n_skip_trans else "")
              + "。加 --apply 执行。")
        return 0

    # 写回：从 blocks 列表里移除被合并的成员（head 保留、已更新 bbox/text）
    for p in data.get("pages", []):
        p["blocks"] = [x for x in p["blocks"] if x["id"] not in drop_ids]
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写回 {f}: 合并 {n_merge} 组、移除 {len(drop_ids)} 个成员块")
    print("提醒: 翻译时 head 块的 zh 要覆盖整段内容；被合并块如已有译文请删除其条目。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
