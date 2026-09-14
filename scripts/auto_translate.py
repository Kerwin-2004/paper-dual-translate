#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / auto_translate.py

技能内的「脚本批量翻译」：**不依赖任何外部引擎**（不需要 pdf2zh_next / BabelDOC），
脚本自己调用 OpenAI 兼容的 chat/completions 接口，把 blocks.json 译成
build_dual.py 需要的 translations.json；表格单元格同理译成 table_trans.json。

与「Agent 直译」的关系：两者产出的文件**完全一致**，之后的 build_dual / qc_check
流程也完全相同。区别只在谁在翻译——这里由脚本批量调 API（无人值守、适合成批跑），
Agent 直译则是一次几页、能看图精修。

用法
----
  # 1) 先看会怎么分批、哪些块会被跳过（不调 API、不花钱）
  python auto_translate.py --blocks work/blocks.json --output work/translations.json --dry-run

  # 2) 正式跑（API 配置优先级：命令行 > 环境变量 PDT_API_* > config.json 的 api_*）
  python auto_translate.py --blocks work/blocks.json --output work/translations.json \
      --tables work/tables.json --table-dict-out work/table_trans.json \
      --glossary work/glossary.csv --workers 4

  # 也可以直接用环境变量：
  #   PDT_API_BASE=https://api.deepseek.com/v1
  #   PDT_API_KEY=sk-xxxx  PDT_API_MODEL=deepseek-chat

  # 若已有其它工具配好的 key，可一次性导入（可选）：
  #   --p2z-config path/to/config.toml

特性
----
  - 分批请求（按字符预算打包），并发数可控；失败自动重试，重试仍失败则**批次二分**
    继续兜底，最大化救回内容
  - --resume（默认开）：断点续跑，已有译文的块不重译
  - 跳过策略：页眉页脚 / 刊名·DOI·版权等元数据 / 作者单位 / 参考文献 /
    纯公式块 / 表格区域（`--no-skip` 关闭全部启发式）
  - 术语表注入（`--glossary source,target` 的 CSV，可由 `kb.py export` 生成）
  - 译文缺失的块**不写占位**，交给 qc_check / verify_render 暴露后人工补齐
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:  # Windows 传统控制台/管道下固定 UTF-8，避免打印中文与 ✅⚠️ 时 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import config as PDT
except Exception:
    PDT = None

SKILL_ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = SKILL_ROOT / "references" / "translate_prompt.txt"

# 兜底提示词：正常情况下用的是 references/translate_prompt.txt（那份可以自行改写）。
# 这里只是最小可用版，保证提示词文件缺失时（例如只拷了 scripts/ 目录）不至于中断。
# 改风格请改 references/translate_prompt.txt，不要改这里——两份内容不需要保持同步。
DEFAULT_PROMPT = (
    "你是严谨的学术论文译者，把英文学术文本译成简体中文。要求：\n"
    "1. 术语准确、全文一致；有术语表时一律优先采用术语表的译法。\n"
    "2. 保持学术书面语，不口语化、不增删原意、不做解释或评论。\n"
    "3. 行内数学记号写成 ASCII 下标记法（如 a_rl^t、S_{t+1}、Δ_i），保留原符号，"
    "不要在译文里插入换行。\n"
    "4. 输入是 JSON 数组，元素形如 {\"id\": \"p3b7\", \"en\": \"英文原文\"}；"
    "整段翻译该英文。\n"
    "5. 只输出一个 JSON 对象：键是 id，值是中文译文；"
    "不要输出解释、前后缀说明或 Markdown 代码块标记。"
)

# ---------------------------------------------------------------- 跳过策略

_PAGE_NO = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdm]{1,6})\s*$", re.I)
_META = re.compile(
    r"(doi\s*:|https?://|www\.|©|\(c\)\s*20|copyright|all rights reserved|"
    r"received\s|accepted\s|available online|contents lists available|"
    r"journal homepage|elsevier|springer|wiley|taylor\s*&\s*francis|"
    r"preprint|under review|licen[cs]e)",
    re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AFFIL = re.compile(
    r"(university|institute|college|laboratory|academy|department|"
    r"school of|faculty)", re.I)
_REF_HEAD = re.compile(r"^\s*(references|bibliography|reference list|参考文献)\s*[:.]?\s*$", re.I)
_CELL_SKIP = re.compile(r"^[\s\d.,%±+\-–—/()\[\]:;×xX*•·✓✗✔✘°<>≤≥≈=]+$")

# 页眉页脚：块**起点**落在页面上/下这个比例带内（实测本机样张：页眉 0.043h、
# 正文首块 0.066h，取 0.062 作阈值，兼顾两行页眉）
_HEADER_BAND = 0.062
_FOOTER_BAND = 0.94

_CORRESP = re.compile(r"(corresponding author|corresp\.)", re.I)
_JOURNAL = re.compile(
    r"(journal of|transactions on|proceedings of|research part|part\s+[a-f]\b|"
    r"elsevier|springer|sciencedirect|wiley|ieee\b)", re.I)
# 题注与结构标签：即使是短块也要译（白名单，避免被"碎片"规则误杀）
_CAPTION = re.compile(
    r"^\s*(fig\.?|figure|table|tab\.?|algorithm|eq\.?|equation|图|表|式|算法)\s*\d", re.I)
_LABEL = re.compile(r"^[A-Z](?:\s+[A-Z]){2,}\.?$")           # "A R T I C L E I N F O"
_LABELISH = re.compile(
    r"^\s*(?:a\s?b\s?s\s?t\s?r\s?a\s?c\s?t|a\s?r\s?t\s?i\s?c\s?l\s?e|"
    r"keywords?|index terms)\s*[:：]?\s*$", re.I)
_FRAG_MIN = 50          # 短于这个长度、又不是题注/标签/公式块的，判为表格图版碎片

_SENT_END = (".", "!", "?", "。", "！", "？")

# 「真标题」判定：extract_blocks 的 heading 标记会把显示公式碎片误判成标题
# （数学字体带粗体特征），所以这里再按文本形态过滤一次
_HEAD_NUM = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+[A-Za-z]|appendix\b|[IVX]{1,4}\.\s+[A-Za-z])", re.I)
_HEAD_CHARS = re.compile(r"^[A-Za-z][A-Za-z\s\-–&,'/:]*$")


def _heading_like(t: str) -> bool:
    """像章节标题的短块（编号标题，或纯字母词的短语标题）。"""
    if _HEAD_NUM.match(t):
        return True
    if not _HEAD_CHARS.match(t):
        return False                      # 含数字/符号的多半是公式碎片
    words = [w for w in t.split() if len(w) >= 3]
    return len(words) >= 2 or (len(words) == 1 and len(t) >= 12)


def build_table_regions(tables_data):
    """表格区域列表：[(page, (x0,y0,x1,y1))]，用于跳过落在表格里的文本块。"""
    out = []
    if not tables_data:
        return out
    for p in tables_data.get("pages", []):
        for t in p.get("tables", []):
            r = t.get("region")
            if r:
                out.append((p.get("page"), tuple(r)))
    return out


def _sym_ratio(t):
    """非空白字符里「非常见字符」的占比（✓ ✘ ≤ × − 这类表格符号）。"""
    ns = [c for c in t if not c.isspace()]
    if not ns:
        return 0.0
    plain = ".,;:'\"()%-–—+/"
    return sum(1 for c in ns if not (c.isalnum() or c in plain)) / len(ns)


_ABS_ANCHOR = re.compile(
    r"^\s*(?:a\s?b\s?s\s?t\s?r\s?a\s?c\s?t|a\s?r\s?t\s?i\s?c\s?l\s?e|"
    r"keywords?|index terms|1\.?\s+introduction)", re.I)


def compute_front_matter(pages):
    """首页正文锚点（Abstract / Keywords / ARTICLE INFO / Introduction）之前的
    作者姓名、单位、题录行 → 跳过（首个块=标题 除外）。"""
    out = set()
    for p in pages:
        if p.get("page") != 1:
            continue
        blks = p.get("blocks", [])
        anchor_y = None
        for b in blks:
            if _ABS_ANCHOR.match((b.get("text") or "").strip()):
                anchor_y = b["bbox"][1]
                break
        if anchor_y is None:
            continue
        for i, b in enumerate(blks):
            t = (b.get("text") or "").strip()
            if i == 0 or b["bbox"][1] >= anchor_y:
                continue
            if _CAPTION.match(t) or _LABELISH.match(t):
                continue
            out.add(b["id"])
    return out


def landscape_pages(pages):
    """横排页（width > height）：整页大表/大图，默认整页保留英文。"""
    out = set()
    for p in pages:
        w, h = float(p.get("width") or 0), float(p.get("height") or 0)
        if w and h and w > h:
            out.add(p.get("page"))
    return out


def in_table(bbox, regions, page, ratio=0.5):
    if not regions:
        return False
    ax0, ay0, ax1, ay1 = bbox
    a_area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    for pg, (rx0, ry0, rx1, ry1) in regions:
        if pg != page:
            continue
        ix = max(0.0, min(ax1, rx1) - max(ax0, rx0))
        iy = max(0.0, min(ay1, ry1) - max(ay0, ry0))
        if ix * iy / a_area > ratio:
            return True
    return False


def should_skip(b, page_no, page_rect, ctx):
    """返回 (skip?, 原因)。启发式规则见文件头说明；--force-ids 指定的 id 不跳。"""
    t = (b.get("text") or "").strip()
    if b.get("id") in ctx.get("force", ()):
        return False, ""
    if not t:
        return True, "空块"
    if page_no in ctx.get("landscape", ()):
        return True, "横排整页图表"
    if b.get("id") in ctx.get("front", ()):
        return True, "首页作者/题录"
    if b.get("nested_in"):
        return True, "簇内碎片"
    if b.get("math_only"):
        return True, "纯公式块"
    if ctx.get("refs_auto", True):
        if ctx["in_refs"]:
            return True, "参考文献"
        if _REF_HEAD.match(t):
            ctx["in_refs"] = True
            return True, "参考文献标题"
    if page_no in ctx["skip_pages"]:
        return True, "指定跳过页"
    if ctx["in_table"](b):
        return True, "表格区域"
    # 题注 / 结构标签 / 真标题：短也译，不受后面的"碎片"规则影响
    keep_short = bool(_CAPTION.match(t) or _LABEL.match(t) or _LABELISH.match(t)
                      or _heading_like(t))
    # 页眉/页脚：块起点落在页面上下比例带内且文本短
    h = (page_rect[1] if page_rect else 0) or 0
    x0, y0, x1, y1 = b.get("bbox", (0, 0, 0, 0))
    if h and len(t) < 90 and (y0 < _HEADER_BAND * h or y0 > _FOOTER_BAND * h):
        return True, "页眉页脚"
    if _PAGE_NO.match(t):
        return True, "页码"
    if _EMAIL.search(t):
        return True, "邮箱"
    if len(t) < 130 and _META.search(t):
        return True, "刊名/DOI/版权"
    if len(t) < 80 and _JOURNAL.search(t):
        return True, "刊名"
    if len(t) < 120 and _CORRESP.search(t):
        return True, "通讯作者"
    if len(t) < 300 and _AFFIL.search(t) and not t.endswith(_SENT_END):
        return True, "作者单位"
    # 行间公式碎片：带数学字形又是短块的，保留英文（译了反而破坏原公式）；
    # 实测含行内公式的**整段** ≥100 字符，短的都是公式/表格碎片
    if b.get("has_math") and len(t) < 100 and not _heading_like(t):
        return True, "公式碎片"
    # 表格/图版碎片：短、非题注、非公式、句未终的残块
    if len(t) < _FRAG_MIN and not keep_short and not t.endswith(_SENT_END):
        return True, "表格图版碎片"
    # 符号密度高的块（✓✘ 矩阵、单位行等）
    if len(t) >= 3 and _sym_ratio(t) > 0.45:
        return True, "符号表格块"
    return False, ""


# ---------------------------------------------------------------- 提示词与 API

def load_prompt():
    if PROMPT_FILE.is_file():
        p = PROMPT_FILE.read_text(encoding="utf-8").strip()
        if p:
            return p
    return DEFAULT_PROMPT


def build_messages(items, glossary):
    sys_prompt = load_prompt()
    if glossary:
        g = "\n".join(f"- {s} → {t}" for s, t in glossary)
        sys_prompt += ("\n\n术语表（必须遵守，优先级高于你习惯的译法；"
                       "译文里要用这些中文词）：\n" + g)
    sys_prompt += ("\n\n【输出契约】只输出一个 JSON 对象：键=输入 id，值=对应中文译文。"
                   "不要输出任何解释、寒暄或 Markdown 代码块标记。")
    user = json.dumps([{"id": i, "en": t} for i, t in items], ensure_ascii=False)
    return [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": user}]


class ApiAuthError(RuntimeError):
    """401/403：key 无效或无权限 —— 重试与二分都没有意义，应立即中止。"""


def call_api(cfg, messages, timeout, temperature, retries=0):
    """一次 HTTP 调用；返回 (content, usage)。"""
    url = cfg["base"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "paper-dual-translate/1.4",
    }
    if cfg.get("key"):
        headers["Authorization"] = f"Bearer {cfg['key']}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            raise ApiAuthError(f"HTTP {e.code} {body}") from None
        raise
    content = ""
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except Exception:
        pass
    return content, (data.get("usage") or {})


def extract_json(text):
    """从模型输出里稳健地抠出 {id: 译文}。"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M).strip()
    for cand in (t, ):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return {str(k): v for k, v in obj.items()}
        except Exception:
            pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(t[i:j + 1])
            if isinstance(obj, dict):
                return {str(k): v for k, v in obj.items()}
        except Exception:
            pass
    out = {}
    for m in re.finditer(r'"([^"]{1,80})"\s*:\s*"((?:[^"\\]|\\.)*)"', t):
        try:
            out[m.group(1)] = json.loads('"' + m.group(2) + '"')
        except Exception:
            out[m.group(1)] = m.group(2)
    return out


def _merge_usage(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)):
            out[k] = out.get(k, 0) + v
    return out


def do_batch(cfg, items, glossary, args, depth=0):
    """译一批；返回 ({id: zh}, usage)。失败重试，仍失败则二分降级。
    鉴权失败（401/403）不重试、不二分，直接向上抛。"""
    if not items:
        return {}, {}
    if getattr(args, "abort", None) is not None and args.abort.is_set():
        return {}, {}
    messages = build_messages(items, glossary)
    err = ""
    for attempt in range(args.retry + 1):
        try:
            content, usage = call_api(cfg, messages, args.timeout, args.temperature)
            got = extract_json(content)
            out = {i: got[i].strip() for i, _ in items
                   if isinstance(got.get(i), str) and got[i].strip()}
            if out:
                return out, usage
            err = "返回为空或不是合法 JSON"
        except ApiAuthError:
            raise
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            err = f"HTTP {e.code} {body}"
        except Exception as e:
            err = str(e)
        if attempt < args.retry:
            time.sleep(min(8.0, 1.5 ** attempt))
    if len(items) > 1 and depth < args.split_depth:
        mid = len(items) // 2
        a, ua = do_batch(cfg, items[:mid], glossary, args, depth + 1)
        b, ub = do_batch(cfg, items[mid:], glossary, args, depth + 1)
        return {**a, **b}, _merge_usage(ua, ub)
    print(f"  ⚠️ 批次失败（{len(items)} 块）: {err}")
    return {}, {}


def make_batches(items, budget):
    """按字符预算把 [(id, text)] 打包成若干批。"""
    batches, cur, n = [], [], 0
    for it in items:
        ln = len(it[1]) + 40
        if cur and n + ln > budget:
            batches.append(cur)
            cur, n = [], 0
        cur.append(it)
        n += ln
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------- 术语表 / 配置

def load_glossary(path):
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        print(f"⚠️ 术语表不存在，忽略: {p}")
        return []
    pairs, seen = [], set()
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            s = (row.get("source") or row.get("en") or "").strip()
            t = (row.get("target") or row.get("zh") or "").strip()
            if s and t and s.lower() not in seen:
                seen.add(s.lower())
                pairs.append((s, t))
    print(f"术语表: {len(pairs)} 条  ({p})")
    return pairs


# 可选的一次性便利：从既有 zotero-pdf2zh 的 config.toml 里导入一组可用 key。
# 注意它的结构是「开关 <服务名> 在顶层 + 明细在 <服务名>_detail 子表」，
# key/model/base_url 都在子表里。
_P2Z_IMPORT = [
    # (服务名, 明细表, key 字段, model 字段, base_url 字段)
    ("deepseek", "deepseek_detail", "deepseek_api_key", "deepseek_model", None),
    ("siliconflow", "siliconflow_detail", "siliconflow_api_key", "siliconflow_model",
     "siliconflow_base_url"),
    ("openaicompatible", "openaicompatible_detail", "openai_compatible_api_key",
     "openai_compatible_model", "openai_compatible_base_url"),
    ("modelscope", "modelscope_detail", "modelscope_api_key", "modelscope_model", None),
    ("zhipu", "zhipu_detail", "zhipu_api_key", "zhipu_model", None),
    ("aliyundashscope", "aliyundashscope_detail", "aliyun_dashscope_api_key",
     "aliyun_dashscope_model", "aliyun_dashscope_base_url"),
]
_P2Z_BASE = {
    "deepseek": "https://api.deepseek.com/v1",
    "modelscope": "https://api-inference.modelscope.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}
_P2Z_PLACEHOLDER = {"", "null", "none", "your_api_key", "请填写", "sk-xxx"}


def import_p2z_keys(path):
    """可选：从既有 pdf2zh 的 config.toml 里导入一组可用的 key（一次性便利）。

    优先取「顶层开关为 true」的服务，其次取任意配了 key 的服务。
    """
    p = Path(path)
    if not p.is_file():
        print(f"⚠️ 找不到 p2z 配置: {p}")
        return None
    raw = p.read_bytes()
    data = None
    for loader in ("tomllib", "toml"):
        try:
            mod = __import__(loader)
            data = (mod.loads if loader == "tomllib" else mod.loads)(
                raw.decode("utf-8-sig", "replace"))
            break
        except Exception:
            continue
    if not isinstance(data, dict):
        print(f"⚠️ p2z 配置解析失败（需要 tomllib 或 toml）: {p}")
        return None
    order = [x for x in _P2Z_IMPORT if data.get(x[0]) is True] \
        + [x for x in _P2Z_IMPORT if data.get(x[0]) is not True]
    for name, table, key_f, model_f, base_f in order:
        sub = data.get(table) or {}
        if not isinstance(sub, dict):
            continue
        key = str(sub.get(key_f) or "").strip()
        if not key or key.lower() in _P2Z_PLACEHOLDER:
            continue
        base = str(sub.get(base_f) or "").strip() if base_f else ""
        base = base or _P2Z_BASE.get(name, "")
        model = str(sub.get(model_f) or "").strip()
        if not base:
            continue
        print(f"已从 p2z 配置导入服务: {name}（{'已启用' if data.get(name) is True else '未启用但有 key'}）"
              f"  model={model or '(默认)'}")
        return {"base": base, "key": key, "model": model or "deepseek-chat"}
    print("⚠️ p2z 配置里没有找到可用的 key（可能是各服务的 key 都为空）")
    return None


def resolve_api(args):
    """命令行 > PDT_API_* 环境变量 > config.json(api_*) > --p2z-config 导入。"""
    if PDT is not None:
        cfg = PDT.api_config(args.api_base, args.api_key, args.api_model)
    else:
        import os
        cfg = {"base": args.api_base or os.environ.get("PDT_API_BASE") or "",
               "key": args.api_key or os.environ.get("PDT_API_KEY") or "",
               "model": args.api_model or os.environ.get("PDT_API_MODEL") or ""}
    if (not cfg.get("key")) and args.p2z_config:
        imp = import_p2z_keys(args.p2z_config)
        if imp:
            cfg.update({k: v for k, v in imp.items() if v})
    return cfg


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    ap = argparse.ArgumentParser("auto_translate", description="技能内 API 批量翻译")
    ap.add_argument("--blocks", required=True, help="extract_blocks.py 产出的 blocks.json")
    ap.add_argument("--output", required=True, help="输出 translations.json")
    ap.add_argument("--tables", default=None, help="extract_tables.py 产出的 tables.json")
    ap.add_argument("--table-dict-out", default=None, help="表格译文词条输出 JSON")
    ap.add_argument("--glossary", default=None, help="术语表 CSV（source,target）")
    ap.add_argument("--pages", default="all", help="只译这些页（1-based），默认全部")
    ap.add_argument("--skip-pages", default=None, help="强制跳过这些页（1-based）")
    ap.add_argument("--no-skip", action="store_true", help="关闭跳过启发式（全部块都译）")
    ap.add_argument("--no-refs-auto", action="store_true",
                    help="不自动跳过 References 之后的块")
    ap.add_argument("--force-ids", default=None,
                    help="强制翻译的块 id（逗号分隔），绕过所有跳过启发式")
    ap.add_argument("--api-base", default=None, help="OpenAI 兼容接口 base url")
    ap.add_argument("--api-key", default=None, help="API Key")
    ap.add_argument("--api-model", default=None, help="模型名")
    ap.add_argument("--p2z-config", default=None,
                    help="可选：从既有 pdf2zh config.toml 导入 key（一次性）")
    ap.add_argument("--workers", type=int, default=4, help="并发批次数，默认 4")
    ap.add_argument("--batch-chars", type=int, default=4000, help="每批字符预算，默认 4000")
    ap.add_argument("--retry", type=int, default=2, help="每批重试次数，默认 2")
    ap.add_argument("--split-depth", type=int, default=3, help="失败后二分降级的最大深度")
    ap.add_argument("--timeout", type=float, default=180.0, help="单次请求超时（秒）")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--resume", dest="resume", action="store_true", default=True,
                    help="断点续跑（默认开）")
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="只译前 N 块（调试用，0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只打印分批与跳过计划，不调 API")
    args = ap.parse_args(argv)
    args.abort = threading.Event()          # 鉴权失败时置位，快速终止其余批次

    data = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
    pages = data.get("pages", [])
    total_pages = data.get("page_count") or (max((p.get("page", 0) for p in pages), default=0))
    want = parse_pages(args.pages, total_pages)
    skip_pages = set(parse_pages(args.skip_pages, total_pages)) if args.skip_pages else set()

    tables_data = None
    if args.tables:
        tables_data = json.loads(Path(args.tables).read_text(encoding="utf-8"))
    regions = build_table_regions(tables_data)

    existing = {}
    out_path = Path(args.output)
    if args.resume and out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            n0 = sum(1 for v in existing.values() if (v.get("zh") or "").strip())
            print(f"续跑: 已有译文 {n0} 条（不会重译）")
        except Exception:
            existing = {}

    ctx = {
        "in_refs": False,
        "refs_auto": not args.no_refs_auto,
        "skip_pages": skip_pages,
        "force": {s.strip() for s in (args.force_ids or "").split(",") if s.strip()},
        "front": set() if args.no_skip else compute_front_matter(pages),
        "landscape": set() if args.no_skip else landscape_pages(pages),
        "in_table": lambda b: in_table(b.get("bbox", (0, 0, 0, 0)), regions, b.get("_page")),
        "counts": {},
    }

    items, skipped = [], {}
    for p in pages:
        pno = p.get("page")
        if want and pno not in want:
            continue
        rect = (0.0, float(p.get("height") or 0.0))
        for b in p.get("blocks", []):
            b["_page"] = pno
            if args.no_skip:
                sk, why = False, ""
            else:
                sk, why = should_skip(b, pno, rect, ctx)
            if sk:
                skipped[b["id"]] = why
                ctx["counts"][why] = ctx["counts"].get(why, 0) + 1
                continue
            if (existing.get(b["id"], {}) or {}).get("zh", "").strip():
                continue
            items.append((b["id"], b["text"]))
    if not ctx["counts"]:
        pass
    if args.limit:
        items = items[:args.limit]

    n_chars = sum(len(t) for _, t in items)
    batches = make_batches(items, args.batch_chars)
    print(f"待译 {len(items)} 块 / {n_chars} 字符 -> {len(batches)} 批"
          f"（每批 ≤{args.batch_chars} 字符，并发 {args.workers}）")
    if skipped:
        dist = "、".join(f"{k}×{v}" for k, v in sorted(ctx["counts"].items(), key=lambda kv: -kv[1]))
        print(f"跳过 {len(skipped)} 块: {dist}")

    if args.dry_run:
        for i, bt in enumerate(batches[:3], 1):
            head = bt[0][1][:60].replace("\n", " ")
            print(f"  批 {i}: {len(bt)} 块，首块 {bt[0][0]}「{head}…」")
        if len(batches) > 3:
            print(f"  …共 {len(batches)} 批")
        msg = build_messages(batches[0], load_glossary(args.glossary)) if batches else []
        if msg:
            print(f"\n[提示词预览] system 前 200 字：\n{msg[0]['content'][:200]}…")
            print(f"[提示词预览] user 前 200 字：\n{msg[1]['content'][:200]}…")
        print("\n(dry-run：未调用任何 API)")
        return 0

    cfg = resolve_api(args)
    is_local = any(h in (cfg.get("base") or "").lower() for h in ("localhost", "127.0.0.1", "0.0.0.0", "192.168."))
    if not cfg.get("key") and not is_local:
        print("❌ 缺少 API Key。三种给法：")
        print("   1) 环境变量  PDT_API_KEY / PDT_API_BASE / PDT_API_MODEL")
        print("   2) 技能目录 config.json 里写 api_key / api_base / api_model")
        print("   3) 命令行 --api-key（或 --p2z-config 从既有配置导入）")
        return 2
    key_disp = f"***{str(cfg['key'])[-4:]}" if cfg.get("key") else "(免Key/本地端点)"
    print(f"API: {cfg['base']}  model={cfg['model']}  key={key_disp}")

    glossary = load_glossary(args.glossary)
    result = dict(existing)
    for i in skipped:
        if i not in result:
            result[i] = {"skip": True}
    usage_total, failed = {}, []
    auth_err = ""
    lock = threading.Lock()
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(do_batch, cfg, bt, glossary, args): bt for bt in batches}
        for fu in as_completed(futs):
            bt = futs[fu]
            try:
                got, usage = fu.result()
            except ApiAuthError as e:
                args.abort.set()             # 其余批次立即空转返回，不再浪费时间
                if not auth_err:
                    auth_err = str(e)
                    print(f"\n❌ API 鉴权失败，已中止后续请求：{auth_err}")
                    print("   请检查 --api-key / PDT_API_KEY / config.json 的 api_key 与 --api-base。")
                continue
            with lock:
                result.update({k: {"zh": v} for k, v in got.items()})
                for k, v in (usage or {}).items():
                    if isinstance(v, (int, float)):
                        usage_total[k] = usage_total.get(k, 0) + v
                done += 1
                miss = [i for i, _ in bt if i not in got]
                failed.extend(miss)
                print(f"  [{done}/{len(batches)}] 批 {len(bt)} 块 -> 得到 {len(got)}"
                      + (f"，缺 {len(miss)}" if miss else ""))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    n_zh = sum(1 for v in result.values() if (v.get("zh") or "").strip())
    dt = time.time() - t0
    print(f"\n输出: {out_path}  共 {len(result)} 条（译文 {n_zh} / skip {len(skipped)}）")
    if usage_total:
        print("token 用量: " + "  ".join(f"{k}={v}" for k, v in usage_total.items()))
    print(f"耗时 {dt:.1f}s")
    if failed and not auth_err:
        print(f"⚠️ {len(failed)} 块未译出（重试+二分后仍失败），已留空交由质检暴露：")
        print("   " + "、".join(failed[:20]) + (" …" if len(failed) > 20 else ""))
        print("   处理建议：减少 --batch-chars、降低 --workers 后重跑（--resume 只补这些块）")
    if auth_err:
        print("\n❌ 因鉴权失败中止：本次只写出了已成功的部分。修好 Key 后重跑（--resume 会接着补）。")
        return 2

    # 表格单元格通道
    if tables_data and args.table_dict_out:
        tdict, tmiss, tusage = translate_cells(cfg, tables_data, glossary, args,
                                               Path(args.table_dict_out))
        print(f"表格词条: {len(tdict)} 条译出" + (f"，{tmiss} 个未译出" if tmiss else ""))
        if tusage:
            print("表格 token 用量: " + "  ".join(f"{k}={v}" for k, v in tusage.items()))

    print("\n下一步: build_dual.py --translations " + str(out_path)
          + (f" --table-dict {args.table_dict_out}" if args.table_dict_out else ""))
    return 1 if failed else 0


def translate_cells(cfg, tables_data, glossary, args, out_path):
    """表格单元格：把唯一文本批量译成 {原文本: 中文} 词条字典。"""
    texts, seen = [], set()
    for p in tables_data.get("pages", []):
        for t in p.get("tables", []):
            for c in t.get("cells", []):
                s = (c.get("text") or "").strip()
                if not s or s in seen or len(s) < 2 or _CELL_SKIP.match(s):
                    continue
                seen.add(s)
                texts.append(s)
    if not texts:
        return {}, 0, {}
    existing = {}
    if args.resume and out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    todo = [t for t in texts if t not in existing]
    items = [(f"c{i}", t) for i, t in enumerate(todo)]
    batches = make_batches(items, args.batch_chars)
    out, usage_total, miss = dict(existing), {}, 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(do_batch, cfg, bt, glossary, args): bt for bt in batches}
        for fu in as_completed(futs):
            bt = futs[fu]
            got, usage = fu.result()
            for i, txt in bt:
                zh = got.get(i)
                if zh:
                    out[txt] = zh
                else:
                    miss += 1
            for k, v in (usage or {}).items():
                if isinstance(v, (int, float)):
                    usage_total[k] = usage_total.get(k, 0) + v
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, miss, usage_total


def parse_pages(spec, total) -> set:
    """'1,3-5' / 'all' -> 1-based 页码集合。"""
    if not spec or str(spec).lower() == "all":
        return set(range(1, int(total or 0) + 1))
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
