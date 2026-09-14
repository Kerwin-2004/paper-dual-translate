# paper-dual-translate

把英文论文 PDF 译成 **左英文原文 / 右中文对照** 的双栏双语 PDF，图表、公式、表格、目录全部保留。
附带**文献知识库**与**术语对照知识库**，每翻译一篇就沉淀一次，越用越准。
---


## 它做什么

| 能力 | 说明 |
|---|---|
| 双栏对照 PDF | 页宽 2 倍，左半是**原封不动的原文页**，右半是中文页；图表/公式/表格/书签全保留 |
| 表格翻译 | 按单元格替换，矢量网格不破坏（不是整块覆盖） |
| 公式排版 | 行内公式按**数学斜体**渲染，与保留下来的显示公式风格一致；纯公式块原样不动 |
| 文献知识库 | 自动登记题录、中译标题/摘要/关键词，产出可检索的索引 |
| 术语对照库 | 每篇翻译自动挖掘 + 确认术语，跨文献累积置信度 |
| 视觉质检 | 把页面渲染成图，让 agent 直接"看"有没有压字、重叠、裁切 |

## 两种工作模式

**排版与构建完全一样**（都是 抽取 → 翻译 → `build_dual` 原位替换），区别只在"谁在翻译"：

| | 谁在翻译 | 需要 API Key | 适用 |
|---|---|---|---|
| **A. Agent 直译（默认）** | 当前 agent 自己 | 不需要 | 精度要求高、要看图精修 |
| **B. 脚本批量** | `auto_translate.py` 直调 LLM API | 需要 | 批量、无人值守 |
两种模式产出的 JSON 格式完全一致，排版引擎与质检完全共用，可根据需要灵活混用。
整个流水线仅依赖 Python 标准库与 PyMuPDF（`pip install pymupdf`）。

### 构建原理（为什么图表/公式不会坏）

右半页的底图就是**原始矢量内容本身**，不是重新排版的结果：

1. 用 redaction **只抹掉原文的文字字形**（`graphics=LINE_ART_NONE, images=IMAGE_NONE`）——
   矢量插图、三线表格的框线、公式字形全部原样留在页面上；
2. 再在原块 bbox 内 `insert_textbox` / `insert_htmlbox` 写入中文，字号自适应；
3. 最后新建**两倍宽**页面：左半 `show_pdf_page` 贴原页，右半贴改好的译页。

所以"图和论文结构不变"是这套做法的**结构性保证**，不靠后期对齐或人工调版。

---

## 快速开始

### 0. 环境自检

```bash
python scripts/config.py            # 看配置、字体、解释器、API Key 是否就绪
```

没装依赖的话：`pip install pymupdf`（唯一硬依赖）。
配置优先级：**命令行参数 > 环境变量 > `config.json` > 自动探测**。

---

### 推荐：一键流水线调度器（pipeline.py）

无需手动依次调用 8~10 个独立脚本，推荐直接使用 `scripts/pipeline.py`：

#### 全自动批量翻译（模式 B）
```bash
# 一条命令完成旋转检查、抽取、审计、批量翻译、构建、渲染验证与综合质检
python scripts/pipeline.py --source paper.pdf --mode auto --output output/paper.dual.pdf --workers 4
```

#### Agent 直译协作（模式 A）
```bash
# 步骤 1：一键预处理抽取（生成 work/blocks.json 与 work/tables.json）
python scripts/pipeline.py --source paper.pdf --mode prepare --work-dir work

# 步骤 2：Agent 产出 translations.json 与 table_trans.json 译文（见 Agent 契约）

# 步骤 3：一键构建双栏 PDF 并运行质检
python scripts/pipeline.py --source paper.pdf --mode build --work-dir work --output output/paper.dual.pdf
```

---

### 分步独立脚本运行方式（供进阶调优与单步调试）

如果需要单独调试特定环节，各子脚本依然支持独立运行：

#### 1. 预处理与抽取

```bash
# 旋转页检查（有旋转页必须先归一化，之后所有 --input/--source 都改用 source_norm.pdf）
python scripts/normalize_pdf.py --check --input paper.pdf
python scripts/normalize_pdf.py --input paper.pdf --output work/source_norm.pdf   # 仅当有旋转页

python scripts/extract_blocks.py --input paper.pdf --output work/blocks.json --pages all
python scripts/extract_tables.py --input paper.pdf --output work/tables.json --pages all

# bbox 嵌套簇审计：段落块包住行内公式碎片会导致误删公式/中文叠印，翻译前先修补
python scripts/audit_nested_blocks.py --blocks work/blocks.json --apply --skeleton work/skeleton.json

# 段落级断句：把被切碎的段落块合并回整段（译文按段落写，不按英文行）
python scripts/merge_paragraphs.py --blocks work/blocks.json --tables work/tables.json --apply
```

### 2. 翻译（两种做法，产出格式一致）

**做法 A（agent 直译，默认）**：Agent 读 `blocks.json`，逐块产出 `translations.json`：

```json
{
  "p1b3":  {"zh": "DuSA：一种面向自动驾驶的 LLM 引导强化学习双环自学习框架"},
  "p1b4":  {"skip": true},
  "p7b10": {"blank": true}
}
```

- `zh` —— 该块的译文
- `skip` —— 保留英文原样（页眉、DOI、作者、参考文献、纯公式块、表格所在块）
- `blank` —— 只抹掉原文不写新字（用于把跨行片段并到相邻块时清残留）

表格译文写成一个**词条字典**（同名单元格自动共用一条）：

```json
{ "Success rate": "成功率", "Low density": "低密度", "Table 1": "表 1" }
```

实操建议按页拆成 `work/trans_p1_2.json`、`work/trans_p3_4.json`… 最后合并
（同名键冲突会逐条报告并以退出码 1 退出），表格译文写 `work/table_trans.json`。

**做法 B（脚本批量，无人值守）**：脚本直调 LLM API，不依赖 agent：

```bash
# 先看分批与跳过计划（不调 API）
python scripts/auto_translate.py --blocks work/blocks.json \
    --output work/translations.json --dry-run

# 正式跑（API 配置：命令行 > PDT_API_* > config.json）
python scripts/auto_translate.py --blocks work/blocks.json --output work/translations.json \
    --tables work/tables.json --table-dict-out work/table_trans.json \
    --glossary work/glossary.csv --workers 4
```

- 跳过策略内置（页眉页脚/元数据/作者单位/参考文献/公式碎片/表格区域/横排页…），
  个别要强译的用 `--force-ids p5b3`；`--no-skip` 可全关。
- `--resume` 默认开：中断后重跑只补缺的块。401/403 会立即中止并提示检查 Key。
- 可选：支持通过 `--p2z-config <config.toml>` 从本地已有配置中导入 API Key。

### 3. 合并与构建

```bash
python scripts/merge_translations.py --dir work --output work/translations.json

python scripts/build_dual.py \
  --source paper.pdf \
  --blocks work/blocks.json --translations work/translations.json \
  --tables work/tables.json --table-dict work/table_trans.json \
  --output "out/paper.no_watermark.zh-CN.LR_dual.pdf" \
  --preview work/preview.png --preview-pages 1,11
```

输出命名规范：`<原名>.no_watermark.zh-CN.LR_dual.pdf`（双栏对照标准格式）。
中文字号默认自动放大（`--font-scale 1.2` 起步、块内自适应），段首默认空两格
（`--no-indent` 关闭），公式 ASCII 记号自动渲染成真上下标。

### 4. 质检（文本 + 渲染完整性 + 视觉）

```bash
# 文本层质检（--ignore-pages 豁免整页图版/参考文献页，避免误报）
python scripts/qc_check.py --source paper.pdf --translated out/paper.no_watermark.zh-CN.LR_dual.pdf \
  --blocks work/blocks.json --translations work/translations.json \
  --tables work/tables.json --table-dict work/table_trans.json

# 渲染完整性：每块译文首尾必须能从成品提取到（区分渲染丢字与查看窗口裁剪）
python scripts/verify_render.py --blocks work/blocks.json --translations work/translations.json \
  --translated out/paper.no_watermark.zh-CN.LR_dual.pdf

# 视觉质检：生成对照图，让 agent 用图像能力直接看
python scripts/render.py contact --pdf out/paper.no_watermark.zh-CN.LR_dual.pdf --out work/scan.png --cols 3
python scripts/render.py compare --source paper.pdf --translated out/paper.no_watermark.zh-CN.LR_dual.pdf \
  --pages 11 --out work/cmp11.png
```

### 5. 沉淀到知识库

```bash
python scripts/kb.py init
python scripts/kb.py harvest   --blocks work/blocks.json --translations work/translations.json \
                               --paper-id xu2026-dusa --domain autonomous-driving -v
python scripts/kb.py add-paper --source-pdf paper.pdf --blocks work/blocks.json \
                               --translations work/translations.json \
                               --translated-pdf out/paper.zh-CN.LR_dual.pdf
python scripts/kb.py export --format prompt        # 给下一次翻译用的术语清单
python scripts/kb.py stats
```

下次翻译前先 `export --format prompt`，把术语清单喂给 agent，术语就跨文献一致了。

---

## 安装与迁移（换到别的 agent / 别的机器）

技能是**自包含**的：整个 `paper-dual-translate/` 目录复制到哪里都能跑。脚本只引用
目录内的文件（`references/` 种子表与提示词），不依赖目录外的任何路径。

```bash
# 1) 复制目录到目标 agent 的技能目录（各产品的 skills 根目录不同；
#    不支持 skills 机制的 agent 也能用——只要能读文件、写文件、看图）
cp -r paper-dual-translate <目标>/skills/

# 2) 装唯一硬依赖（用目标机器上带 pip 的解释器）
pip install pymupdf

# 3) 自检：打印 API 配置/解释器/字体/知识库/工作目录的实际位置与缺失项
python paper-dual-translate/scripts/config.py
```

环境变量一览（完整定义见 `scripts/config.py` 顶部）：

| 变量 | 用途 |
|---|---|
| `PDT_PYTHON` | 指定跑脚本的解释器（默认自动探测，优先当前解释器） |
| `PDT_FONT` / `PDT_FONT_BOLD` | 中文正文 / 标题字体文件 |
| `PDT_MATH_FONT` | 数学字体（渲染行内公式的斜体上下标） |
| `PDT_KB_DIR` | 知识库目录（默认 `~/.paper-dual-translate/kb`） |
| `PDT_WORK_DIR` | 工作目录（默认 `~/.paper-dual-translate/work`） |
| `PDT_HOME` | 上面两个默认目录的根 |
| `PDT_API_BASE` / `PDT_API_KEY` / `PDT_API_MODEL` | 模式 B 用的 OpenAI 兼容接口 |

迁移注意：

- **`config.json` 是本机私有配置，不要随技能复制**（它可能钉着本机专属路径）。
  需要固定路径时，从 `config.example.json` 复制一份再改。
  优先级：**命令行参数 > 环境变量 > config.json > 自动探测**。
- **字体是换机器后的头号坑**：中文正文/标题字体必须"文字可被提取"
  （见 `references/font_guide.md`：CJK 兼容区码位陷阱与实测可用字体清单）。
  `build_dual.py` 内置探针自检，会自动跳过不合格的字体并打印告警；
  Linux/macOS 上若字体全不合格，用 `--font` 指定一个 ToUnicode 正常的 CJK 字体文件。
- 知识库与工作目录默认在 `~/.paper-dual-translate/`（跨项目复用），
  用 `PDT_KB_DIR` / `PDT_WORK_DIR` 环境变量或 `config.json` 改。**知识库是资产，
  迁移时按需单独拷**（`glossary.csv` + `literature.jsonl`），它不在技能目录里。
- 模式 B（脚本批量）支持任意 OpenAI 兼容的 LLM 接口：在 `config.json` 中配置 `api_base` / `api_key` /
  `api_model`，或直接设置 `PDT_API_KEY` 等环境变量。

---

## 翻译格式约定与输出契约（模式 A）

在模式 A 下，翻译输出需遵循以下规范：

1. **输入与输出**：
   - 读取：`work/blocks.json`（正文段落）与 `work/tables.json`（表格单元格）。
   - 产出：`work/translations.json` 与 `work/table_trans.json`。
2. **核心硬约束**：
   - **段内不保留硬换行**：排版引擎会自动根据页面原始文本块的几何尺寸自适应折行与计算字号。
   - **数学公式标记**：行内数学变量使用 ASCII 下标表示法（如 `a_rl^t`、`S_{t+1}`、`Δ_i`），排版引擎会自动渲染为统一的数学斜体。
   - **表格块处理**：表格所在文本块在 `translations.json` 中标记为 `skip`，由表格专用通道独立处理。
3. **视觉质检**：
   - 可调用 `render.py` 渲染指定页面的 PNG 高清图像，直观核验文字排版与公式渲染效果。

---

## 目录结构

```
paper-dual-translate/
├── SKILL.md                            技能规范说明（遵循渐进披露规范）
├── README.md                           项目使用说明
├── config.example.json                 配置模板（复制为 config.json）
├── .gitignore                          版本控制忽略规则
├── scripts/
│   ├── pipeline.py                     一键式全自动 / 分步流水线调度器
│   ├── config.py                       统一配置解析与环境自检（doctor）
│   ├── normalize_pdf.py                旋转页检测与烘焙归一化
│   ├── extract_blocks.py               源 PDF -> 段落级文本块
│   ├── extract_tables.py               源 PDF -> booktabs 表格单元格拆解
│   ├── audit_nested_blocks.py          bbox 嵌套簇审计/修补（公式碎片归簇）
│   ├── merge_paragraphs.py             段落断句合并（几何与语义综合续接）
│   ├── merge_translations.py           合并分页译文 trans_p*.json
│   ├── verify_render.py                逐块渲染完整性验证（排查丢字与截断）
│   ├── build_dual.py                   译文 -> 双栏 PDF 构建（模式 A/B 核心）
│   ├── qc_check.py                     多维度综合质检报告
│   ├── render.py                       页面渲染工具（视觉质检与人工核查）
│   ├── kb.py                           文献知识库与跨文献术语自精进系统
│   ├── glossary.py                     单篇术语表合并与块级审计
│   └── auto_translate.py               脚本批量翻译：直调 LLM API（模式 B）
└── references/
    ├── translate_prompt.txt            科研论文批量翻译提示词契约
    ├── glossary_seed_autonomous_driving.csv 领域种子术语表示例
    ├── font_guide.md                   字体兼容性与 ToUnicode 规范
    └── troubleshooting.md              排错手册与常见避坑指南
```

知识库默认落在 `~/.paper-dual-translate/kb/`（跨项目复用），可用 `PDT_KB_DIR`
环境变量或 `config.json` 的 `kb_dir` 改。知识库不在技能目录里，迁移时按需单独拷。

---

## 可扩展点

- **换学科**：`references/` 下加一份种子术语表，`kb.py init` 会自动并入
- **换语言**：模式 A/B 都面向中文；换语言需同时换字体、句尾标点表
  （`build_dual.py` 的 `_TERM_TAIL`）与首行缩进判定
- **换翻译服务**：模式 B 只要 OpenAI 兼容接口，改 `api_base` / `api_model` 即可
  （DeepSeek、通义、硅基流动、OpenAI、任意网关都行）
- **换提示词**：改 `references/translate_prompt.txt`（保留"只输出 JSON 对象"的输出契约）
- **加质检项**：在 `qc_check.py` 的 `add(...)` 里加，报告会自动汇总
