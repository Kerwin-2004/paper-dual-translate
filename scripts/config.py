#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / config.py

统一的配置与路径解析。**这是"可迁移"的关键**：
技能里任何脚本都不写死本机路径，而是走这里。

优先级（从高到低）：
  1. 命令行参数（各脚本自己的 --font / --kb / --api-key 等）
  2. 环境变量（下表）
  3. 技能目录下的 config.json
  4. 自动探测

翻译 API（auto_translate.py 用；OpenAI 兼容接口）
------------------------------------------------
PDT_API_BASE       接口 base url（默认 https://api.deepseek.com/v1）
PDT_API_KEY        API Key（必填，否则批量翻译无法工作）
PDT_API_MODEL      模型名（默认 deepseek-chat）

路径类环境变量
--------------
PDT_HOME           技能数据目录（默认 ~/.paper-dual-translate）
PDT_KB_DIR         知识库目录（默认 <PDT_HOME>/kb）
PDT_WORK_DIR       默认工作目录（存放 blocks.json / translations.json）
PDT_PYTHON         首选解释器（需带 PyMuPDF）
PDT_FONT           正文字体文件
PDT_FONT_BOLD      标题字体文件
PDT_MATH_FONT      数学字体文件

config.json（可选，放在技能根目录；**本机私有，不要随技能分发**）
------------------------------------------------------------------
{
  "api_base": "https://api.deepseek.com/v1",
  "api_key": "sk-xxxx",
  "api_model": "deepseek-chat",
  "python": "C:/Python312/python.exe",
  "font": "C:/Windows/Fonts/STSONG.TTF",
  "font_bold": "C:/Windows/Fonts/STZHONGS.TTF",
  "math_font": "C:/Windows/Fonts/cambria.ttc",
  "kb_dir": "D:/research/kb",
  "work_dir": "D:/research/work"
}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = SKILL_ROOT / "config.json"


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 固定为 UTF-8。

    Windows 传统控制台以及「输出重定向/被上层管道捕获」的场景下，默认编码
    是 cp936，脚本里的中文与 ✅⚠️ 会抛 UnicodeEncodeError 直接中断。
    本模块导入时自动调用一次；不依赖 config.py 的脚本也各自内联了同样的保护。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


ensure_utf8_stdio()

# ---- 字体候选（跨平台，按优先级；构建时会逐个做"文字可提取"自检）----

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    r"C:\Windows\Fonts\STSONG.TTF",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/Supplemental/Heiti.ttc",
    r"C:\Windows\Fonts\STZHONGS.TTF",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
]
MATH_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/crosextra/CambriaMath.ttf",
    "/System/Library/Fonts/Supplemental/Cambria Math.ttf",
    r"C:\Windows\Fonts\cambria.ttc",
]

DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_API_MODEL = "deepseek-chat"


def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


_CFG = None


def cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def _first(cands, explicit=None, env=None, configured=None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
    if env:
        v = os.environ.get(env)
        if v:
            p = Path(v).expanduser()
            if p.is_file():
                return p
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return p
    for c in cands:
        c = Path(c)
        if c.is_file():
            return c
    return None


def home() -> Path:
    d = Path(os.environ.get("PDT_HOME") or cfg().get("home") or (Path.home() / ".paper-dual-translate"))
    d = d.expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def kb_dir(explicit=None) -> Path:
    d = Path(explicit or os.environ.get("PDT_KB_DIR") or cfg().get("kb_dir") or (home() / "kb"))
    d = d.expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir(explicit=None) -> Path:
    d = Path(explicit or os.environ.get("PDT_WORK_DIR") or cfg().get("work_dir") or (home() / "work"))
    d = d.expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_python(explicit=None) -> Path:
    """构建/翻译用的解释器（可用 PDT_PYTHON 或 config.json 覆盖）。"""
    v = explicit or os.environ.get("PDT_PYTHON")
    if v and Path(v).is_file():
        return Path(v)
    if cfg().get("python") and Path(cfg()["python"]).is_file():
        return Path(cfg()["python"])

    cur = Path(sys.executable)
    try:
        import fitz  # noqa: F401
        return cur
    except ImportError:
        pass

    # 当前解释器没有 fitz 时，只探测与本技能相邻的虚拟环境——
    # 这里**不写任何机器绝对路径**（技能要能原样复制到别的机器）。
    cands = [
        SKILL_ROOT / ".venv" / "Scripts" / "python.exe",
        SKILL_ROOT / ".venv" / "bin" / "python",
        SKILL_ROOT.parent / ".venv" / "Scripts" / "python.exe",
        SKILL_ROOT.parent / ".venv" / "bin" / "python",
        Path.home() / ".virtualenvs" / "pdt" / "Scripts" / "python.exe",
        Path.home() / ".virtualenvs" / "pdt" / "bin" / "python",
    ]
    for c in cands:
        cp = Path(c)
        if cp.is_file():
            try:
                import subprocess
                r = subprocess.run([str(cp), "-c", "import fitz"],
                                   capture_output=True, timeout=2)
                if r.returncode == 0:
                    return cp
            except Exception:
                continue
    return cur


def find_font(explicit=None, bold=False) -> Path | None:
    c = cfg()
    if bold:
        return (_first(FONT_BOLD_CANDIDATES, explicit, "PDT_FONT_BOLD", c.get("font_bold"))
                or _first(FONT_CANDIDATES, configured=c.get("font")))
    return _first(FONT_CANDIDATES, explicit, "PDT_FONT", c.get("font"))


def find_math_font(explicit=None) -> Path | None:
    return _first(MATH_FONT_CANDIDATES, explicit, "PDT_MATH_FONT", cfg().get("math_font"))


def api_config(explicit_base=None, explicit_key=None,
               explicit_model=None) -> dict:
    """解析翻译 API 配置：命令行 > PDT_API_* 环境变量 > config.json > 默认值。

    返回 {"base", "key", "model", "source"}；key 可能为空（未配置）。
    """
    c = cfg()
    base = explicit_base or os.environ.get("PDT_API_BASE") or c.get("api_base") or DEFAULT_API_BASE
    key = explicit_key or os.environ.get("PDT_API_KEY") or c.get("api_key") or ""
    model = explicit_model or os.environ.get("PDT_API_MODEL") or c.get("api_model") or DEFAULT_API_MODEL
    src = []
    if explicit_base or explicit_key or explicit_model:
        src.append("命令行")
    if os.environ.get("PDT_API_KEY") or os.environ.get("PDT_API_BASE") or os.environ.get("PDT_API_MODEL"):
        src.append("环境变量")
    if c.get("api_key") or c.get("api_base") or c.get("api_model"):
        src.append("config.json")
    if not src:
        src.append("默认值")
    return {"base": str(base).strip(), "key": str(key).strip(),
            "model": str(model).strip(), "source": "+".join(src)}


def ensure_pymupdf():
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        n = find_python()
        print("❌ 需要 PyMuPDF（import fitz）。", file=sys.stderr)
        print(f"   试试用这个解释器运行：{n}", file=sys.stderr)
        print(f"   或安装：{n} -m pip install pymupdf", file=sys.stderr)
        return False


def doctor() -> int:
    c = api_config()
    key = c["key"]
    if not key:
        key_disp = "(未配置)"
    elif len(key) >= 4:
        key_disp = "***" + key[-4:]
    else:
        key_disp = "(过短?)"
    print("=" * 72)
    print("paper-dual-translate · 环境")
    print("=" * 72)
    print(f"技能目录     : {SKILL_ROOT}")
    print(f"config.json  : {CONFIG_FILE if CONFIG_FILE.is_file() else '(未提供，走环境变量/默认值)'}")
    print(f"数据目录     : {home()}")
    print(f"知识库目录   : {kb_dir()}")
    print(f"工作目录     : {work_dir()}")
    print()
    print(f"翻译 API     : {c['base']}  model={c['model']}  key={key_disp}  [{c['source']}]")
    if not key:
        print("               ℹ️ 未配 Key 不影响 Agent 直译（模式 A）；"
              "脚本批量翻译需 PDT_API_KEY 或 config.json 的 api_key")
    print(f"解释器       : {find_python()}")
    print(f"正文字体     : {find_font() or '❌ 未找到'}")
    print(f"标题字体     : {find_font(bold=True) or '❌ 未找到'}")
    print(f"数学字体     : {find_math_font() or '(未找到，公式将走内置回退)'}")
    py_rec = find_python()
    ok = True
    try:
        import fitz
        print(f"PyMuPDF      : {fitz.__doc__.splitlines()[0]}")
    except ImportError:
        # 检查推荐解释器是否安装了 PyMuPDF
        import subprocess
        try:
            r = subprocess.run([str(py_rec), "-c", "import fitz; print(fitz.__doc__.splitlines()[0])"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                print(f"PyMuPDF      : ✅ 就绪（位于推荐解释器: {r.stdout.strip()}）")
            else:
                ok = False
                print(f"PyMuPDF      : ❌ 未安装（运行: {py_rec} -m pip install pymupdf）")
        except Exception:
            ok = False
            print(f"PyMuPDF      : ❌ 未安装（运行: {py_rec} -m pip install pymupdf）")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(doctor())
