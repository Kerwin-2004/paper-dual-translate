---
name: paper-dual-translate
description: >-
  把英文学术论文 PDF 翻译成「左英文原文 / 右中文对照」的双栏双语 PDF，
  尽量完整保留原版矢量插图、三线表格、行内与行间公式及论文版式排版，
  并通过渲染与质检步骤暴露无法安全保留或排版失败的页面。
  支持两种模式：Agent 直译（模式 A，使用当前 Agent 自身模型，0 外部 API 费用）与
  脚本批量翻译（模式 B，直调 LLM API 无人值守全自动）。
  内置一键式流水线调度（pipeline.py）、视觉质检与跨文献术语自精进知识库。
  当用户要求「双栏对照翻译」「左英右中」「文献翻译成 PDF」「把这篇论文翻译成中文」、
  「双语对照 PDF」「LR_dual」「翻译论文并保留排版」「建文献库/术语库」时使用。
license: MIT
version: 1.5.1
---

# 双栏对照文献翻译（左英文原文 / 右中文对照）

产出 `*.no_watermark.zh-CN.LR_dual.pdf` 双栏成果：页面宽度翻倍，**左半是原封不动的英文原页，右半是中文译页**；右页优先保留原有图表、公式、表格与排版结构，并用渲染和质检结果明确标出例外。

## 两种工作模式

| 模式 | 翻译执行者 | 外部 API Key | 典型场景 |
|---|---|:---:|---|
| **模式 A：Agent 直译（默认）** | 当前 Agent 自身能力 | **不需要** | 精度要求高、重点段落精修、交互式质检 |
| **模式 B：脚本批量全自动** | `auto_translate.py` 直调 LLM API | 需要 (OpenAI 兼容) | 大批量论文翻译、无人值守自动化 |

两种模式使用完全相同的底层排版引擎（`build_dual.py`）与中间文件标准（`translations.json`），完全兼容并可混用。

---

## 快速开始（推荐使用流水线调度器）

本技能提供统一流水线调度器 `scripts/pipeline.py`，无需手动依次记忆并运行各子脚本。

### 模式 A：Agent 直译（两阶段工作流）

1. **第一阶段：一键预处理抽取（v4 列感知自然段模型）**
   ```bash
   python scripts/pipeline.py --source "paper.pdf" --mode prepare --work-dir work
   ```
   > 自动完成：旋转页归一化、**表格优先抽取（题注与表体硬隔离）**、**v4.1 列感知自然段抽取**（自动区分 left/right/full 列、拆分混栏原始块、识别跨栏图片/矢量图/表格 barrier、显式 `flow_index` 阅读序）、bbox 嵌套簇审计。生成 `work/blocks.json`、`work/tables.json` 与 `work/manifest.json` 来源签名。
   > **强烈推荐**：翻译前可运行 `python scripts/debug_flow.py --source paper.pdf --blocks work/blocks.json --pages 1-3 --out work/flow-overlay.pdf` 先行核对真实阅读序。

2. **第二阶段：Agent 翻译生成 JSON**
   - 读 `work/blocks.json`，按下方 [Agent 翻译契约](#agent-翻译硬约束与输出契约) 生成 `work/translations.json`（可按页拆分为 `trans_p1_2.json` 等多文件）；
   - 读 `work/tables.json`，将表格单元格写成词条字典 `work/table_trans.json`。

3. **第三阶段：一键构建、验证与质检**
   ```bash
   python scripts/pipeline.py --source "paper.pdf" --mode build --work-dir work --output "output/paper.dual.pdf"
   ```
   > 自动完成：分页译文合并、双栏 PDF 原位构建（文字抹除+矢量图保留+数学斜体排版+字号自适应放大）、逐块渲染完整性验证（`verify_render`）与综合质检（`qc_check`）。

---

### 模式 B：脚本批量全自动翻译

配置 API 凭证（优先级：命令行 > 环境变量 `PDT_API_KEY` / `PDT_API_BASE` > `config.json`），直接运行全自动流水线：

```bash
# 全自动一条命令完成全流程
python scripts/pipeline.py --source "paper.pdf" --mode auto --output "output/paper.dual.pdf" --workers 4
```
可选参数：`--dry-run`（仅规划不调 API）、`--glossary 术语表.csv`（注入领域术语）、`--ignore-pages "5,16-18"`（豁免纯图版页质检）。

---

## Agent 翻译硬约束与输出契约

在模式 A 下，Agent 产出译文必须严格遵守以下规则：

### 1. 文本块处理原则与状态字段
译文写在 `work/translations.json` 中。`blocks.json` 的每个块都带有 `source_hash`
和版面敏感的 `layout_uid`；Agent 产出条目时应把二者原样复制到译文条目中，例如：
```json
{
  "p1b3": {"zh": "中文译文", "source_hash": "<复制 source_hash>", "layout_uid": "<复制 layout_uid>"},
  "p1b4": {"skip": true, "source_hash": "<复制 source_hash>", "layout_uid": "<复制 layout_uid>"}
}
```
构建器会同时校验文本来源与版面身份，防止重新抽取后 `p1bN` 顺序变化、同页重复文本
互换或 bbox 改变造成静默错配。schema v4 默认拒绝缺少身份字段的旧手工译文；确需兼容时
显式传入 `--allow-unverified-translations`。

状态字段含义：
- **`zh`（翻译）**：标题、摘要、正文、章节标题、图表题注、算法伪代码说明、致谢。
- **`skip: true`（跳过，右半保留原英文）**：
  - 页眉页脚、页码、DOI、刊名、投稿/接收日期、版权行、作者姓名/单位/邮箱/脚注；
  - **参考文献整页**（必须跳过）；
  - **纯公式块（`math_only=True` 或 `kind="math_only"`）**（跳过才能 100% 保留原始 Math 字体字形）；
  - **表格所在的文本块（`kind="table"`）**（表格走独立通道，避免整块替换破坏矢量网格）。
- **`blank: true`（清空）**：仅抹除原文，不写入新字（用于跨行碎片并入主块后的残留清理）。

**v4 列感知与流序关键字段（`blocks.json`）**：
- **`flow_index`**：自然阅读顺序编号。翻译与构建均严格按此顺序串流，保证先读完左栏再读右栏（全宽大图表/标题前后重新分区）。
- **`kind`**：语义类别（`body` / `heading` / `table` / `table_caption` / `figure_caption` / `math_only` / `meta`）。
- **`column`**：分栏属性（`left` / `right` / `full`）。
- **`continues_to_next` / `continues_from_prev`**：检测到跨页连续句子时标记；自动翻译会把标记传给模型，构建器也会据此禁止续段误缩进。
- **`layout_uid`**：页码、列、量化 bbox 与源文本共同生成的版面身份；必须与 `source_hash` 一起复制。

### 2. 数学公式与符号排版规范
- PDF 中提取出的行内特殊数学符号（如 $\mathcal{S}, \mathcal{A}, \mathcal{P}, \gamma$）可能缺失字体码位，redaction 会抹除它们；
- Agent 必须将其转写为 **纯 ASCII 上下标标记法**：
  - `S_t`、`S_{t+1}`、`Δ_i`、`θ`、`a_rl^t`、`x^2`、`N_p`；
- `build_dual.py` 的排版引擎会自动将标记解析为原生 `<sub>/<sup>` 上下标标签，并使用数学字体渲染为斜体（复制出来的依然是标准 ASCII 字符，完全支持全文搜索）。

### 3. 表格单元格翻译字典
表格单元格译文写在 `work/table_trans.json` 中，采用 **`{英文原单元格: 中文译文}`** 词条字典格式：
```json
{
  "Success rate": "成功率",
  "Low density": "低密度",
  "Method": "方法",
  "Table 1": "表 1"
}
```
同名单元格自动复用，构建引擎自动计算单格原有字号并原位居中/靠左替换，保留所有矢量边框。

### 4. 段落排版与换行格式
- **译文中严禁插入手动换行符 (`\n`)**：所有换行由排版引擎根据自然段 bbox 宽度自动计算；构建器会兜底清除误产生的硬换行。
- **段落合并以版面几何为主**：同列、同行距连续的碎片即使前一块以句号结束、下一块以大写字母开头，也会先合并为一个自然段再翻译/排版；列表、参考文献、题注等结构边界仍保持独立。
- **首行缩进**：默认自动执行中文排版标准（段首空两格全角空格 `\u3000\u3000`）。标题后首块必缩进；断句接续块（逗号结尾/公式槽位跨段）不缩进。

---

## 视觉质检（具备视觉能力的 Agent 必做）

### 1. 翻译前：流序可视化检查（推荐，debug_flow）
在预处理完成后、启动翻译前，快速生成阅读流序可视化 PDF，彻底将“版面抽取错序”与“翻译问题”解耦：
```bash
python scripts/debug_flow.py --source "paper.pdf" --blocks work/blocks.json --pages 1-3 --out work/flow-overlay.pdf
```
> 每个文本框左上角标有 `flow_index kind column id`；跨栏图片、矢量图和表格 barrier 以橙色虚线框显示。重点核实编号是否按“图上左栏 → 图上右栏 → 跨栏对象 → 图下左栏 → 图下右栏”推进，且表题和表体没有混流。

### 2. 构建后：排版与渲染视觉质检（render.py）
纯文本提取无法查出文字遮挡图片、字体叠印或视口裁切。使用 `scripts/render.py` 生成可视化图像进行核查：

```bash
# 1. 缩略总览全篇（扫排版异常页）
python scripts/render.py contact --pdf output/paper.dual.pdf --out work/scan.png --cols 3

# 2. 逐页对照核验（左英文、右中文）
python scripts/render.py compare --source "paper.pdf" --translated output/paper.dual.pdf --pages 1 --out work/cmp1.png

# 3. 局部高清放大检查
python scripts/render.py strip --pdf output/paper.dual.pdf --page 5 --top 600 --bottom 750 --zoom 3 --out work/zoom.png
```
> **注意**：`contact` 缩略图在低采样率下会产生字形伪影。**任何视觉怀疑必须截取局部高清图（zoom ≥ 2）复核再下结论**。

---

## 知识库积累与跨文献自精进

所有文献题录与术语沉淀保存在知识库中（默认 `~/.paper-dual-translate/kb`，或由 `config.json` 指定）：

```bash
# 1. 翻译前：导出术语清单喂给 Agent，保证跨文献术语统一
python scripts/kb.py export --format prompt

# 2. 翻译后：自动挖掘新术语并更新置信度（自精进核心）
python scripts/kb.py harvest --blocks work/blocks.json --translations work/translations.json --paper-id <id> --domain <学科> -v

# 3. 归档题录与索引
python scripts/kb.py add-paper --source-pdf "paper.pdf" --blocks work/blocks.json --translations work/translations.json --translated-pdf output/paper.dual.pdf
```

---

## 详细参考指南 (Progressive Disclosure)

- [排错手册与常见避坑指南](./references/troubleshooting.md)：旋转页处理、401/429 API 报错、放不下留空、环境踩坑应对；
- [字体兼容性与 ToUnicode 指南](./references/font_guide.md)：为什么禁用思源/Noto 字体、CJK 兼容码位陷阱、跨平台字体安装；
- [学术翻译提示词模板](./references/translate_prompt.txt)：批量翻译所用的学术严谨性 Prompt 规范；
- [技术架构与独立脚本手册](./README.md)：各独立子脚本底层原理与单独调用参数说明。
