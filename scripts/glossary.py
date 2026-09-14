#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / glossary.py

单篇翻译的术语表工具：**合并**多份术语表 + **审计**术语落地情况。

CSV 格式: source,target[,tgt_lng]
  - 可由 `kb.py export --format glossary-csv` 生成，也可自己写两列
  - tgt_lng 列兼容保留（留空即可），本技能不读它

子命令:
  merge    合并多个术语表，后面的文件优先级更高
           （典型用法：种子表 → KB 导出 → 自己的人工修订表）
  audit    术语审计：找出"术语表里该用的译法没被用上"的块，输出待重译清单
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

CSV_FIELDS = ["source", "target", "tgt_lng"]


def read_glossary(path: Path) -> list[tuple[str, str, str]]:
    """读 CSV（容忍空行/多余列/BOM）。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")

    rows: list[tuple[str, str, str]] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows
    fields = [f.strip() for f in reader.fieldnames if f]
    if "source" not in fields or "target" not in fields:
        # 可能是无表头的 TSV/CSV
        reader = csv.reader(io.StringIO(text))
        for r in reader:
            if len(r) >= 2 and r[0].strip():
                rows.append((r[0].strip(), r[1].strip(), (r[2].strip() if len(r) > 2 else "")))
        return rows

    for row in reader:
        src = (row.get("source") or "").strip()
        tgt = (row.get("target") or "").strip()
        lng = (row.get("tgt_lng") or "").strip()
        if not src or not tgt:
            continue
        rows.append((src, tgt, lng))
    return rows


def write_glossary(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, doublequote=True)
        w.writeheader()
        for src, tgt, lng in rows:
            w.writerow({"source": src, "target": tgt, "tgt_lng": lng})


def dedupe(rows, later_wins=True):
    """按 normalized source 去重；later_wins=True 时后出现的覆盖先出现的。"""
    order: list[str] = []
    table: dict[str, tuple[str, str, str]] = {}
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    for src, tgt, lng in rows:
        k = norm(src)
        if k not in table:
            order.append(k)
        table[k] = (src, tgt, lng)
    return [table[k] for k in order]


def cmd_merge(a) -> int:
    all_rows: list[tuple[str, str, str]] = []
    for p in a.glossaries:
        pp = Path(p)
        if not pp.is_file():
            print(f"⚠️ 跳过不存在的文件: {pp}")
            continue
        r = read_glossary(pp)
        print(f"  读入 {len(r):5d} 条  {pp}")
        all_rows.extend(r)
    merged = dedupe(all_rows, later_wins=True)
    merged = [(s, t, "") for s, t, _ in merged]
    write_glossary(Path(a.output), merged)
    print(f"合并: 共 {len(all_rows)} 条 -> 去重后 {len(merged)} 条（后面的文件优先）")
    print(f"输出: {Path(a.output).resolve()}")
    return 0


def _term_pattern(term: str) -> str:
    """把术语转成"词边界"正则，避免 USA 命中 usage、RS 命中 cross 之类误报。"""
    esc = re.escape(term.strip().lower())
    esc = esc.replace(r"\ ", r"[\s\-]+")   # 术语内的空格允许写成空格或连字符
    return r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])"


def term_in_text(term: str, text: str) -> bool:
    try:
        return re.search(_term_pattern(term), text.lower()) is not None
    except re.error:
        return term.lower() in text.lower()


def find_conflicts(rows) -> list[tuple[str, list[str]]]:
    """找出术语表内部自相矛盾的条目（同一源词给出多个不同译法）。"""
    table: dict[str, set] = {}
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    for src, tgt, _ in rows:
        table.setdefault(norm(src), set()).add(tgt)
    return [(k, sorted(v)) for k, v in table.items() if len(v) > 1]


def cmd_audit(a) -> int:
    """术语审计：找出"术语表里该用的译法没被用上"的块，输出待重译清单。

    注意：自动抽取的术语表常有错条目（比如把 RS 抽成"随机搜索"，
    而文中 RS 其实是 Reward Shaping）。所以审计会同时报出
    术语表自身的可疑条目与内部冲突 —— 覆盖率低往往不是译文差，
    而是术语表本身需要先人工梳理。
    """
    rows = read_glossary(Path(a.glossary))
    bd = json.loads(Path(a.blocks).read_text(encoding="utf-8"))
    tr = json.loads(Path(a.translations).read_text(encoding="utf-8"))

    def ns(s):
        return re.sub(r"\s+", "", s or "")

    zh_by_id = {k: ns(v.get("zh")) for k, v in tr.items() if (v or {}).get("zh")}
    all_zh = "".join(zh_by_id.values())
    blk_text = {
        b["id"]: re.sub(r"\s+", " ", b["text"])
        for p in bd.get("pages", []) for b in p.get("blocks", [])
        if b["id"] in zh_by_id
    }

    conflicts = find_conflicts(rows)

    misses: dict[str, list[str]] = {}
    for src, tgt, _ in rows:
        if not tgt or ns(tgt) in all_zh:
            continue
        hit_blocks = [bid for bid, txt in blk_text.items() if term_in_text(src, txt)]
        if hit_blocks:
            misses[f"{src} -> {tgt}"] = hit_blocks

    affected_blocks: set[str] = set()
    for blks in misses.values():
        affected_blocks.update(blks)

    print(f"术语表条目: {len(rows)}；已译块: {len(zh_by_id)}")
    print(f"未落地且源词确实出现的术语: {len(misses)} 条")
    print(f"涉及待重译块: {len(affected_blocks)} 个")
    print("-" * 72)
    ranked = sorted(misses.items(), key=lambda kv: -len(kv[1]))
    for term, blks in ranked[: int(a.top)]:
        print(f"  {term:<58} 影响 {len(blks)} 块: {','.join(blks[:6])}")

    if conflicts:
        print("-" * 72)
        print(f"⚠️ 术语表内部冲突 {len(conflicts)} 组（同一源词多个译法，需人工裁决）:")
        for src, tgts in conflicts[:15]:
            print(f"  {src}: {' | '.join(tgts)}")
        print("  → 这类冲突多半是自动抽取造成的，先把术语表梳理干净再谈覆盖率。")

    print("-" * 72)
    print("提醒: 覆盖率低 ≠ 译文差。若上面出现明显错条目（如 RS→随机搜索），")
    print("      说明该术语表本身需要先人工整理（改 kb 的 glossary.csv 或合并一份修订表）。")

    if a.out:
        out = {
            "affected_blocks": sorted(affected_blocks),
            "terms": {k: v for k, v in ranked},
            "glossary_conflicts": [{"source": s, "targets": t} for s, t in conflicts],
        }
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细已写入: {Path(a.out).resolve()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("glossary", description="术语表工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("merge", help="合并术语表（后者优先）")
    p.add_argument("--glossaries", nargs="+", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("audit", help="术语审计：定位该用未用的术语及其所在块")
    p.add_argument("--glossary", required=True)
    p.add_argument("--blocks", required=True, help="blocks.json")
    p.add_argument("--translations", required=True, help="translations.json")
    p.add_argument("--top", type=int, default=40, help="展示前 N 条术语")
    p.add_argument("--out", default=None, help="把明细写成 JSON")
    p.add_argument("--verbose", action="store_true", help="同时打印待重译块清单")
    p.set_defaults(func=cmd_audit)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
