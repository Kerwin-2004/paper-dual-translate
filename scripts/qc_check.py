#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / qc_check.py

对「左英右中」双栏译文 PDF 做质检，输出 PASS/WARN/FAIL 报告。

用法:
  python qc_check.py --source src.pdf --translated out.pdf [--glossary g.csv] [--json]
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_SPACE = re.compile(r"[\u4e00-\u9fff][ \u00a0][\u4e00-\u9fff]")
ASCII_WORDY = re.compile(r"^[\x20-\x7e]{15,}$")

RESULTS: list[dict] = []


def add(name, level, detail, value=None):
    RESULTS.append({"check": name, "level": level, "detail": detail, "value": value})


def cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    return len(CJK.findall(s)) / max(1, len(s))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def nospace(s: str) -> str:
    """去掉所有空白。

    本技能插入的中文会按框宽自动换行，PyMuPDF 取文时会在行末插入 \\n，
    于是 "安全过滤器" 可能被读成 "安全过\\n滤器"。做术语/字符串匹配前
    必须先去掉全部空白，否则命中率会被严重低估。
    """
    return re.sub(r"\s+", "", s or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("qc_check", description="双栏对照译文质检")
    ap.add_argument("--source", required=True, help="英文原文 PDF")
    ap.add_argument("--translated", required=True, help="双栏译文 PDF")
    ap.add_argument("--glossary", default=None, help="术语表 CSV，可选")
    ap.add_argument("--blocks", default=None, help="blocks.json（精确判定未译块时需要）")
    ap.add_argument("--translations", default=None, help="translations.json（配合 --blocks）")
    ap.add_argument("--tables", default=None, help="tables.json（检查表格翻译覆盖）")
    ap.add_argument("--table-dict", default=None, help="表格译文词条 JSON（配合 --tables）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--left-lang-tolerance", type=float, default=0.02,
                    help="左半 CJK 占比容差，默认 0.02")
    ap.add_argument("--min-right-cjk", type=float, default=0.20,
                    help="右半 CJK 占比下限，默认 0.20")
    ap.add_argument("--ignore-pages", default=None,
                    help="豁免质检的页（1-based，如 \"5,16-18,38-39\"），"
                         "用于整页图版/参考文献页等设计上保留英文的页，避免低中文占比误报")
    args = ap.parse_args(argv)

    try:
        import fitz
    except ImportError:
        print("❌ 需要 PyMuPDF（import fitz）。请运行: pip install pymupdf")
        return 2

    src_p, ref_p = Path(args.source).resolve(), Path(args.translated).resolve()
    for p in (src_p, ref_p):
        if not p.is_file():
            print(f"❌ 文件不存在: {p}")
            return 2

    src = fitz.open(src_p)
    ref = fitz.open(ref_p)

    # 豁免页（1-based）→ 0-based 集合；用于图版页/参考文献页等本就不译的页
    ignore0: set[int] = set()
    if args.ignore_pages:
        for part in args.ignore_pages.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                ignore0.update(range(int(a) - 1, int(b)))
            elif part:
                ignore0.add(int(part) - 1)

    # 1. 页数
    if src.page_count == ref.page_count:
        add("页数一致", "PASS", f"{src.page_count} 页 == {ref.page_count} 页", src.page_count)
    else:
        add("页数一致", "FAIL", f"源 {src.page_count} 页 != 译文 {ref.page_count} 页",
            {"src": src.page_count, "ref": ref.page_count})

    # 2. 页面尺寸（宽度应为 2 倍）
    sw = src[0].rect.width
    rw = ref[0].rect.width
    ratio = rw / sw if sw else 0
    if 1.85 <= ratio <= 2.15:
        add("页面尺寸(左右拼接)", "PASS", f"源宽 {sw:.1f} -> 译文宽 {rw:.1f}（比值 {ratio:.2f}）", round(ratio, 3))
    else:
        add("页面尺寸(左右拼接)", "FAIL",
            f"源宽 {sw:.1f} -> 译文宽 {rw:.1f}（比值 {ratio:.2f}，期望≈2；"
            f"若是上下交替页模式请忽略本项）", round(ratio, 3))

    # 3/4. 左右半语言（逐页）
    bad_left, bad_right, right_empty = [], [], []
    n = min(src.page_count, ref.page_count)

    # 若给了 translations.json，就只检查"应当有译文"的页
    # （参考文献页、纯图表页本来就不翻译，不该算失败）
    expect_pages = None
    if args.blocks and args.translations:
        bd_ = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
        tr_ = json.loads(Path(args.translations).read_text(encoding="utf-8"))
        expect_pages = {
            p["page"] for p in bd_.get("pages", [])
            if any((tr_.get(b["id"]) or {}).get("zh") for b in p.get("blocks", []))
        }

    for i in range(n):
        if i in ignore0:
            continue
        page = ref[i]
        mid = page.rect.width / 2
        left = page.get_text("text", clip=fitz.Rect(0, 0, mid, page.rect.height)) or ""
        right = page.get_text("text", clip=fitz.Rect(mid, 0, page.rect.width, page.rect.height)) or ""
        lr = cjk_ratio(left)
        if lr > args.left_lang_tolerance:
            bad_left.append((i + 1, round(lr, 3)))
        if expect_pages is not None and (i + 1) not in expect_pages:
            continue  # 该页本就无需翻译
        if right.strip():
            rr = cjk_ratio(right)
            if rr < args.min_right_cjk:
                bad_right.append((i + 1, round(rr, 3)))
        else:
            right_empty.append(i + 1)

    if bad_left:
        add("左半为原文(未被改)", "WARN",
            f"{len(bad_left)} 页左半出现中文（可能被误译/串栏）: {bad_left[:12]}")
    else:
        add("左半为原文(未被改)", "PASS", "所有页左半 CJK 占比≈0")

    if bad_right or right_empty:
        add("右半已翻译", "FAIL" if len(bad_right) + len(right_empty) > n * 0.3 else "WARN",
            f"疑似未翻译页 {len(bad_right)} 页 {bad_right[:10]}；空白页 {len(right_empty)} 页 {right_empty[:10]}"
            + ("（只统计应译页）" if expect_pages is not None else "")
            + (f"（已豁免 {len(ignore0)} 页）" if ignore0 else "")
            + "。提示：整页只有图+题注的图版页、图续页、参考文献页右半 CJK 天然偏低，"
              "属设计内保留英文——用 --ignore-pages 把这些页列进去即可（先不加参数跑一遍看列表）")
    else:
        add("右半已翻译", "PASS",
            "所有应译页右半 CJK 占比达标" + (f"（应译页 {len(expect_pages)} 页）" if expect_pages else ""))

    # 5. 目录
    try:
        st, rt = src.get_toc(), ref.get_toc()
        if len(st) == len(rt):
            add("目录条目一致", "PASS", f"{len(st)} 条", len(st))
        else:
            add("目录条目一致", "WARN", f"源 {len(st)} 条 vs 译文 {len(rt)} 条")
    except Exception as e:
        add("目录条目一致", "WARN", f"读取失败: {e}")

    # 6. 图片
    si = sum(len(src[i].get_images(full=True)) for i in range(n))
    ri = sum(len(ref[i].get_images(full=True)) for i in range(n))
    if ri >= si:
        add("图片保留", "PASS", f"源 {si} -> 译文 {ri}", {"src": si, "ref": ri})
    else:
        add("图片保留", "WARN", f"源 {si} -> 译文 {ri}（译文更少，可能有图丢失）", {"src": si, "ref": ri})

    # 7. 未翻译段落
    # 精确模式：给出 blocks.json + translations.json 时，逐块检查"应当已翻译"的区域
    # 右半是否仍是英文。不给出时退化为按行启发式。
    untranslated = []
    if args.blocks and args.translations:
        bd = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
        tr = json.loads(Path(args.translations).read_text(encoding="utf-8"))
        checked = 0
        for p in bd.get("pages", []):
            i = p["page"] - 1
            if i < 0 or i >= n or i in ignore0:
                continue
            page = ref[i]
            mid = page.rect.width / 2
            for b in p.get("blocks", []):
                t = tr.get(b["id"]) or {}
                if not (t.get("zh") or "").strip():
                    continue  # 故意保留原文的块，不参与判定
                checked += 1
                x0, y0, x1, y1 = b["bbox"]
                rect = fitz.Rect(x0 + mid, y0, x1 + mid, y1)
                txt = (page.get_text("text", clip=rect) or "").strip()
                if not txt:
                    untranslated.append((p["page"], b["id"], "(空)"))
                    continue
                letters = len(re.findall(r"[A-Za-z]", txt))
                compact = re.sub(r"\s+", "", txt)
                # 译文以 ASCII 数学记号为主的块（V_desired×Δt 之类）字母占比天然高，
                # 只要 clip 里出现了中文就是已翻译；纯英文且无任何中文才算残留
                has_cjk = bool(CJK.search(txt))
                if letters >= 15 and letters / max(1, len(compact)) > 0.6 and not has_cjk:
                    untranslated.append((p["page"], b["id"], compact[:80]))
        add("无未翻译段落", "PASS" if not untranslated else ("WARN" if len(untranslated) < 10 else "FAIL"),
            f"逐块检查 {checked} 个应译块，异常 {len(untranslated)} 个",
            untranslated[:20])
    else:
        for i in range(n):
            if i in ignore0:
                continue
            page = ref[i]
            mid = page.rect.width / 2
            left = page.get_text("text", clip=fitz.Rect(0, 0, mid, page.rect.height)) or ""
            right = page.get_text("text", clip=fitz.Rect(mid, 0, page.rect.width, page.rect.height)) or ""
            lset = {nospace(line) for line in left.splitlines() if len(line.strip()) > 15}
            for line in right.splitlines():
                s = line.strip()
                if len(s) > 15 and ASCII_WORDY.match(s) and nospace(s) in lset:
                    untranslated.append((i + 1, s[:80]))
        if not untranslated:
            add("无未翻译段落", "PASS", "未发现右半照抄原文的段落")
        else:
            add("无未翻译段落", "WARN" if len(untranslated) < 10 else "FAIL",
                f"发现 {len(untranslated)} 处右半照抄原文（含故意保留的元数据/参考文献/表格，"
                f"给出 --blocks/--translations 可精确判定）",
                untranslated[:20])

    # 8. 术语表落地率
    if args.glossary:
        gp = Path(args.glossary)
        if gp.is_file():
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import glossary as G
            rows = G.read_glossary(gp)
            hay = []
            for i in range(n):
                page = ref[i]
                mid = page.rect.width / 2
                hay.append(page.get_text("text", clip=fitz.Rect(mid, 0, page.rect.width, page.rect.height)) or "")
            hay = "\n".join(hay)
            hay_ns = nospace(hay)

            # 只看"源词确实出现在已译块里"的术语，否则会被日期、期刊名等
            # 本就不该翻译的条目稀释（分母虚高、覆盖率虚低）
            rows_use = rows
            if args.blocks and args.translations:
                bd2 = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
                tr2 = json.loads(Path(args.translations).read_text(encoding="utf-8"))
                src_buf = " ".join(
                    b["text"] for p in bd2.get("pages", []) for b in p.get("blocks", [])
                    if (tr2.get(b["id"]) or {}).get("zh")
                )
                src_buf = re.sub(r"\s+", " ", src_buf).lower()
                rows_use = [(s, t, lang) for s, t, lang in rows if s.lower() in src_buf]

            hit = [(s, t) for s, t, _ in rows_use if t and nospace(t) in hay_ns]
            miss = [(s, t) for s, t, _ in rows_use if not (t and nospace(t) in hay_ns)]
            rate = len(hit) / max(1, len(rows_use))
            lvl = "PASS" if rate >= 0.8 else ("WARN" if rate >= 0.6 else "FAIL")
            add("术语表落地率", lvl,
                f"{len(hit)}/{len(rows_use)} = {rate:.1%}"
                + ("（已限定为原文中真实出现的术语，并忽略换行影响）"
                   if len(rows_use) != len(rows) else "（已忽略换行影响）"),
                round(rate, 4))
            if miss and lvl != "PASS":
                add("术语未命中样例", "INFO", "; ".join(f"{s}->{t}" for s, t in miss[:15]))

    # 9. PDF 文字是否可搜索（ToUnicode 完整性）
    # 若嵌入字体把汉字映射到 CJK 兼容区码位（U+FAxx/U+F9xx），PDF 就无法被
    # 正常搜索或复制。Noto Serif/Sans SC、SourceHanSerif 经 PyMuPDF 嵌入时会踩这个坑。
    compat = 0
    compat_sample = None
    for i in range(n):
        page = ref[i]
        mid = page.rect.width / 2
        right = page.get_text("text", clip=fitz.Rect(mid, 0, page.rect.width, page.rect.height)) or ""
        hits_ = [c for c in right if 0xF900 <= ord(c) <= 0xFAFF]
        if hits_:
            compat += len(hits_)
            if compat_sample is None:
                compat_sample = "".join(hits_[:12])
    if compat == 0:
        add("PDF文字可搜索", "PASS", "未发现 CJK 兼容区码位（可正常搜索/复制）", 0)
    else:
        add("PDF文字可搜索", "FAIL" if compat > 20 else "WARN",
            f"发现 {compat} 个 CJK 兼容区码位，搜索/复制会异常。"
            f"样例: {compat_sample}。换用 STSONG/simsun/Deng 等静态字体可修复。", compat)

    # 10. 中文字间多余空格（BabelDOC 排版瑕疵）
    spaces = 0
    sample = None
    for i in range(n):
        page = ref[i]
        mid = page.rect.width / 2
        right = page.get_text("text", clip=fitz.Rect(mid, 0, page.rect.width, page.rect.height)) or ""
        ms = CJK_SPACE.findall(right)
        spaces += len(ms)
        if ms and sample is None:
            m = CJK_SPACE.search(right)
            if m:
                sample = right[max(0, m.start() - 20): m.end() + 20].replace("\n", "\\n")
    lvl = "PASS" if spaces < 50 else ("WARN" if spaces < 400 else "FAIL")
    add("中文字间多余空格", lvl, f"共 {spaces} 处" + (f"，样例: {sample}" if sample else ""), spaces)

    # 11. 表格翻译覆盖率（给出 --tables/--table-dict 时）
    if args.tables and args.table_dict:
        td = json.loads(Path(args.tables).read_text(encoding="utf-8"))
        tdict = json.loads(Path(args.table_dict).read_text(encoding="utf-8"))
        total = done = missing = 0
        bad = []
        for p in td.get("pages", []):
            i = p["page"] - 1
            if i < 0 or i >= n:
                continue
            page = ref[i]
            mid = page.rect.width / 2
            for t in p.get("tables", []):
                for c in t.get("cells", []):
                    if not tdict.get(c["text"]):
                        continue
                    total += 1
                    bx = c["bbox"]
                    rect = fitz.Rect(bx[0] + mid, bx[1], bx[2] + mid, bx[3])
                    # 格子的 bbox 很紧，扩一点边距再取，避免取不到
                    rect = fitz.Rect(rect.x0 - 3, rect.y0 - 2, rect.x1 + 3, rect.y1 + 2)
                    txt = nospace(page.get_text("text", clip=rect) or "")
                    if CJK.findall(txt):
                        done += 1
                    else:
                        missing += 1
                        bad.append((p["page"], c["text"][:24]))
        if total:
            rate = done / total
            lvl = "PASS" if rate >= 0.95 else ("WARN" if rate >= 0.8 else "FAIL")
            add("表格翻译覆盖", lvl, f"{done}/{total} = {rate:.1%} 个单元格已译", round(rate, 4))
            if bad:
                add("表格未译样例", "INFO", "; ".join(f"p{pg}:{t}" for pg, t in bad[:12]))
        else:
            add("表格翻译覆盖", "INFO", "tables.json 中没有匹配到需翻译的单元格")

    src.close()
    ref.close()

    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
        return 1 if any(r["level"] == "FAIL" for r in RESULTS) else 0

    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}
    print("=" * 72)
    print("paper-dual-translate · 质检报告")
    print("=" * 72)
    print(f"原文: {src_p.name}")
    print(f"译文: {ref_p.name}")
    print("-" * 72)
    for r in RESULTS:
        print(f"{icon.get(r['level'], '  ')} {r['check']:<20} {r['detail']}")

    n_pass = sum(1 for r in RESULTS if r["level"] == "PASS")
    n_warn = sum(1 for r in RESULTS if r["level"] == "WARN")
    n_fail = sum(1 for r in RESULTS if r["level"] == "FAIL")
    print("-" * 72)
    print(f"合计: PASS {n_pass} / WARN {n_warn} / FAIL {n_fail}")
    print("=" * 72)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
