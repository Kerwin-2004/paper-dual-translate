#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper-dual-translate / pipeline.py

综合流水线调度脚本：一站式完成从源 PDF 到双栏对照 PDF 的全流程。
统一串联 normalize_pdf、extract_blocks、extract_tables、audit_nested_blocks、
merge_paragraphs、auto_translate、build_dual、verify_render 与 qc_check。

支持三种模式：
  1. auto (模式 B 全自动):
     一条命令自动完成抽取、审计、批量翻译、构建与质检。
  2. prepare (模式 A 阶段一):
     一键完成旋转页归一化、抽取、审计与段落合并，产出待翻译 JSON 供 Agent 翻译。
  3. build (模式 A 阶段二):
     在 Agent 完成译文后，一键合并分页译文、构建双栏 PDF 并运行完整性质检。
  4. check:
     仅对已生成的双栏 PDF 运行 verify_render 与 qc_check。

用法示例:
  # 全自动模式 (直调 LLM API 批量翻译并构建)
  python scripts/pipeline.py --source paper.pdf --mode auto --output out/paper.dual.pdf

  # Agent 直译准备阶段 (生成 blocks.json 与 tables.json)
  python scripts/pipeline.py --source paper.pdf --mode prepare --work-dir work

  # Agent 翻译完成后一键构建与质检
  python scripts/pipeline.py --source paper.pdf --mode build --work-dir work --output out/paper.dual.pdf
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:  # Windows 控制台 UTF-8 输出保护
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent

try:
    import config as PDT
    import provenance as PROV
except Exception:
    sys.path.insert(0, str(SCRIPT_DIR))
    import config as PDT
    import provenance as PROV


def _safe_cmd_for_log(cmd: list[str]) -> str:
    out: list[str] = []
    redact_next = False
    for part in cmd:
        s = str(part)
        if redact_next:
            out.append("***REDACTED***")
            redact_next = False
            continue
        if s == "--api-key":
            out.append(s)
            redact_next = True
        elif s.startswith("--api-key="):
            out.append("--api-key=***REDACTED***")
        else:
            out.append(s)
    return " ".join(out)


def run_step(py_exe: Path, script_name: str, args: list[str], desc: str,
             env: dict[str, str] | None = None) -> int:
    """运行子脚本并展示清晰的步骤进度。"""
    script_path = SCRIPT_DIR / script_name
    cmd = [str(py_exe), str(script_path)] + [str(a) for a in args]
    print(f"\n{'='*72}")
    print(f"▶ {desc}")
    print(f"  命令: {_safe_cmd_for_log(cmd)}")
    print(f"{'='*72}")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        print(f"❌ 步骤失败 (退出码 {res.returncode}): {script_name}", file=sys.stderr)
    return res.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        "pipeline",
        description="paper-dual-translate 综合流水线调度器（一站式全自动/分步执行）"
    )
    ap.add_argument("--source", "-s", required=True, help="英文原文 PDF 文件路径")
    ap.add_argument(
        "--mode", "-m",
        choices=["auto", "prepare", "build", "check"],
        default="auto",
        help="运行模式: auto=全自动批量翻译; prepare=仅预处理抽取(供Agent翻译); "
             "build=译文已就绪时执行构建与质检; check=仅质检"
    )
    ap.add_argument("--work-dir", "-w", default=None,
                    help="中间文件工作目录 (默认: work/<论文文件名不含后缀>)")
    ap.add_argument("--output", "-o", default=None,
                    help="输出双栏对照 PDF 路径 (默认: output/<原名>.no_watermark.zh-CN.LR_dual.pdf)")
    ap.add_argument("--pages", default="all", help="处理页码 (如 all 或 1-3)")
    ap.add_argument("--glossary", "-g", default=None, help="术语表 CSV 路径")
    ap.add_argument("--python", default=None, help="指定运行环境解释器 (默认自动探测含 PyMuPDF 的 Python)")
    
    # 模式 B (auto_translate) 相关参数
    ap.add_argument("--api-key", default=None, help="翻译 API Key (也可通过 PDT_API_KEY / config.json 提供)")
    ap.add_argument("--api-base", default=None, help="翻译 API Base URL")
    ap.add_argument("--api-model", default=None, help="翻译模型名")
    ap.add_argument("--workers", type=int, default=4, help="批量翻译并发线程数 (默认 4)")
    ap.add_argument("--batch-chars", type=int, default=4000, help="批量翻译单批字符预算 (默认 4000)")
    
    # 构建与排版参数
    ap.add_argument("--font", default=None, help="指定 CJK 正文字体文件路径")
    ap.add_argument("--font-scale", type=float, default=1.2, help="中文字号自适应起始倍率 (默认 1.2)")
    ap.add_argument("--no-indent", action="store_true", help="关闭中文首行缩进 (段首空两格)")
    ap.add_argument("--allow-unverified-translations", action="store_true",
                    help="允许 schema v4 构建缺少身份字段的旧手工译文")
    ap.add_argument("--preview", default=None, help="预览图输出路径 (PNG)")
    ap.add_argument("--preview-pages", default="1", help="预览哪些页 (1-based)")
    
    # 质检参数
    ap.add_argument("--ignore-pages", default=None, help="豁免质检的页码 (如 \"5,16-18,38-39\")")
    ap.add_argument("--dry-run", action="store_true", help="仅打印执行计划，不实际执行")

    args = ap.parse_args(argv)

    src_path = Path(args.source).resolve()
    if not src_path.is_file():
        print(f"❌ 原文 PDF 不存在: {src_path}", file=sys.stderr)
        return 2

    # 解释器探测
    py_exe = Path(args.python).resolve() if args.python else PDT.find_python()
    if not py_exe.is_file():
        print(f"❌ 找不到有效的 Python 解释器: {py_exe}", file=sys.stderr)
        return 2

    stem = src_path.stem
    # 工作目录默认设定
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    else:
        work_dir = (Path.cwd() / "work" / stem).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # 成品输出路径默认设定
    if args.output:
        out_pdf = Path(args.output).resolve()
    else:
        out_dir = Path.cwd() / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_pdf = out_dir / f"{stem}.no_watermark.zh-CN.LR_dual.pdf"

    norm_pdf = work_dir / f"{stem}.norm.pdf"
    blocks_json = work_dir / "blocks.json"
    tables_json = work_dir / "tables.json"
    translations_json = work_dir / "translations.json"
    table_trans_json = work_dir / "table_trans.json"
    manifest_json = work_dir / "manifest.json"
    incomplete_marker = work_dir / ".prepare-incomplete"

    print("=" * 72)
    print("paper-dual-translate · 流水线调度")
    print("=" * 72)
    print(f"原文路径   : {src_path}")
    print(f"运行模式   : {args.mode}")
    print(f"工作目录   : {work_dir}")
    print(f"输出路径   : {out_pdf}")
    print(f"解释器     : {py_exe}")
    print("=" * 72)

    if args.dry_run:
        print("ℹ️ Dry-run 模式：仅规划流程，不执行具体命令。")
        return 0

    manifest = None
    if args.mode in ("build", "check"):
        if incomplete_marker.is_file():
            print("❌ 此 work-dir 的最近一次预处理未完成；拒绝复用可能混杂的新旧中间产物。",
                  file=sys.stderr)
            print("   请重新运行 --mode prepare，成功后该标记会自动清除。", file=sys.stderr)
            return 2
        ok, manifest, reason = PROV.validate_manifest(manifest_json, src_path)
        if not ok:
            print(f"❌ 工作目录与当前源 PDF 不匹配：{reason}", file=sys.stderr)
            print("   请重新运行 --mode prepare，或改用与该 work-dir 对应的源 PDF。",
                  file=sys.stderr)
            return 2
        if manifest is None:
            print("⚠️ work-dir 没有 manifest.json（旧版工作目录），无法验证来源绑定。")
        elif args.mode == "build" and not PROV.page_selection_covers(
                manifest.get("pages", "all"), args.pages):
            print(f"❌ 当前 work-dir 只预处理了页码 {manifest.get('pages')}，"
                  f"不能构建请求的页码 {args.pages}。", file=sys.stderr)
            print("   请用覆盖目标页码的 --pages 重新运行 --mode prepare。", file=sys.stderr)
            return 2

    # ---------------- 模式 1: check 仅质检 ----------------
    if args.mode == "check":
        if not out_pdf.is_file():
            print(f"❌ 目标译文 PDF 不存在: {out_pdf}", file=sys.stderr)
            return 2
        
        # verify_render
        verify_rc = 0
        if blocks_json.is_file() and translations_json.is_file():
            verify_rc = run_step(
                py_exe, "verify_render.py",
                ["--blocks", str(blocks_json), "--translations", str(translations_json),
                 "--translated", str(out_pdf)],
                "逐块渲染完整性验证 (verify_render)"
            )
        else:
            print("ℹ️ 未检测到 blocks.json 或 translations.json，跳过逐块渲染完整性验证。")
        
        # qc_check
        qc_args = ["--source", str(src_path), "--translated", str(out_pdf)]
        if blocks_json.is_file():
            qc_args += ["--blocks", str(blocks_json)]
        if translations_json.is_file():
            qc_args += ["--translations", str(translations_json)]
        if tables_json.is_file():
            qc_args += ["--tables", str(tables_json)]
        if table_trans_json.is_file():
            qc_args += ["--table-dict", str(table_trans_json)]
        if args.glossary:
            qc_args += ["--glossary", str(args.glossary)]
        if args.ignore_pages:
            qc_args += ["--ignore-pages", str(args.ignore_pages)]
            
        qc_rc = run_step(py_exe, "qc_check.py", qc_args, "译文多维综合质检 (qc_check)")
        return 1 if (verify_rc != 0 or qc_rc != 0) else 0

    # ---------------- 预处理步骤 (auto 与 prepare 均需要) ----------------
    if args.mode in ("auto", "prepare"):
        incomplete_marker.write_text(
            "prepare started; this marker is removed only after all extraction steps succeed\n",
            encoding="utf-8",
        )
        # Step 1: 旋转页检查与归一化
        chk_res = subprocess.run([str(py_exe), str(SCRIPT_DIR / "normalize_pdf.py"),
                                  "--check", "--input", str(src_path)],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        effective_source = src_path
        if "检测到旋转页" in chk_res.stdout or "检测到旋转页" in chk_res.stderr:
            print("⚠️ 检测到源 PDF 包含旋转页面，执行自动烘焙归一化...")
            rc = run_step(py_exe, "normalize_pdf.py",
                          ["--input", str(src_path), "--output", str(norm_pdf)],
                          "旋转页归一化 (normalize_pdf)")
            if rc != 0:
                return rc
            effective_source = norm_pdf
        else:
            print("✅ 源 PDF 无旋转页，无需归一化。")

        # Step 2: 先抽取表格。v4 文本提取必须知道表格区域，避免正文与表格串流。
        rc = run_step(py_exe, "extract_tables.py",
                      ["--input", str(effective_source), "--output", str(tables_json),
                       "--pages", args.pages],
                      "表格单元格抽取 (extract_tables)")
        if rc != 0:
            return rc

        # Step 3: 列感知自然段抽取
        ext_args = ["--input", str(effective_source), "--output", str(blocks_json),
                    "--tables", str(tables_json), "--pages", args.pages]
        rc = run_step(py_exe, "extract_blocks.py", ext_args,
                      "列感知自然段抽取 (extract_blocks v4)")
        if rc != 0:
            return rc

        # Step 4: bbox 嵌套簇审计与修补
        rc = run_step(py_exe, "audit_nested_blocks.py",
                      ["--blocks", str(blocks_json), "--apply"],
                      "bbox 嵌套簇审计与修补 (audit_nested_blocks)")
        if rc != 0:
            return rc

        # Step 5: 段落合并与断句
        # 已有译文时**必须**传 --translations：merge_paragraphs 会跳过"含已译成员"的合并组。
        # 否则重跑 prepare 会把已译块并进 head 块，成员块的 zh 条目变成孤儿
        # （build 时那段中文静默丢失）——这是保护译文的关键一道闸门。
        merge_p_args = ["--blocks", str(blocks_json), "--tables", str(tables_json), "--apply"]
        if translations_json.is_file():
            merge_p_args += ["--translations", str(translations_json)]
            print("ℹ️ 检测到已有译文 translations.json：合并将跳过含已译成员的段落（保护译文）")
        rc = run_step(py_exe, "merge_paragraphs.py", merge_p_args,
                      "段落断句合并 (merge_paragraphs)")
        if rc != 0:
            return rc

        # 所有预处理步骤成功后才更新来源清单并解除失败保护。
        manifest = PROV.write_manifest(manifest_json, src_path, effective_source, args.pages)
        incomplete_marker.unlink(missing_ok=True)
        print(f"来源清单   : {manifest_json}  sha256={manifest['source']['sha256'][:12]}…")

        if args.mode == "prepare":
            print("\n" + "=" * 72)
            print("🎉 预处理完成！可供 Agent 直译的数据已就绪：")
            print(f"  - 正文文本块: {blocks_json}")
            print(f"  - 表格单元格: {tables_json}")
            print("\n下一步（模式 A）:")
            print("  1. Agent 阅读 blocks.json 并产出 translations.json")
            print("  2. Agent 阅读 tables.json 并产出 table_trans.json (词条字典)")
            print(f"  3. 运行构建命令: python scripts/pipeline.py --source \"{src_path}\" --mode build --work-dir \"{work_dir}\"")
            print("=" * 72)
            return 0

    # ---------------- 模式 auto 专属: 批量自动翻译 ----------------
    if args.mode == "auto":
        trans_args = ["--blocks", str(blocks_json), "--output", str(translations_json),
                      "--tables", str(tables_json), "--table-dict-out", str(table_trans_json),
                      "--workers", str(args.workers), "--batch-chars", str(args.batch_chars)]
        child_env = None
        if args.api_key:
            child_env = os.environ.copy()
            child_env["PDT_API_KEY"] = str(args.api_key)
        if args.api_base:
            trans_args += ["--api-base", str(args.api_base)]
        if args.api_model:
            trans_args += ["--api-model", str(args.api_model)]
        if args.glossary:
            trans_args += ["--glossary", str(args.glossary)]
        if args.pages and args.pages.lower() != "all":
            trans_args += ["--pages", str(args.pages)]

        rc = run_step(py_exe, "auto_translate.py", trans_args,
                      "LLM 批量翻译 (auto_translate)", env=child_env)
        if rc != 0:
            return rc

    # ---------------- 构建步骤 (auto 与 build 均需要) ----------------
    if args.mode in ("auto", "build"):
        # 检查是否需要合并分页翻译 (trans_p*.json)
        page_trans_files = list(work_dir.glob("trans_p*.json"))
        if page_trans_files and not translations_json.is_file():
            print(f"ℹ️ 检测到 {len(page_trans_files)} 个分页译文文件，正在自动合并...")
            rc = run_step(py_exe, "merge_translations.py",
                          ["--dir", str(work_dir), "--output", str(translations_json)],
                          "分页译文合并 (merge_translations)")
            if rc != 0:
                return rc

        if not translations_json.is_file():
            print(f"❌ 缺少译文文件: {translations_json}", file=sys.stderr)
            return 2

        # 只使用 manifest 明确绑定的 effective source，防止旧 .norm.pdf 被误用。
        build_source = src_path
        if manifest is not None:
            eff_hash = (manifest.get("effective_source") or {}).get("sha256")
            src_hash = (manifest.get("source") or {}).get("sha256")
            if manifest.get("normalized"):
                if not norm_pdf.is_file():
                    print(f"❌ manifest 要求使用归一化 PDF，但文件不存在: {norm_pdf}", file=sys.stderr)
                    return 2
                if PROV.sha256_file(norm_pdf) != eff_hash:
                    print("❌ 归一化 PDF 与 manifest 不匹配；拒绝使用可能过期的中间文件。",
                          file=sys.stderr)
                    return 2
                build_source = norm_pdf
            elif eff_hash != src_hash:
                print("❌ manifest 的 effective_source 与 source 状态异常。", file=sys.stderr)
                return 2
            elif norm_pdf.is_file():
                print(f"ℹ️ 忽略未绑定的旧归一化文件: {norm_pdf}")
        elif norm_pdf.is_file():
            print("⚠️ 旧 work-dir 无来源清单，暂按旧行为使用现有 .norm.pdf。")
            build_source = norm_pdf

        build_args = [
            "--source", str(build_source),
            "--blocks", str(blocks_json),
            "--translations", str(translations_json),
            "--output", str(out_pdf),
            "--pages", str(args.pages),
            "--font-scale", str(args.font_scale),
        ]
        if tables_json.is_file() and table_trans_json.is_file():
            build_args += ["--tables", str(tables_json), "--table-dict", str(table_trans_json)]
        if args.font:
            build_args += ["--font", str(args.font)]
        if args.no_indent:
            build_args.append("--no-indent")
        if args.allow_unverified_translations:
            build_args.append("--allow-unverified-translations")
        if args.preview:
            build_args += ["--preview", str(args.preview), "--preview-pages", str(args.preview_pages)]

        rc = run_step(py_exe, "build_dual.py", build_args, "双栏对照 PDF 构建 (build_dual)")
        if rc != 0:
            return rc

        # 完整性验证
        verify_rc = run_step(py_exe, "verify_render.py",
                             ["--blocks", str(blocks_json), "--translations", str(translations_json),
                              "--translated", str(out_pdf)],
                             "逐块渲染完整性验证 (verify_render)")

        # 质检
        qc_args = [
            "--source", str(build_source),
            "--translated", str(out_pdf),
            "--blocks", str(blocks_json),
            "--translations", str(translations_json),
        ]
        if tables_json.is_file() and table_trans_json.is_file():
            qc_args += ["--tables", str(tables_json), "--table-dict", str(table_trans_json)]
        if args.glossary:
            qc_args += ["--glossary", str(args.glossary)]
        if args.ignore_pages:
            qc_args += ["--ignore-pages", str(args.ignore_pages)]

        qc_rc = run_step(py_exe, "qc_check.py", qc_args, "译文多维质检 (qc_check)")

        print("\n" + "=" * 72)
        print("🎉 任务完成！")
        print(f"成品 PDF : {out_pdf}")
        print("=" * 72)
        return 1 if (verify_rc != 0 or qc_rc != 0) else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
