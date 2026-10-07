"""文档抽取：把供应商发来的东西变成"文本 + 图片"（2026-10-04）。

供应商发来的可能是：Excel、PDF（有文字层的 / 扫描件）、拍照/截图（jpg/png）、
或者干脆一段微信文字。这一层只做**格式搬运**，不做任何"理解"——
理解交给模型，但**校验必须回到这里抽出的原文**（逐字核对，见 ai_quote_parse）。

## 几个刻意的取舍

  · **xlsx 直接把行渲染成带行号的 TSV**：模型因此能准确报"第几行"，
    而我们也能用行号校验它没瞎编。
  · **PDF 有文字层就抽文字**（Excel 导出的 PDF 都是这种，最准也最便宜）；
    文字太少（扫描件）才把内嵌图片取出来走多模态。
  · **限制规模**：文本 4 万字符、图片 6 张、PDF 20 页、表格 500 行。
    供应商报价单通常几页到几十行，超出这个量说明该人工拆了，
    硬塞给模型只会更慢更贵更不准。
"""

from __future__ import annotations

import csv
import io
import pathlib
from dataclasses import dataclass, field

from openpyxl import load_workbook

from app.core.logging import get_logger

log = get_logger("services.ai_extract")

MAX_TEXT_CHARS = 40_000
MAX_IMAGES = 6
MAX_PDF_PAGES = 20
MAX_XLSX_ROWS = 500
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
TEXT_EXT = {".txt", ".csv", ".md", ".json"}
XLSX_EXT = {".xlsx", ".xlsm"}
MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}


class ExtractError(RuntimeError):
    """文件读不了（损坏/加密/格式不认识）。接口层转成 422 给用户看。

    以前这类错误会冒到全局处理器变成 `internal_error` + 500 ——
    用户看到的是"服务器内部错误"，既不知道换文件也不知道找谁。
    """


@dataclass
class ExtractResult:
    """抽取结果。`text` 是给模型看的、也是校验时的"原文唯一来源"。"""

    engine: str
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    images: list[tuple[bytes, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 供前端展示：文件里到底读到了什么（截断）
    preview: str = ""


def sniff_kind(filename: str, mime: str | None, data: bytes) -> str:
    """判断文件类型：扩展名优先，其次 MIME，最后看魔术字节。"""
    ext = pathlib.Path(filename or "").suffix.lower()
    if ext in XLSX_EXT:
        return "xlsx"
    if ext == ".pdf":
        return "pdf"
    if ext in IMG_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    if data[:2] == b"PK":
        return "xlsx"
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:8].startswith(b"\x89PNG") or data[:3] == b"\xff\xd8\xff":
        return "image"
    m = (mime or "").lower()
    if "spreadsheet" in m or "excel" in m:
        return "xlsx"
    if "pdf" in m:
        return "pdf"
    if m.startswith("image/"):
        return "image"
    return "text"


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def extract_xlsx(data: bytes) -> ExtractResult:
    """读所有工作表 → 行 + 带行号的 TSV。

    合并单元格要**向下填充**：供应商表头常是两级（"合计"跨两列），不填充的话
    第一列是空的，模型只能猜。
    """
    res = ExtractResult(engine="xlsx")
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    try:
        lines: list[str] = []
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                rows.append([_cell(c) for c in row])
                if len(rows) >= MAX_XLSX_ROWS:
                    res.warnings.append(
                        f"工作表「{ws.title}」超过 {MAX_XLSX_ROWS} 行，只取了前 {MAX_XLSX_ROWS} 行"
                    )
                    break

            # 合并单元格向下/向右填充
            for rng in ws.merged_cells.ranges:
                r0, c0 = rng.min_row - 1, rng.min_col - 1
                if r0 >= len(rows) or c0 >= len(rows[r0]):
                    continue
                value = rows[r0][c0]
                if not value:
                    continue
                for rr in range(rng.min_row - 1, min(rng.max_row, len(rows))):
                    for cc in range(rng.min_col - 1, rng.max_col):
                        if rr < len(rows) and cc < len(rows[rr]) and not rows[rr][cc]:
                            rows[rr][cc] = value

            if not any(any(c for c in r) for r in rows):
                continue
            lines.append(f"### 工作表: {ws.title}")
            for idx, r in enumerate(rows, start=1):
                lines.append(f"{idx}\t" + "\t".join(r))
            res.rows.extend(rows)
        res.text = "\n".join(lines)
        res.preview = res.text[:2000]
    finally:
        wb.close()
    return res


def extract_pdf(data: bytes) -> ExtractResult:
    """PDF：先文字层；文字太少（扫描件）再取内嵌图片走多模态。"""
    from pypdf import PdfReader

    res = ExtractResult(engine="pdf")
    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    if pages > MAX_PDF_PAGES:
        res.warnings.append(f"PDF 有 {pages} 页，只读了前 {MAX_PDF_PAGES} 页")

    chunks: list[str] = []
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        try:
            t = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 个别页坏了不该整份失败
            t = ""
            res.warnings.append(f"第 {i} 页文字抽取失败（{type(exc).__name__}），已跳过")
        if t.strip():
            chunks.append(f"### 第 {i} 页\n{t.strip()}")

    text = "\n".join(chunks)
    if len(text.strip()) >= 80:
        res.engine = "pdf-text"
        res.text = text
        res.preview = text[:2000]
        return res

    # 扫描件：抽内嵌图片
    res.engine = "pdf-image"
    res.warnings.append("PDF 里几乎没有文字层（可能是扫描件），改用图片识别")
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        if len(res.images) >= MAX_IMAGES:
            break
        try:
            for img in page.images:
                if len(res.images) >= MAX_IMAGES:
                    break
                name = (getattr(img, "name", "") or "").lower()
                ext = pathlib.Path(name).suffix.lower()
                res.images.append((img.data, MIME_BY_EXT.get(ext, "image/png")))
        except Exception as exc:  # noqa: BLE001
            res.warnings.append(f"第 {i} 页取图失败（{type(exc).__name__}）")
    if not res.images:
        res.warnings.append("扫描件里也没能取出图片，请把关键页截图后按图片上传")
    res.text = text  # 可能为空
    return res


def extract_text_file(data: bytes, filename: str = "") -> ExtractResult:
    res = ExtractResult(engine="text")
    raw = data.decode("utf-8", "replace")
    if pathlib.Path(filename or "").suffix.lower() == ".csv":
        rows = [r for r in csv.reader(io.StringIO(raw)) if any(x.strip() for x in r)]
        res.rows = [[_cell(c) for c in r] for r in rows]
        res.text = "\n".join(
            f"{i}\t" + "\t".join(r) for i, r in enumerate(res.rows, start=1)
        )
    else:
        res.text = raw
    res.preview = res.text[:2000]
    return res


def extract_image(data: bytes, mime: str | None) -> ExtractResult:
    res = ExtractResult(engine="image")
    res.images = [(data, mime or "image/png")]
    res.warnings.append("图片按多模态识别；看不清的数字请人工核对")
    return res


def extract_document(
    *, filename: str, data: bytes, mime: str | None = None
) -> ExtractResult:
    """统一入口：按类型分派 + 统一截断。

    **任何解析异常都转成 ExtractError**（→ 接口返回 422）：
    用户需要看到的是"这份文件读不了，换一份/截图重发"，
    而不是一个 500 加一句"服务器内部错误"。
    """
    if not data:
        raise ExtractError("文件是空的（0 字节）")
    kind = sniff_kind(filename, mime, data)
    try:
        if kind == "xlsx":
            res = extract_xlsx(data)
        elif kind == "pdf":
            res = extract_pdf(data)
        elif kind == "image":
            res = extract_image(data, mime or MIME_BY_EXT.get(
                pathlib.Path(filename or "").suffix.lower()
            ))
        else:
            res = extract_text_file(data, filename)
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning(f"抽取失败：{filename}（{type(exc).__name__}: {exc}）")
        raise ExtractError(
            f"这份文件读不了（{type(exc).__name__}）。"
            "可能是文件损坏、加密或有密码保护；"
            "也可以把关键页截图后按图片再传一次。"
        ) from exc

    if len(res.text) > MAX_TEXT_CHARS:
        res.text = res.text[:MAX_TEXT_CHARS]
        res.warnings.append(
            f"内容过长，只送前 {MAX_TEXT_CHARS} 个字符给模型（超出部分请人工核对）"
        )
    log.info(
        f"抽取完成：{filename} → {res.engine}，文本 {len(res.text)} 字，"
        f"表格 {len(res.rows)} 行，图片 {len(res.images)} 张"
    )
    return res
