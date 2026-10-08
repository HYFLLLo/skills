# 处理超大 PDF（本地分章提取策略）

当图书 PDF 超过上下文窗口时，不能一次性读完。全部使用本地 `extract_pdf.py` 按章分块提取，不依赖任何远程服务。

## 前置步骤：确认脚本可用

```bash
python "<skill目录>/scripts/extract_pdf.py" --help
```

依赖 `pymupdf`（必需）、`rapidocr_onnxruntime`（仅扫描版 OCR 时按需加载）。缺依赖时用 `pip install pymupdf` 或 `pip install rapidocr-onnxruntime` 补齐。

## 第一步：建立全书地图

导出内嵌目录，得到「章节 → 起始页码」映射：

```bash
python "<skill目录>/scripts/extract_pdf.py" "<PDF路径>" --dump-toc
```

输出形如 `- L1 [第 12 页] 第 2 章 信息技术发展`。据此确定每章起始页，构建 `<book-slug>`（书名的小写连字符形式，如 `deep-learning`）。

若书籍无内嵌目录，读正文前若干页（前言/目录页）推断章节结构。

## 第二步：按章提取（核心策略）

**每次只提取一章**，学完再提取下一章：

```bash
python "<skill目录>/scripts/extract_pdf.py" "<PDF路径>" "<输出txt路径>" \
  --start-page <起始页> --end-page <结束页> --tables --images-dir "<图片目录>"
```

- `--start-page` / `--end-page` 根据第一步的目录确定（页码 1 开始）。
- `--tables` 把表格转成 Markdown；`--images-dir` 保存图片并在正文标注 `[图片：xxx]`。
- 如果某章特别长（超过 30 页），拆成 2~3 段提取，每段 10~15 页。

## 读取提取结果

提取出的 txt 里每页有 `--- Page N ---` 标记。用 `read` 的 `offset` / `limit` 参数按页窗口读取，避免一次性把整章塞进上下文：

- 先读该章前 1~2 页，了解本章主题与小节
- 按 `--- Page N ---` 分页，一次读若干页讲解，讲完再读下一段

## 扫描版处理

若提取结果中出现 `[本页无可提取文本，可能为扫描件]`，说明是图片型 PDF。对该章加 `--ocr` 重新提取：

```bash
python "<skill目录>/scripts/extract_pdf.py" "<PDF路径>" "<输出txt路径>" \
  --start-page <起始页> --end-page <结束页> --ocr
```

整本扫描版则对所有章节统一加 `--ocr`（CPU 即可，较慢，提前告知用户）。

## 图表与公式

- 图片：用 `look_at`（或 `multimodal-looker` 代理）读取 `[图片：xxx]` 指向的文件，描述图表含义。
- 表格：已是 Markdown，直接读。
- 公式：若文本提取丢失/乱码，用 `look_at` 定位对应页图片补看；无法复原则向用户说明。

## 章节状态记录

每读完一章，在 `~/.book-tutor/books/<book-slug>/state.md` 中记录：
- 当前章节、页码范围（`--start-page` / `--end-page` 的值）
- 已读页数 / 全书总页数
- 下次继续的章节与起始页
