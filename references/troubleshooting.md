# 排错手册 · paper-dual-translate

## 环境类

### 提示需要 PyMuPDF
```
❌ 需要 PyMuPDF（import fitz）
```
唯一硬依赖：`pip install pymupdf`。装完 `python scripts/config.py` 自检。
若系统里有多个解释器，注意用**同一个**跑所有脚本（否则会出现"脚本能跑但依赖缺失"的错配）。

### 中文 Windows 下打印乱码 / UnicodeEncodeError
输出被重定向到文件或被上层管道捕获时，Windows 默认用 cp936 编码，
脚本里的中文与 ✅⚠️ 会直接抛错。本技能已在 `config.py` 与各脚本里把
stdout/stderr 固定为 UTF-8；若仍有问题，可显式设环境变量：
```powershell
$env:PYTHONIOENCODING = "utf-8"
```
另外 PowerShell 会话的 stdout 有时会被吞，用 `*> file` 重定向后再读文件。

### 换机器后中文 PDF 不能复制/搜索
字体问题（不是构建问题）。本技能**只能**用"文字可提取"的 CJK 字体：
`build_dual.py` 内置探针自检并自动跳过不合格字体，会在启动时打印实际选用的字体。
已知**不可用**：NotoSerifSC-VF/NotoSansSC-VF、SourceHanSerifCN-*、simsunb、SimsunExtG。
Linux/macOS 上若候选全不合格，用 `--font /path/to/font.ttf` 指定一个 ToUnicode 正常的字体。

---

## 模式 B（脚本批量翻译 / auto_translate.py）

### 缺少 API Key
```
❌ 缺少 API Key。三种给法：
```
1. 环境变量：`PDT_API_KEY` / `PDT_API_BASE` / `PDT_API_MODEL`
2. 技能目录 `config.json`：`api_key` / `api_base` / `api_model`
3. 命令行 `--api-key`，或 `--p2z-config <config.toml>` 从既有 zotero-pdf2zh 配置导入

快速确认当前状态：`python scripts/config.py`（会打印 API 来源与 key 尾号）。

### 401 / 403 鉴权失败
脚本会**立即中止**（不重试、不二分），并打印服务端返回的原因，例如：
```
❌ API 鉴权失败，已中止后续请求：HTTP 401 {"error":{"message":"Authentication Fails..."}}
```
排查：key 是否过期/被吊销、`api_base` 是否与该 key 所属服务商一致
（DeepSeek 的 key 不能配到硅基流动的 base 上）。
**注意**：若从本地历史配置文件导入的 key 已失效，请直接用 `--api-key` 或环境变量提供有效 key 验证。

### 429 限流 / 请求超时
- 降并发：`--workers 2`（默认 4）
- 减小批次：`--batch-chars 2000`
- 增大超时：`--timeout 300`
- 换服务或稍后重跑；`--resume` 默认开，重跑只补缺的块。

### 某些块始终没译出来
脚本结尾会列出未译出的块 id。通常是该批内容过长/格式特殊，模型没返回合法 JSON。
处理：`--batch-chars 1500 --retry 3` 重跑（`--resume` 只补这些块）；
仍不行就手工在 `translations.json` 里补这几条，格式 `{"zh": "..."}`。

### 跳过策略误判
内置启发式：页眉页脚 / 刊名·DOI·版权 / 作者单位 / 通讯作者 / 参考文献 /
纯公式块 / 行间公式碎片 / 表格区域 / 横排整页图表 / 表格图版碎片 / 首页题录区。
- **该跳的没跳** → `--skip-pages 5,38-39` 整页跳过，或事后在该块上写 `{"skip": true}`
- **该译的被跳了** → `--force-ids p31b7,p35b6` 强制翻译
- **全关启发式** → `--no-skip`（会把参考文献页也送去翻译，慎用）

先 `--dry-run` 看计划（打印各原因各跳多少块），确认后再正式跑。

### 想省 token
- 关掉跳过策略没有意义（反而更贵）；真正省钱的是：
  `--batch-chars` 调大（减少提示词重复开销）、`--glossary` 只放真正需要的术语
- 参考文献/图版页占比大的论文，跳过策略本身已经省掉一大半
- 只补缺的块：`--resume`（默认开）

---

## 翻译质量

### 术语不统一 / 译错
1. 把正确写法写进术语表 CSV（`source,target`），然后重译受影响块：
   ```bash
   python scripts/kb.py export --format glossary-csv --out work/glossary.csv
   # 手工增补/修正后
   python scripts/auto_translate.py ... --glossary work/glossary.csv --force-ids p5b3,p5b4
   ```
2. 或者直接用 Agent 直译（模式 A）改那几块：手工替换 `translations.json` 里对应条目后重新构建。
3. 术语库是长期资产：裁决完用 `kb.py harvest` / `add-paper` 沉淀，下次自动受益。

### 术语覆盖率低 ≠ 译文差（别盲目追指标）
自动抽取的术语表**本身常有错**。实测某 Elsevier 论文的自动术语表：
- `RS -> 随机搜索` —— 错，文中 RS 是 Reward Shaping，应为「奖励重塑」
- `headway -> 车头时距` 与 `headway distance -> 车头间距` —— 自相矛盾
- `ramp merge -> 匝道汇入` —— 不如「匝道合流」标准

所以**覆盖率低不等于译文差**。正确顺序是：
1. 先**核对术语表本身**（`kb.py export --format markdown` 导出通读一遍），不要拿它直接当标准；
2. 用 `glossary.py merge` 把「领域种子表 → KB 导出 → 人工修订表」按优先级叠起来（后者优先）；
3. 跑 `glossary.py audit` 定位真正"该用未用"的术语，**先裁决术语表，再定点重译受影响块**；
4. 匹配必须用**词边界**（audit 已内置），否则 `USA` 会命中 `usage`、`RS` 会命中 `researchers`。

### 右半出现英文（块没被翻译）
先跑 `qc_check.py` 定位页/块：
- `translations.json` 里标了 `skip` → 设计内行为（参考文献、元数据、公式碎片等）
- 该批请求失败留空 → 见上面「某些块始终没译出来」
- 译了但**内容跑到框外** → 见「某个块放不下，已留空」，并用 `verify_render.py` 复核

---

## 版面类

### 图片 / 公式错位或丢失
- 旋转页必须先归一化：`normalize_pdf.py --check`，有旋转页就归一化后
  用 `source_norm.pdf` 重新抽取与构建（否则坐标错位）。
- 段落块包住行内公式碎片（bbox 嵌套簇）会导致公式被 redaction 抹掉：
  翻译前跑 `audit_nested_blocks.py --blocks ... --apply`。

### 公式被当成正文翻译了
本技能的规矩是：**纯公式块（`math_only`）与行间公式碎片一律保留英文**，
行内公式只用 ASCII 记号写进中文流（`mark_math` 渲染成真上下标）。
若发现公式区出现中文：
- 检查 `blocks.json` 里该块的 `math_only` / `has_math` 标记，必要时手工加 `{"skip": true}`
- 模式 B 下是跳过启发式没覆盖 → 用 `--force-ids` 的反面（写 skip）或 `--skip-pages`
- 若 `qc_check.py` 报左半出现中文，说明原文区被动了，属构建参数问题

---

## 质检（qc_check / verify_render / render）

### 「疑似未翻译页」误报与 --ignore-pages
整页只有一张图 + 一个题注的**图版页**、图续页、参考文献页，右半 CJK 占比天然偏低，
会被 qc_check 判成「疑似未翻译」（WARN）。做法是：
**先不带 `--ignore-pages` 跑一遍**，把报出来的图版页/文献页写进豁免列表，再跑一次，
直到 `WARN 0 / FAIL 0`。

以 Kaviani 2026（TRC，39 页）为例，完整列表是：
```bash
--ignore-pages "5,16-18,32,34,36,37,38,39"
```
（5 = 横排 ✓/✘ 矩阵表页；16-18 = 表格密集页；32/34/36 = 整页图版页；
37 = 图续页 + 参考文献起始；38-39 = 参考文献页）
豁免只是**不检查**这些页，不会改动成品内容。列表要按实际文档来，不要照抄。

### 缩略图会骗人：视觉质检必须放大复核
`render.py contact` 在低采样率下会产生**字形伪影**，缩略图上像"文字碎片"的地方，
放大后往往是干净的。**任何视觉怀疑都必须截局部高清图（zoom ≥ 2）复核再下结论。**
```bash
python scripts/render.py contact --pdf out.pdf --out work/scan.png --cols 3   # 先扫全篇找可疑页
python scripts/render.py compare --source paper.pdf --translated out.pdf --pages 11 --out work/cmp11.png
python scripts/render.py strip --pdf out.pdf --page 15 --top 690 --bottom 790 --half right --zoom 4
python scripts/render.py page --pdf out.pdf --pages 1 --out work/p1.png --zoom 2
python scripts/render.py probe --pdf out.pdf --page 7 --out work/grid.png --step 50   # 叠坐标网格，便于写 bbox
```
>`verify_render.py` 只能证明**文字都在**，证明不了"位置对、没压图"——两类质检都要做。

---

## 运行环境与跨平台注意事项

### 跨平台 Shell 命令兼容
不同操作系统下的 Shell 环境（如 Windows PowerShell / Git Bash / Linux bash / macOS zsh）可能表现不一，建议优先使用 Python 原生脚本，避免依赖特定的管道命令（如 `head`、`sed`、`cat`）。

### 文件删除与清理保护
在部分带有受保护文件系统或回收站拦截的终端环境下，shell 的 `rm` 命令可能受阻，可使用 Python 的 `os.remove()` 或 `shutil.rmtree()` 进行可靠清理。

---

## 模式 A（Agent 直译）常见问题

### 知识库相关
- **`kb.py` 说找不到库** → 先 `kb.py init`；库默认在 `~/.paper-dual-translate/kb`，
  用 `PDT_KB_DIR` / `--kb` 改，或写进 `config.json` 的 `kb_dir`。
- **挖掘出的术语明显不对**（如"抓到整句"）→ 缩写桥接的三道精度闸门被绕过了。
  在 `kb.py` 的 `_plausible()` 里加规则，或调低该条的 `confidence`
  并从翻译用词表里排除（`export --min-confidence` 控制）。
- **同一术语出现译法冲突** → `stats` 会报"存在译法冲突 N 条"，看 `target_alt` 列人工裁决：
  把选定的译法写进 `target`，`confidence` 调高到 `0.95`（= manual）。
- **术语库越用越乱** → 定期 `export --format markdown` 导出通读，
  删噪声、裁决冲突。KB 是资产，但需要偶尔打理。

### 表格没被识别成表格
`page.find_tables()` 对 booktabs 风格（只有横线）的表格不可用。本技能用
`extract_tables.py` 自己解析（题注锚定 + 横线归属 + 文字聚类）。
如果某张表没被解析出来，检查：
- 题注是否写成 `Table N`（正则 `Table\s+\d+`）。没有题注的表不会被识别。
- 表格的横线是否是矢量线。若表格是用图片画的，解析不了，只能整块保留。
- 并排两张表题注 y 相同时，靠"是否同一半页"裁决；若两表都在同一栏内堆叠，可能误判。

### 某个块"放不下，已留空"
`insert_textbox` 放不下时不写内容并返回负数。脚本会自动逐级缩小字号；
缩到 4.5pt 仍放不下才留空。常见原因：
- 译文比原文长太多（中文虽短，但公式/缩写展开后会变长）。
- 该块 bbox 很矮（单行块）。可把译文压得更短，或接受留空后手工补。

### 公式渲染成了正体、没有数学斜体
`mark_math()` 只对"上下文明确是数学符号"的记号套斜体。某个符号没被套上，
说明它不符合识别规则（例如被 `(` 紧跟、或嵌在更长的单词里）。
必要时在译文里手工写 `<span class="m">X</span>`（`build_dual` 会原样保留）。
注意：译文里一旦含 ASCII `<`/`>`，会被 `html_escape` 转义，别拿它们当标记。

### CSS 指定字体不生效
必须用 `@font-face{font-family:别名;src:url(字体文件名)}` +
`body{font-family:别名}`，且 `url()` 里的文件名要能在 `--font` 所在目录找到。
直接写 `font-family:'STSong'` 不生效，会静默回退到 MuPDF 内置的 Droid Sans。
