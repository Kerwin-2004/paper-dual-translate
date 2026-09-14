#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / kb.py

文献知识库 + 术语对照知识库。**每次翻译都往里沉淀，下次翻译自动受益。**

两个库（默认放在 ~/.paper-dual-translate/kb，可用 PDT_KB_DIR / --kb 改）：
  glossary.csv     术语对照库：source,target,domain,confidence,hits,papers,...
  literature.jsonl 文献库：题录 + 中译标题/摘要/关键词 + 该文贡献的术语
  INDEX.md         人类可读的索引

子命令
------
  init        初始化知识库
  add-paper   把一篇翻译成果登记进文献库（题录 + 中译标题/摘要/关键词）
  harvest     从一次翻译里挖掘术语对，沉淀进术语库（自精进的核心）
  lookup      查术语
  export      导出术语（prompt / glossary-csv / markdown 三种用途）
  stats       看两个库的规模

harvest 的三条挖掘通道（都要求"英文块与其中文译文块"对齐，块 id 天然对齐）：
  1. 缩写桥接：英文块里的 "Full Name (ABC)" + 中文块里的 "中文名（ABC）"
     → 按 ABC 配对，得到高精度术语对
  2. 关键词对齐：原文 Keywords 行与译文"关键词"行按位置一一配对
  3. 已有术语确认：术语库里的词出现在英文块且其译文出现在对应中文块 → 计入 hits
  （泛化"行内注释"通道已弃用：实测会把 "仅 DQN 智能体（RL only）" 这类
    配置标签误当成术语，噪声太大）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as P  # noqa: E402

CSV_FIELDS = ["source", "target", "domain", "confidence", "hits", "papers",
              "target_alt", "source_kind", "updated"]

CJK = re.compile(r"[\u4e00-\u9fff]")
# 英文 "全称 (ABC)"
RE_EN_ACRO = re.compile(r"([A-Za-z][A-Za-z0-9\-]*(?:[ \-][A-Za-z0-9\-]+){0,5})\s*\(([A-Z][A-Z0-9\-]{1,9})\)")
# 中文 "中文名（ABC）"；中文名允许夹字母数字（如 深度Q网络、ISO认证）
RE_ZH_GLOSS = re.compile(r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9\-]{1,21})（([A-Z][A-Z0-9\-]{1,9})）")
RE_KEYWORDS_EN = re.compile(r"^\s*(keywords|index terms|key words)\s*[:：]?\s*(.+)$", re.I)
RE_KEYWORDS_ZH = re.compile(r"^\s*(关键词|关键字)\s*[:：]?\s*(.+)$")

STOP_PAIRS = {
    ("et al", "等人"), ("fig", "图"), ("table", "表"), ("eq", "式"),
}

# 英文全称里不该出现在开头的词（抓到整句的元凶）
_EN_LEAD_STOP = {
    "we", "the", "a", "an", "this", "that", "these", "those", "our", "their", "its",
    "it", "in", "by", "for", "to", "as", "of", "and", "or", "is", "are", "was", "were",
    "however", "moreover", "furthermore", "additionally", "finally", "thus", "therefore",
    "besides", "specifically", "notably", "here", "there", "using", "based", "with",
    "called", "named", "termed", "namely", "such", "including", "include", "includes",
    "proposed", "propose", "develop", "developed", "introduce", "introduced", "present",
    "presented", "use", "used", "adopt", "adopted", "see", "e.g", "i.e",
}
# 中文译文里不该出现在开头的噪声
_ZH_LEAD_NOISE = [
    "本工作得到", "本工作", "本文", "本研究", "该项目", "该", "其中", "即为", "即",
    "例如", "比如", "包括", "采用", "利用", "基于", "得到", "需要", "可以", "能够",
    "以及", "和", "与", "及", "在", "由", "是", "称为", "命名为",
]


def _clean_en_phrase(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip(" ,;.:")) if w]
    while words and words[0].lower().strip(".,;:") in _EN_LEAD_STOP:
        words.pop(0)
    while words and words[-1].lower().strip(".,;:") in _EN_LEAD_STOP:
        words.pop()
    if not words or len(words) > 6:
        return ""
    # 单词都太短（如 "of the"）不成术语
    if all(len(w) <= 2 for w in words):
        return ""
    phrase = " ".join(words)
    return phrase if 3 <= len(phrase) <= 52 else ""


def _clean_zh_term(cn: str) -> str:
    cn = cn.strip()
    # 在标点处截断，只保留最后一节
    cn = re.split(r"[。，；：！？、（）()\[\]【】]", cn)[-1].strip()
    changed = True
    while changed:
        changed = False
        for noise in sorted(_ZH_LEAD_NOISE, key=len, reverse=True):
            if cn.startswith(noise) and len(cn) > len(noise) + 1:
                cn = cn[len(noise):]
                changed = True
                break
    return cn if 2 <= len(cn) <= 24 else ""


# ----------------------------------------------------------------- 词条读写

def read_glossary(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("source") or "").strip():
                continue
            out.append({k: (row.get(k) or "").strip() for k in CSV_FIELDS})
    return out


def write_glossary(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (-float(r.get("confidence") or 0), r["source"].lower()))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, doublequote=True)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def upsert_term(rows: list[dict], source: str, target: str, *, domain: str = "",
                kind: str = "harvest", confidence: float = 0.7, paper: str = ""):
    """插入或更新一条术语。已有高置信度的译法不会被低置信度的覆盖。"""
    source, target = source.strip(), target.strip()
    if not source or not target or len(source) < 2 or len(target) < 2:
        return None
    if (source.lower(), target) in STOP_PAIRS:
        return None
    # 纯数字/纯符号不要
    if not re.search(r"[A-Za-z]", source) or not CJK.search(target):
        return None

    key = _norm(source)
    for r in rows:
        if _norm(r["source"]) != key:
            continue
        old_c = float(r.get("confidence") or 0)
        if paper and paper not in (r.get("papers") or ""):
            r["hits"] = str(int(r.get("hits") or 0) + 1)
            r["papers"] = ";".join([p for p in (r.get("papers") or "").split(";") if p] + [paper])
        if _norm(r["target"]) == _norm(target):
            if confidence > old_c:
                r["confidence"] = f"{confidence:.2f}"
                r["source_kind"] = kind
            r["updated"] = datetime.now().strftime("%Y-%m-%d")
            return r
        # 译法冲突：保留原译，把新译记进 target_alt
        alts = [a for a in (r.get("target_alt") or "").split("|") if a]
        if target not in alts and _norm(target) != _norm(r["target"]):
            alts.append(target)
            r["target_alt"] = "|".join(alts[:4])
        if confidence > old_c + 0.1:
            # 新证据明显更强时允许改判，但把旧译降级为 alt
            if r["target"] not in alts:
                alts.insert(0, r["target"])
            r["target"] = target
            r["target_alt"] = "|".join(alts[:4])
            r["confidence"] = f"{confidence:.2f}"
            r["source_kind"] = kind
        r["updated"] = datetime.now().strftime("%Y-%m-%d")
        return r

    row = {"source": source, "target": target, "domain": domain,
           "confidence": f"{confidence:.2f}",
           "hits": "1" if paper else "0",
           "papers": paper or "", "target_alt": "", "source_kind": kind,
           "updated": datetime.now().strftime("%Y-%m-%d")}
    rows.append(row)
    return row


# ----------------------------------------------------------------- 挖掘通道

def _plausible(name: str, cn: str, acronym: str, known_sources: set[str]) -> bool:
    """三道精度闸门，挡住"抓到整句"这类噪声。

    1) 该缩写本身已经在术语库里（如 DQN / MDP），说明缩写条目更有用，
       不再用它的英文全称去生成短语条目
    2) 英文全称首词是分词/动名词（designed/guided/using…）→ 说明前面是句子
       的一部分，不是术语
    3) 中英长度比校验：中文长度要落在词数推算出的合理区间内
    """
    if _norm(acronym) in known_sources:
        return False
    words = name.split()
    if not words:
        return False
    w0 = words[0].lower()
    if len(w0) > 4 and (w0.endswith("ed") or w0.endswith("ing")):
        return False
    lo = max(3, len(words) * 2)
    hi = min(24, max(lo, len(words) * 4))
    return lo <= len(cn) <= hi


def harvest_pairs(en: str, zh: str, known_sources: set[str] | None = None
                  ) -> list[tuple[str, str, str, float]]:
    """从一对对齐的段落里挖 (source, target, kind, confidence)。

    只保留**高精度**通道：缩写桥接（英文全称 (ABC) ↔ 中文名（ABC））。
    刻意不做"中文（English Original）"这种泛化行内注释 —— 实测它会
    把 "仅 DQN 智能体（RL only）" 这类配置标签误当成术语，噪声太大。
    术语召回主要靠"已有术语确认"和"关键词对齐"两条通道。
    """
    out: list[tuple[str, str, str, float]] = []
    if not (en and zh):
        return out
    known = known_sources or set()

    en_map: dict[str, str] = {}
    for name, acro in RE_EN_ACRO.findall(en):
        clean = _clean_en_phrase(name)
        if clean:
            en_map.setdefault(acro.upper(), clean)

    zh_map: dict[str, str] = {}
    for cn, acro in RE_ZH_GLOSS.findall(zh):
        clean = _clean_zh_term(cn)
        if clean:
            zh_map.setdefault(acro.upper(), clean)

    for acro, name in en_map.items():
        cn = zh_map.get(acro)
        if cn and _norm(name) != _norm(cn) and _plausible(name, cn, acro, known):
            out.append((name, cn, "acronym", 0.78))
    return out


def keyword_pairs(en_text: str, zh_text: str) -> list[tuple[str, str, str, float]]:
    """原文 Keywords 行 ↔ 译文"关键词"行，按位置配对。"""
    me = RE_KEYWORDS_EN.search(en_text or "")
    mz = RE_KEYWORDS_ZH.search(zh_text or "")
    if not (me and mz):
        return []
    splitter = re.compile(r"[;；,，、]|\s{2,}")
    ens = [p.strip(" .;,") for p in splitter.split(me.group(2)) if p.strip()]
    zhs = [p.strip(" ;，、。") for p in splitter.split(mz.group(2)) if p.strip()]
    out = []
    if len(ens) == len(zhs):
        for a, b in zip(ens, zhs):
            if re.search(r"[A-Za-z]", a) and CJK.search(b) and len(a) > 2:
                out.append((a, b, "keyword", 0.95))
    return out


# ----------------------------------------------------------------- 子命令

def cmd_init(a) -> int:
    d = P.kb_dir(a.kb)
    gp = d / "glossary.csv"
    lp = d / "literature.jsonl"
    if not gp.is_file():
        write_glossary(gp, [])
    if not lp.is_file():
        lp.write_text("", encoding="utf-8")
    # 把技能自带的领域种子词表并入（低权重，作为起点）
    seed = P.SKILL_ROOT / "references" / "glossary_seed_autonomous_driving.csv"
    n_seed = 0
    if seed.is_file():
        rows = read_glossary(gp)
        with seed.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                s, t = (r.get("source") or "").strip(), (r.get("target") or "").strip()
                if s and t and upsert_term(rows, s, t, domain="autonomous-driving",
                                           kind="seed", confidence=0.75):
                    n_seed += 1
        write_glossary(gp, rows)
    print(f"知识库: {d}")
    print(f"  术语库   : {gp}  ({len(read_glossary(gp))} 条)")
    print(f"  文献库   : {lp}")
    if n_seed:
        print(f"  已并入技能自带种子术语 {n_seed} 条")
    return 0


def _load_run(a):
    blocks = json.loads(Path(a.blocks).read_text(encoding="utf-8"))
    trans = json.loads(Path(a.translations).read_text(encoding="utf-8"))
    tables = json.loads(Path(a.tables).read_text(encoding="utf-8")) if a.tables else None
    tdict = json.loads(Path(a.table_dict).read_text(encoding="utf-8")) if a.table_dict else None
    return blocks, trans, tables, tdict


def _paper_id(src_pdf: Path, meta: dict) -> str:
    doi = (meta.get("subject") or "").lower()
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", doi)
    if m:
        return re.sub(r"[^0-9a-z]+", "-", m.group(1).lower()).strip("-")
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", src_pdf.stem)[:60].strip("-")


def _guess_title(blocks: list[dict], meta: dict) -> tuple[str, str | None]:
    """找标题块：优先用 PDF 元数据的 title，否则取第 1 页字号最大的块。"""
    t = (meta.get("title") or "").strip()
    p1 = [b for b in blocks if b["id"].startswith("p1b")]
    if not t and p1:
        cand = max(p1, key=lambda b: (b["size"], b["chars"]))
        return cand["text"], cand["id"]
    for b in p1:
        if t and _norm(t)[:40] and _norm(t)[:40] in _norm(b["text"]):
            return b["text"], b["id"]
    return t, None


def _guess_abstract(blocks: list[dict]) -> tuple[str, str | None]:
    p1 = [b for b in blocks if b["id"].startswith("p1b")]
    if not p1:
        return "", None
    cand = max(p1, key=lambda b: b["chars"])
    return (cand["text"] if cand["chars"] > 200 else ""), (cand["id"] if cand["chars"] > 200 else None)


def _guess_keywords(blocks: list[dict], trans: dict) -> tuple[str, str, str, str]:
    """返回 (en_keywords, zh_keywords, en_text, zh_text)。"""
    for b in blocks:
        if RE_KEYWORDS_EN.search(b["text"]):
            zh = (trans.get(b["id"]) or {}).get("zh", "")
            return b["text"], zh, b["text"], zh
    return "", "", "", ""


def cmd_add_paper(a) -> int:
    from pathlib import Path as _P
    import fitz
    kb = P.kb_dir(a.kb)
    lp = kb / "literature.jsonl"
    if not lp.is_file():
        cmd_init(a)

    src = _P(a.source_pdf).resolve()
    doc = fitz.open(src)
    meta = {k: (v or "") for k, v in (doc.metadata or {}).items()}
    doc.close()

    blocks, trans, tables, tdict = _load_run(a)
    flat = [b for p in blocks["pages"] for b in p["blocks"]]
    domain = a.domain or ""

    title_en, tid = _guess_title(flat, meta)
    title_zh = (trans.get(tid) or {}).get("zh", "") if tid else ""
    abs_en, aid = _guess_abstract(flat)
    abs_zh = (trans.get(aid) or {}).get("zh", "") if aid else ""
    kw_en, kw_zh, kw_block_en, kw_block_zh = _guess_keywords(flat, trans)

    kws_en = [x.strip(" .;,") for x in re.split(r"[;；,，]|\s{2,}", RE_KEYWORDS_EN.search(kw_block_en).group(2))] \
        if RE_KEYWORDS_EN.search(kw_block_en) else []
    kws_zh = [x.strip(" ;，、。") for x in re.split(r"[;；,，、]|\s{2,}", RE_KEYWORDS_ZH.search(kw_block_zh).group(2))] \
        if RE_KEYWORDS_ZH.search(kw_block_zh) else []

    n_blocks = sum(1 for v in trans.values() if (v or {}).get("zh"))
    n_cells = 0
    if tables and tdict:
        n_cells = sum(1 for p in tables.get("pages", []) for t in p.get("tables", [])
                      for c in t.get("cells", []) if tdict.get(c["text"]))

    rec = {
        "id": _paper_id(src, meta),
        "title_en": title_en,
        "title_zh": title_zh,
        "authors": meta.get("author", ""),
        "journal": meta.get("subject", "")[:160],
        "year": (re.search(r"(20\d{2})", meta.get("creationDate", "") or "") or [None, ""])[1]
        if re.search(r"(20\d{2})", meta.get("creationDate", "") or "") else "",
        "doi": (re.search(r"(10\.\d{4,9}/[^\s;]+)", meta.get("subject", "") or "") or [None, ""])[1]
        if re.search(r"(10\.\d{4,9}/[^\s;]+)", meta.get("subject", "") or "") else "",
        "keywords_en": kws_en,
        "keywords_zh": kws_zh,
        "abstract_en": abs_en[:1500],
        "abstract_zh": abs_zh[:1200],
        "domain": domain,
        "source_pdf": str(src),
        "translated_pdf": str(_P(a.translated_pdf).resolve()) if a.translated_pdf else "",
        "pages": blocks.get("page_count", 0),
        "translated_at": datetime.now().strftime("%Y-%m-%d"),
        "n_blocks": n_blocks,
        "n_table_cells": n_cells,
    }

    recs = []
    if lp.is_file():
        for line in lp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    recs = [r for r in recs if r.get("id") != rec["id"]] + [rec]
    lp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    _write_index(kb, recs)

    print(f"已登记文献: {rec['id']}")
    print(f"  标题(中): {rec['title_zh'][:60] or '(未识别)'}")
    print(f"  关键词  : {len(kws_en)}/{len(kws_zh)} 对")
    print(f"  翻译量  : {n_blocks} 块 + {n_cells} 表格格")
    print(f"  文献库  : {len(recs)} 篇")
    return 0


def _write_index(kb: Path, recs: list[dict]) -> None:
    lines = ["# 文献知识库索引", ""]
    lines.append(f"共 {len(recs)} 篇（更新于 {datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    lines.append("")
    lines.append("| 日期 | 中译标题 | 关键词 | 页数 | 译块 | 表格格 |")
    lines.append("|---|---|---|---|---|---|")
    for r in sorted(recs, key=lambda x: x.get("translated_at", ""), reverse=True):
        t = (r.get("title_zh") or r.get("title_en") or r["id"]).replace("|", "\\|")[:60]
        kw = "、".join((r.get("keywords_zh") or [])[:5]).replace("|", "\\|")
        lines.append(f"| {r.get('translated_at','')} | {t} | {kw} | "
                     f"{r.get('pages','')} | {r.get('n_blocks','')} | {r.get('n_table_cells','')} |")
    lines.append("")
    (kb / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")


def cmd_harvest(a) -> int:
    kb = P.kb_dir(a.kb)
    gp = kb / "glossary.csv"
    if not gp.is_file():
        cmd_init(a)
    rows = read_glossary(gp)

    blocks, trans, tables, tdict = _load_run(a)
    paper = a.paper_id or Path(a.source_pdf or a.blocks).stem[:50]
    domain = a.domain or ""

    found: list[tuple[str, str, str, float]] = []
    known_sources = {_norm(r["source"]) for r in rows if r.get("source")}
    # 顺便把"只以缩写形式出现"的词也视作已存在（如 DQN / MDP）
    for p in blocks["pages"]:
        for b in p["blocks"]:
            t = trans.get(b["id"]) or {}
            zh = t.get("zh")
            if not zh:
                continue
            en = b["text"]
            found += harvest_pairs(en, zh, known_sources)
            found += keyword_pairs(en, zh)

    # 已有术语确认：英文块有该词 且 对应中文块有该译法
    confirmed = 0
    pref = {}
    for p in blocks["pages"]:
        for b in p["blocks"]:
            zh = (trans.get(b["id"]) or {}).get("zh")
            if zh:
                pref[_norm(b["text"])] = zh
    for r in rows:
        s, tg = r["source"], r["target"]
        if not s or not tg:
            continue
        for norm_en, zh in pref.items():
            if _norm(s) in norm_en and tg in zh:
                r["hits"] = str(int(r.get("hits") or 0) + 1)
                if paper not in (r.get("papers") or ""):
                    r["papers"] = ";".join([x for x in (r.get("papers") or "").split(";") if x] + [paper])
                base = float(r.get("confidence") or 0)
                r["confidence"] = f"{min(0.95, base + 0.03):.2f}"
                r["updated"] = datetime.now().strftime("%Y-%m-%d")
                confirmed += 1
                break

    added = 0
    for s, t, kind, conf in found:
        before = len(rows)
        upsert_term(rows, s, t, domain=domain, kind=kind, confidence=conf, paper=paper)
        if len(rows) > before:
            added += 1

    write_glossary(gp, rows)
    print(f"术语挖掘完成（论文 {paper}）")
    print(f"  高精度术语对（缩写桥接 + 关键词对齐）: {len(found)}")
    print(f"    新增 {added} 条，更新 {len(found) - added} 条")
    print(f"  已有术语确认: {confirmed} 次命中")
    print(f"  术语库现有 {len(rows)} 条 -> {gp}")
    if found and a.verbose:
        print("\n  新挖掘样例:")
        for s, t, k, c in found[:40]:
            print(f"    [{k} {c:.2f}] {s}  ->  {t}")
    return 0


def cmd_lookup(a) -> int:
    rows = read_glossary(P.kb_dir(a.kb) / "glossary.csv")
    q = a.query.lower()
    hits = [r for r in rows if q in r["source"].lower() or q in r["target"].lower()]
    hits.sort(key=lambda r: -float(r.get("confidence") or 0))
    print(f"命中 {len(hits)} 条 / 库内 {len(rows)} 条")
    for r in hits[: a.limit]:
        alt = f"  (备选: {r['target_alt']})" if r.get("target_alt") else ""
        print(f"  {r['source']}  ->  {r['target']}{alt}")
        print(f"      conf={r['confidence']} hits={r['hits']} kind={r['source_kind']} "
              f"papers={(r['papers'] or '-')[:60]}")
    return 0


def cmd_export(a) -> int:
    rows = read_glossary(P.kb_dir(a.kb) / "glossary.csv")
    mn = float(a.min_confidence)
    rows = [r for r in rows if float(r.get("confidence") or 0) >= mn]
    out = Path(a.out) if a.out else None
    fmt = {"engine-csv": "glossary-csv"}.get(a.format, a.format)   # 兼容旧名
    if fmt == "glossary-csv":
        # 术语表 CSV（source,target,tgt_lng）：给 auto_translate.py 当 --glossary 输入，
        # 也可直接喂给任何支持 CSV 术语表的工具。默认文件名避开知识库本体 glossary.csv
        if not out:
            out = P.kb_dir(a.kb) / "glossary.export.csv"
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, doublequote=True)
            w.writerow(["source", "target", "tgt_lng"])
            for r in rows:
                w.writerow([r["source"], r["target"], ""])
    elif a.format == "markdown":
        if not out:
            out = P.kb_dir(a.kb) / "GLOSSARY.md"
        lines = ["# 术语对照表", "",
                 f"共 {len(rows)} 条（置信度 ≥ {mn}）", "",
                 "| 英文 | 中文 | 置信度 | 命中 | 类型 | 备选 |", "|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['source']} | {r['target']} | {r['confidence']} | "
                         f"{r['hits']} | {r['source_kind']} | {r.get('target_alt','')} |")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:  # prompt：给"模式 A"塞进翻译提示词用的紧凑清单
        if not out:
            out = P.kb_dir(a.kb) / "glossary.prompt.txt"
        by_dom = {}
        for r in rows:
            by_dom.setdefault(r.get("domain") or "general", []).append(r)
        buf = ["以下是本项目的术语对照表，翻译时必须使用这些译法：", ""]
        for dom, rs in by_dom.items():
            buf.append(f"## {dom}")
            for r in rs:
                buf.append(f"- {r['source']} = {r['target']}")
            buf.append("")
        out.write_text("\n".join(buf), encoding="utf-8")
    print(f"导出 {len(rows)} 条 -> {out}")
    return 0


def cmd_stats(a) -> int:
    kb = P.kb_dir(a.kb)
    rows = read_glossary(kb / "glossary.csv")
    recs = []
    lp = kb / "literature.jsonl"
    if lp.is_file():
        for line in lp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    print(f"知识库目录: {kb}")
    print(f"术语库: {len(rows)} 条")
    by_kind = {}
    for r in rows:
        by_kind[r.get("source_kind") or "?"] = by_kind.get(r.get("source_kind") or "?", 0) + 1
    for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"   {k:12} {v:4} 条")
    multi = [r for r in rows if int(r.get("hits") or 0) >= 2]
    print(f"   多篇确认(hits≥2): {len(multi)} 条")
    alts = [r for r in rows if r.get("target_alt")]
    if alts:
        print(f"   ⚠️ 存在译法冲突: {len(alts)} 条（需人工裁决，见 target_alt 列）")
    print(f"文献库: {len(recs)} 篇")
    tot_b = sum(r.get("n_blocks", 0) for r in recs)
    tot_c = sum(r.get("n_table_cells", 0) for r in recs)
    print(f"   累计翻译 {tot_b} 块 + {tot_c} 表格格")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("kb", description="文献知识库 / 术语库")
    ap.add_argument("--kb", default=None, help="知识库目录（默认 PDT_KB_DIR 或 ~/.paper-dual-translate/kb）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化知识库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-paper", help="登记一篇翻译成果")
    p.add_argument("--source-pdf", required=True)
    p.add_argument("--blocks", required=True)
    p.add_argument("--translations", required=True)
    p.add_argument("--tables", default=None)
    p.add_argument("--table-dict", default=None)
    p.add_argument("--translated-pdf", default=None)
    p.add_argument("--domain", default=None, help="学科/领域标签，如 autonomous-driving")
    p.set_defaults(func=cmd_add_paper)

    p = sub.add_parser("harvest", help="从一次翻译挖掘术语")
    p.add_argument("--blocks", required=True)
    p.add_argument("--translations", required=True)
    p.add_argument("--tables", default=None)
    p.add_argument("--table-dict", default=None)
    p.add_argument("--source-pdf", default=None)
    p.add_argument("--paper-id", default=None)
    p.add_argument("--domain", default=None)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("lookup", help="查术语")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("export", help="导出术语")
    p.add_argument("--format", default="prompt",
                   choices=["prompt", "glossary-csv", "engine-csv", "markdown"],
                   help="prompt=给 agent 的术语清单；glossary-csv=术语表 CSV"
                        "（engine-csv 为旧名，等价）；markdown=人类可读表格")
    p.add_argument("--out", default=None)
    p.add_argument("--min-confidence", default="0.6", dest="min_confidence")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="看库规模")
    p.set_defaults(func=cmd_stats)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
