#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / verify_render.py

逐块渲染完整性验证：每块译文的首/尾片段必须能在成品 PDF 的对应区域
提取到。用于排查「疑似截断/丢字」——区分是**文件真的丢字**（构建缺陷，
如 html 回退静默截断）还是**查看窗口裁剪**（整页宽段落被视口切掉行首）。

用法:
  python verify_render.py --blocks blocks.json --translations translations.json \
      --translated out.pdf [--head-len 18] [--json]
退出码: 全部完整=0，有缺失=1。
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


NS = re.compile(r"\s+")


def nospace(s: str) -> str:
    return NS.sub("", s or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("verify_render", description="逐块渲染完整性验证")
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--translations", required=True)
    ap.add_argument("--translated", required=True)
    ap.add_argument("--head-len", type=int, default=18)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    data = json.loads(open(args.blocks, encoding="utf-8").read())
    tr = json.loads(open(args.translations, encoding="utf-8").read())
    doc = fitz.open(args.translated)

    bad = []
    checked = 0
    for p in data.get("pages", []):
        pno = p["page"] - 1
        if pno < 0 or pno >= doc.page_count:
            continue
        page = doc[pno]
        dx = page.rect.width / 2
        for blk in p.get("blocks", []):
            t = tr.get(blk["id"], {}) or {}
            zh = (t.get("zh") or "").strip()
            if not zh:
                continue
            checked += 1
            x0, y0, x1, y1 = blk["bbox"]
            # 取词范围各方向稍放宽，避免相邻行干扰
            rect = fitz.Rect(x0 + dx - 6, y0 - 2, x1 + dx + 6, y1 + 2)
            got = nospace(page.get_text("text", clip=rect))
            # 译文含 X_sub 记号时，渲染为真下标后下划线/尖号不出现 → 两种形态都接受
            variants = [nospace(zh),
                        nospace(zh.replace("_", "").replace("^", ""))]
            ok = False
            for z in variants:
                if z[: args.head_len] in got and z[-args.head_len:] in got:
                    ok = True
                    break
            if not ok:
                bad.append({"page": p["page"], "id": blk["id"], "part": "HEAD/TAIL",
                            "frag": zh[:40]})

    if args.json:
        print(json.dumps({"checked": checked, "incomplete": bad},
                         ensure_ascii=False, indent=1))
    else:
        print(f"checked {checked} blocks, incomplete {len(bad)}")
        for r in bad[:20]:
            print(f"  p{r['page']} {r['id']} [{r['part']}] {r['frag']}")
        if not bad:
            print("✅ 所有译文块的首尾都完整出现在成品 PDF 中（无渲染丢字）")
            print("   若仍看到『行首缺字』，是查看窗口/截图把整页宽段落的行左半切掉了，")
            print("   缩小缩放或向左滚动即可看到完整行。")
    doc.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
