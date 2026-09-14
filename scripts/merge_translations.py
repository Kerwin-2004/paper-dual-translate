#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / merge_translations.py

把按页拆分的译文文件（work/trans_p1.json、trans_p2_3.json …）合并成
build_dual.py 需要的 translations.json。同名块 id 在多个文件出现且内容
不一致时报为冲突（保留最后读入的值，退出码 1）。

用法:
  python merge_translations.py --dir work --output work/translations.json
  # --pattern 默认 "trans_p*.json"；table_trans.json 不会被匹配到
"""
from __future__ import annotations

import argparse
import json
import sys
try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("merge_translations", description="合并分页译文")
    ap.add_argument("--dir", default=".", help="译文文件所在目录")
    ap.add_argument("--pattern", default="trans_p*.json")
    ap.add_argument("--output", default=None, help="默认 <dir>/translations.json")
    args = ap.parse_args(argv)

    d = Path(args.dir).resolve()
    files = sorted(d.glob(args.pattern))
    if not files:
        print(f"❌ {d} 下没有匹配 {args.pattern} 的文件")
        return 2

    merged: dict = {}
    conflicts = 0
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in merged and merged[k] != v:
                conflicts += 1
                print(f"⚠️ 冲突 {k}: {f.name} 覆盖 {merged[k]!r} -> {v!r}")
            merged[k] = v

    out = Path(args.output).resolve() if args.output else d / "translations.json"
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")

    n_files = len(files)
    n_zh = sum(1 for v in merged.values() if (v.get("zh") or "").strip())
    n_skip = sum(1 for v in merged.values() if v.get("skip"))
    n_blank = sum(1 for v in merged.values() if v.get("blank"))
    print(f"合并 {n_files} 个文件 -> {out}")
    print(f"共 {len(merged)} 条: 译文 {n_zh} / skip {n_skip} / blank {n_blank}"
          + (f" / 其他 {len(merged) - n_zh - n_skip - n_blank}" if len(merged) != n_zh + n_skip + n_blank else ""))
    if conflicts:
        print(f"❌ {conflicts} 处冲突，请检查上面的清单")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
