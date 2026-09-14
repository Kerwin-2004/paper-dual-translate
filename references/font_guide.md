# 字体兼容性与 ToUnicode 避坑指南 · paper-dual-translate

在双栏对照 PDF 构建中，字体选择是决定最终 PDF **是否可搜索、可复制、术语可匹配**的核心环节。

---

## 1. 核心陷阱：CJK 兼容区码位映射错误

### 现象
使用某些常见的思源字体或 Noto 字体生成 PDF 后：
- 在 PDF 阅读器（如 Adobe Acrobat、Zotero 内置阅读器、浏览器）中选中文本复制，复制出来的是乱码或异体汉字；
- 无法使用 `Ctrl+F` 搜索中文关键字；
- `qc_check.py` 会报告搜索性测试失败。

### 原因
`NotoSerifSC-VF.ttf`、`NotoSansSC-VF.ttf`、`SourceHanSerifCN-*` 以及部分新版 OpenType 字体经 PyMuPDF 嵌入 PDF 时，其生成的 `ToUnicode` 映射表会将常用汉字映射到 **CJK Compatibility Ideographs（CJK 兼容区码位）**：
- `器` → 被映射为 `U+FA38`（而非标准的 `U+5668`）
- `力` → 被映射为 `U+F98A`（而非标准的 `U+529B`）
- `远` → 被映射为 `U+FA8E`（而非标准的 `U+8FDC`）

> **原理**：部分新版可变字体内部未嵌入标准 CMap 表，依赖外部重度渲染器动态生成映射流。在基于 PyMuPDF 的轻量化构建体系中，必须选择本身能被正确提取的字体。

---

## 2. 字体兼容性实测清单

| 字体名称 | 字体文件示例 | 提取测试 | 推荐用途 |
|---|---|:---:|---|
| **华文宋体** | `STSONG.TTF` | ✅ PASS | 正文首选（学术风格庄重） |
| **华文中宋** | `STZHONGS.TTF` | ✅ PASS | 章节标题首选（粗细适中） |
| **中易宋体** | `simsun.ttc` | ✅ PASS | 备选正文 |
| **等线** | `Deng.ttf` / `Dengb.ttf` | ✅ PASS | 现代无衬线备选 |
| **微软雅黑** | `msyh.ttc` / `msyhbd.ttc` | ✅ PASS | 备选正文与标题 |
| **中易黑体** | `simhei.ttf` | ✅ PASS | 备选标题 |
| **文鼎宋体 (Linux)** | `uming.ttc` | ✅ PASS | Linux 环境备选 |
| **宋体 (macOS)** | `Songti.ttc` / `Heiti.ttc` | ✅ PASS | macOS 环境内置 |
| **思源系列** | `SourceHanSerifCN-*.ttf` | ❌ FAIL | **禁止使用** |
| **Noto SC 变体** | `NotoSerifSC-VF.ttf` | ❌ FAIL | **禁止使用** |
| **扩展字库** | `simsunb.ttf` / `SimsunExtG.ttf` | ❌ FAIL | **禁止使用** |

---

## 3. 内置探针自检机制 (`verify_font`)

`scripts/build_dual.py` 内置了探针自检函数：
```python
_FONT_PROBE = "安全过滤器 潜力 远离 车辆 策略 换道 匝道 合流"
_FONT_PROBE_CHARS = "器力远"
```
在构建或自检时，脚本会自动创建一个内存虚拟 PDF 页面，使用待检测字体插入探针文本，并立即用 `page.get_text()` 读回。如果读回内容未包含标准的 `器力远` 字符，脚本会自动判定该字体不合格，并在启动时发出告警，自动回退到下一个候选字体。

---

## 4. 跨平台配置建议

### Windows
Windows 默认通常自带华文宋体与中宋：
- 正文：`C:\Windows\Fonts\STSONG.TTF`
- 标题：`C:\Windows\Fonts\STZHONGS.TTF`
- 数学字体：`C:\Windows\Fonts\cambria.ttc`

### Linux (Ubuntu / Debian / CentOS)
推荐安装 `fonts-arphic-uming` 或提取系统 TrueType 字体：
```bash
sudo apt-get install -y fonts-arphic-uming fonts-crosextra-carlito
```
若使用自定义字体文件，可通过环境变量或命令行参数指定：
```bash
export PDT_FONT="/usr/share/fonts/truetype/arphic/uming.ttc"
```

### macOS
macOS 内置字体路径通常为：
- 正文：`/System/Library/Fonts/Supplemental/Songti.ttc`
- 标题：`/System/Library/Fonts/Supplemental/Heiti.ttc`
- 数学字体：`/System/Library/Fonts/Supplemental/Cambria Math.ttf`

---

## 5. 自定义配置方式

如果系统默认路径找不到合适的字体，可以在技能根目录下的 `config.json`（由 `config.example.json` 复制而来）中直接指定：

```json
{
  "font": "/path/to/valid_songti.ttf",
  "font_bold": "/path/to/valid_heiti.ttf",
  "math_font": "/path/to/valid_cambria_math.ttf"
}
```
优先级：**命令行参数 `--font` > 环境变量 `PDT_FONT` > `config.json` > 系统自动探测**。
