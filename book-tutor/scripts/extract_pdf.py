#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_pdf.py — book-tutor 本地 PDF 提取器（主路径，纯本地）

能力：
  1. 文本提取（pymupdf，按阅读顺序、每页加 `--- Page N ---` 标记）
  2. 图片提取（保存到本地目录，正文对应位置标注 `[图片：xxx]`）
  3. 表格提取（page.find_tables → Markdown 表格，正文对应位置插入）
  4. 扫描页 OCR 兜底（RapidOCR，需 --ocr；仅对无文本的页启用）
  5. 目录导出（--dump-toc，输出带页码的层级目录，用于建立全书地图）

典型用法：
  # 1) 先导出目录，建立全书地图（章节 -> 起始页码）
  python extract_pdf.py "book.pdf" --dump-toc

  # 2) 按章提取（含表格、图片），一次只提一章，学完再提下一章
  python extract_pdf.py "book.pdf" "ch01.txt" --start-page 1 --end-page 30 --tables --images-dir "ch01_images"

  # 3) 扫描版按章提取（启用 OCR，慢）
  python extract_pdf.py "book.pdf" "ch01.txt" --start-page 1 --end-page 30 --ocr

参数：
  input           输入 PDF 路径
  output          输出文本文件路径（UTF-8；--dump-toc 模式下省略）
  --images-dir    图片保存目录（默认 <output去掉扩展名>_images，传 "none" 则不提取）
  --start-page    起始页码（1 开始，默认 1）
  --end-page      结束页码（默认到末页）
  --tables        启用表格抽取（转为 Markdown 表格）
  --ocr           对无可提取文本的页用 RapidOCR 兜底（慢，仅扫描版需要）
  --dump-toc      只输出目录（层级 + 页码），不提取正文；此时无需 output

依赖：
  pymupdf  （pip install pymupdf）           —— 必需
  rapidocr_onnxruntime（pip install rapidocr-onnxruntime）—— 仅 --ocr 时按需加载
"""
import argparse
import os
import sys


# --------------------------------------------------------------------------- #
# 图片辅助
# --------------------------------------------------------------------------- #

def _get_image_ext(data: bytes) -> str:
    """根据图片二进制头部判断扩展名。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"\x00\x00\x01\x00" or data[:4] == b"\x00\x00\x02\x00":
        return "ico"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"II" or data[:2] == b"MM":
        return "tiff"
    return "png"


def _write_image(img_dir, page_num, img_idx, data: bytes) -> str:
    """保存图片，返回图片文件名。"""
    ext = _get_image_ext(data)
    fname = f"page{page_num}_img{img_idx}.{ext}"
    with open(os.path.join(img_dir, fname), "wb") as f:
        f.write(data)
    return fname


# --------------------------------------------------------------------------- #
# 表格辅助
# --------------------------------------------------------------------------- #

def _table_to_markdown(rows) -> str:
    """把 find_tables 的 extract() 结果（list[list[str|None]]）转成 Markdown 表格。"""
    if not rows:
        return ""
    # 清洗：None -> ""，去掉表头行里可能的换行符
    clean = [[(str(c) if c is not None else "").replace("\n", " ") for c in row]
             for row in rows]
    clean = [row for row in clean if any(row)]  # 去掉全空行
    if not clean:
        return ""
    header = clean[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for row in clean[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _rects_intersect(a, b, tol=2.0) -> bool:
    """判断两个 bbox (x0,y0,x1,y1) 是否相交（带少量容差）。"""
    return not (a[2] <= b[0] + tol or b[2] <= a[0] + tol or
                a[3] <= b[1] + tol or b[3] <= a[1] + tol)


# --------------------------------------------------------------------------- #
# OCR 辅助
# --------------------------------------------------------------------------- #

_ocr_engine = None  # 惰性单例，避免非 OCR 场景的加载开销


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            print("错误：--ocr 需要 rapidocr_onnxruntime。"
                  "请运行: pip install rapidocr-onnxruntime", file=sys.stderr)
            sys.exit(1)
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_page(page, engine) -> str:
    """把页面渲染成图片后用 RapidOCR 识别，返回按阅读顺序拼好的文本。"""
    pix = page.get_pixmap(dpi=200)
    png_bytes = pix.tobytes("png")
    result, _ = engine(png_bytes)
    if not result:
        return ""
    # result 形如 [[box, text, score], ...]，按顶部 y 坐标排序保证阅读顺序
    items = []
    for box, text, score in result:
        top = min(p[1] for p in box)
        items.append((top, text))
    items.sort(key=lambda x: x[0])
    return "\n".join(text for _, text in items)


# --------------------------------------------------------------------------- #
# 目录导出
# --------------------------------------------------------------------------- #

def dump_toc(pdf_path):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 兼容旧版
        except ImportError:
            print("错误：需要安装 pymupdf。请运行: pip install pymupdf", file=sys.stderr)
            sys.exit(1)

    doc = pymupdf.open(pdf_path)
    toc = doc.get_toc()  # list[ [level, title, page] ]
    total = doc.page_count
    doc.close()

    print(f"# 目录：{pdf_path}")
    print(f"# 总页数：{total}")
    if not toc:
        print("# （本书无内嵌目录，需靠正文标题推断章节）")
        return 0

    for level, title, page in toc:
        indent = "  " * (level - 1)
        print(f"{indent}- L{level} [第 {page} 页] {title}")
    return 0


# --------------------------------------------------------------------------- #
# 正文提取
# --------------------------------------------------------------------------- #

def extract(pdf_path, txt_path, images_dir=None, start_page=1, end_page=None,
            extract_tables=False, use_ocr=False):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 兼容旧版
        except ImportError:
            print("错误：需要安装 pymupdf。请运行: pip install pymupdf", file=sys.stderr)
            sys.exit(1)

    # 默认图片目录：output.txt 去掉扩展名 + _images
    if images_dir is None:
        base = os.path.splitext(txt_path)[0]
        images_dir = base + "_images"

    extract_images = images_dir.lower() != "none"
    if extract_images:
        os.makedirs(images_dir, exist_ok=True)

    ocr_engine = _get_ocr_engine() if use_ocr else None

    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    if end_page is None or end_page > total:
        end_page = total
    start_page = max(1, start_page)
    if start_page > end_page:
        print(f"错误：start_page({start_page}) 大于 end_page({end_page})", file=sys.stderr)
        sys.exit(1)

    unique_images = 0

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# 提取自: {pdf_path}\n")
        f.write(f"# 总页数: {total}\n")
        f.write(f"# 提取范围: 第 {start_page} - {end_page} 页\n")
        flags = []
        if extract_tables:
            flags.append("表格")
        if use_ocr:
            flags.append("OCR")
        if extract_images:
            flags.append("图片")
        if flags:
            f.write(f"# 启用能力: {'、'.join(flags)}\n")
        if extract_images:
            f.write(f"# 图片目录: {images_dir}\n")
        f.write("\n")

        for page_num in range(start_page, end_page + 1):
            page = doc.load_page(page_num - 1)  # 0-based
            f.write(f"\n--- Page {page_num} ---\n")

            # 1) 收集本页表格（含 bbox），供文本块去重与按位插入
            table_items = []
            if extract_tables:
                try:
                    finder = page.find_tables()
                    for t in finder.tables:
                        md = _table_to_markdown(t.extract())
                        if md:
                            table_items.append((tuple(t.bbox), md))
                except Exception:
                    pass  # 表格检测失败不影响文本提取

            # 2) 收集文本块与图片块，按阅读顺序（先 y 再 x）
            content_items = []  # (sort_key, kind, payload)
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                bbox = block.get("bbox", (0, 0, 0, 0))
                btype = block.get("type")
                if btype == 0:  # 文本块
                    # 落在表格 bbox 内的文本块跳过，避免与 Markdown 表格重复
                    if extract_tables and any(
                            _rects_intersect(bbox, tb) for tb, _ in table_items):
                        continue
                    text = ""
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text += span.get("text", "")
                        text += "\n"
                    if text.strip():
                        content_items.append((bbox[1], bbox[0], 0, "text", text))
                elif btype == 1 and extract_images:  # 图片块
                    image_bytes = block.get("image", None)
                    if image_bytes:
                        content_items.append((bbox[1], bbox[0], 0, "image", image_bytes))

            # 3) 把表格也并入，按位置排序后写出
            for tb, md in table_items:
                content_items.append((tb[1], tb[0], 1, "table", md))

            content_items.sort(key=lambda x: (x[0], x[1], x[2]))

            img_idx = 0
            has_content = False
            for _, _, _, kind, payload in content_items:
                if kind == "text":
                    f.write(payload)
                    has_content = True
                elif kind == "image":
                    img_idx += 1
                    fname = _write_image(images_dir, page_num, img_idx, payload)
                    f.write(f"[图片：{fname}]\n")
                    has_content = True
                elif kind == "table":
                    f.write("\n[表格]\n")
                    f.write(payload)
                    f.write("\n[/表格]\n")
                    has_content = True

            if not has_content:
                if use_ocr:
                    f.write("[本页为扫描件，OCR 识别结果如下]\n")
                    ocr_text = _ocr_page(page, ocr_engine)
                    if ocr_text:
                        f.write(ocr_text + "\n")
                    else:
                        f.write("[OCR 未识别出内容]\n")
                else:
                    f.write("[本页无可提取文本，可能为扫描件；如需识别请加 --ocr]\n")

    doc.close()

    # 统计唯一图片数量
    if extract_images:
        unique_images = len([n for n in os.listdir(images_dir)
                             if os.path.isfile(os.path.join(images_dir, n))])

    print(f"成功: 从第 {start_page} 页到第 {end_page} 页（共 {total} 页），"
          f"已提取到 {txt_path}")
    if extract_images:
        print(f"图片保存到: {images_dir}")
        print(f"共提取 {unique_images} 张唯一图片")
    if extract_tables:
        print("表格已转为 Markdown（正文中以 [表格] ... [/表格] 标记）")
    if use_ocr:
        print("已对扫描页启用 RapidOCR 识别")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="book-tutor 本地 PDF 提取器（文本/图片/表格/OCR/目录）")
    parser.add_argument("input", help="输入 PDF 路径")
    parser.add_argument("output", nargs="?", default=None,
                        help="输出 txt 路径（--dump-toc 模式下省略）")
    parser.add_argument("--images-dir", type=str, default=None,
                        help="图片保存目录（默认 <output>_images，传 none 则不提取）")
    parser.add_argument("--start-page", type=int, default=1, help="起始页码（默认 1）")
    parser.add_argument("--end-page", type=int, default=None, help="结束页码（默认到末页）")
    parser.add_argument("--tables", action="store_true", help="抽取表格为 Markdown")
    parser.add_argument("--ocr", action="store_true", help="扫描页用 RapidOCR 兜底")
    parser.add_argument("--dump-toc", action="store_true", help="只导出目录并退出")
    args = parser.parse_args()

    if args.dump_toc:
        sys.exit(dump_toc(args.input))

    if not args.output:
        print("错误：未提供 output；或使用 --dump-toc 只导出目录", file=sys.stderr)
        sys.exit(1)

    sys.exit(extract(args.input, args.output, args.images_dir,
                     args.start_page, args.end_page, args.tables, args.ocr))


if __name__ == "__main__":
    main()
