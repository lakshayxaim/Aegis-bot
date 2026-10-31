"""Documents and data: read anything, convert, create, query tables, chart, summarise, OCR, vision."""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from aegis import checks, paths
from aegis.toolkit import WRITE, Approval, Result, ToolError, arg, resolve_input, resolve_output, tool
from aegis.tools.desktop import capture

logger = logging.getLogger("aegis.docs")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW
_OCR_SCRIPT = Path(__file__).with_name("windows_ocr.ps1")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
MEDIA_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
BINARY_EXTS = {".exe", ".dll", ".zip", ".rar", ".7z", ".iso", ".msi", ".bin", ".dat", ".db", ".sqlite"}


def ocr_image(path: Path) -> str:
    """Text in an image, using the OCR engine built into Windows."""
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(_OCR_SCRIPT), str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=_NO_WINDOW)
    if done.returncode:
        raise ToolError(f"Windows OCR failed: {done.stderr.strip()[:200]}", "Windows may need an OCR language pack.")
    return done.stdout.strip()


def _read_pdf(path: Path) -> tuple[str, str]:
    import fitz

    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ToolError(f"{path.name} is password-protected.", "Ask the user to remove the password.")
        pages = [page.get_text("text") for page in pdf]
        count = pdf.page_count
        text = "\n\n".join(f"[Page {index + 1}]\n{body.strip()}" for index, body in enumerate(pages) if body.strip())
        if len(text) < 40 * max(1, count) / 4:  # scanned: no text layer
            recovered = []
            for index, page in enumerate(pdf):
                if index >= 15:
                    break
                image = paths.temp_file(".png")
                page.get_pixmap(dpi=170).save(image)
                recovered.append(f"[Page {index + 1}]\n{ocr_image(image)}")
                image.unlink(missing_ok=True)
            return "\n\n".join(recovered), f"{count} pages (scanned, read with OCR)"
    return text, f"{count} pages"


def _read_docx(path: Path) -> tuple[str, str]:
    import docx

    document = docx.Document(str(path))
    parts: list[str] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = docx.text.paragraph.Paragraph(child, document)
            if paragraph.text.strip():
                parts.append(paragraph.text)
        elif tag == "tbl":
            table = docx.table.Table(child, document)
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts), f"{len(parts)} paragraphs"


def _read_pptx(path: Path) -> tuple[str, str]:
    namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
    with zipfile.ZipFile(path) as archive:
        slides = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda name: int(re.search(r"(\d+)\.xml", name).group(1)))
        parts = []
        for index, name in enumerate(slides, 1):
            words = [node.text for node in ElementTree.fromstring(archive.read(name)).iter(namespace) if node.text]
            parts.append(f"[Slide {index}]\n" + " ".join(words))
    return "\n\n".join(parts), f"{len(slides)} slides"


def _read_xlsx(path: Path) -> tuple[str, str]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for sheet in workbook.worksheets[:6]:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None for cell in row):
                rows.append(",".join("" if cell is None else str(cell) for cell in row))
            if len(rows) >= 300:
                rows.append("[more rows not shown; use query_table]")
                break
        parts.append(f"[Sheet {sheet.title}]\n" + "\n".join(rows))
    names = len(workbook.worksheets)
    workbook.close()
    return "\n\n".join(parts), f"{names} sheet(s)"


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    if "\x00" in text[:2000]:
        raise ToolError(f"{path.name} looks like a binary file, not text.")
    return text, f"{text.count(chr(10)) + 1} lines"


def extract_text(path: Path) -> tuple[str, str]:
    """(text, description) for any readable file type."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pptx":
        return _read_pptx(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _read_xlsx(path)
    if suffix in IMAGE_EXTS:
        text = ocr_image(path)
        return text or "(no text found in the image)", "image, read with OCR"
    if suffix in MEDIA_EXTS:
        raise ToolError(f"{path.name} is audio or video.", "Use transcribe_audio for speech or media_info for details.")
    if suffix in BINARY_EXTS:
        raise ToolError(f"{path.name} is a binary file, so it cannot be read as text.")
    return _read_text(path)


def _gate_read(ctx, path: str = "", start: int = 0) -> Approval | None:
    target = paths.expand(path)
    if paths.is_sensitive(target):
        return Approval(f"Read {target.name}?", f"{target} looks like it holds passwords or keys. Its contents would be shown to the local model.")
    return None


@tool("read_file", "Read a file's text: txt, md, code, csv, pdf, docx, xlsx, pptx, or an image with OCR. Use this before answering questions about a file.", group="Documents", gate=_gate_read, doing="Reading a file",
      keywords="read open look content contents say says inside text pdf docx word doc note page what",
      path=arg("string", "The file", required=True), start=arg("integer", "Character offset to continue from when a file is long"))
def read_file(ctx, path: str, start: int = 0) -> Result:
    target = resolve_input(path)
    if target.is_dir():
        raise ToolError(f"{target} is a folder.", "Use list_dir instead.")
    text, description = extract_text(target)
    limit = max(1500, min(int(ctx.settings["num_ctx"]) * 2, 16000)) if ctx.settings else 6000
    chunk = text[start:start + limit]
    remaining = len(text) - start - len(chunk)
    note = f"\n[{remaining} more characters; call read_file with start={start + len(chunk)} to continue]" if remaining > 0 else ""
    return Result(f"Read {target.name} ({description}, {len(text)} characters). Full path: {target}", chunk + note)


# ---------------------------------------------------------------------- summaries
@tool("summarize_file", "Summarise a long document of any length. Prefer this to read_file when the user wants a summary.", group="Documents", doing="Summarising", direct=True,
      keywords="summarize summarise summary tldr key points digest brief overview",
      path=arg("string", "The file", required=True), focus=arg("string", "What to concentrate on, if anything"))
def summarize_file(ctx, path: str, focus: str = "") -> Result:
    target = resolve_input(path)
    text, description = extract_text(target)
    if not text.strip():
        raise ToolError(f"{target.name} has no readable text.")
    aim = f" Focus on: {focus}." if focus else ""
    budget = int(ctx.settings["num_ctx"] * 2.2)
    if len(text) <= budget:
        summary = ctx.llm.complete(f"Summarise this document in 4-7 concise bullet points, keeping names, numbers and dates exact.{aim}\n\n{text}", max_tokens=450)
    else:
        chunks = [text[start:start + budget] for start in range(0, len(text), budget)]
        chunks = chunks[:12]
        notes = []
        for index, chunk in enumerate(chunks, 1):
            ctx.check_cancelled()
            ctx.progress(f"Reading part {index} of {len(chunks)}")
            notes.append(ctx.llm.complete(f"Note the key facts from this part of a document in up to 5 short bullets, keeping names, numbers and dates exact.{aim}\n\n{chunk}", max_tokens=220))
        ctx.progress("Writing the summary")
        summary = ctx.llm.complete(f"Combine these notes into one summary of 5-8 concise bullet points, removing repetition.{aim}\n\n" + "\n\n".join(notes), max_tokens=500)
    covered = "" if len(text) <= budget * 12 else f" Only the first {budget * 12} of {len(text)} characters were covered."
    return Result(f"Summary of {target.name} ({description}).{covered}", summary.strip())


# ---------------------------------------------------------------------- creating documents
def _markdown_to_docx(content: str, target: Path, title: str) -> int:
    import docx

    document = docx.Document()
    if title:
        document.add_heading(title, 0)
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading:
            document.add_heading(_plain(heading.group(2)), min(len(heading.group(1)), 4))
        elif stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[index + 1]):
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            table = document.add_table(rows=1 + len(rows), cols=len(header))
            table.style = "Light Grid Accent 1"
            for column, value in enumerate(header):
                table.rows[0].cells[column].text = _plain(value)
            for row_index, row in enumerate(rows, 1):
                for column, value in enumerate(row[:len(header)]):
                    table.rows[row_index].cells[column].text = _plain(value)
            continue
        elif re.match(r"^[-*+]\s+", stripped):
            _add_runs(document.add_paragraph(style="List Bullet"), re.sub(r"^[-*+]\s+", "", stripped))
        elif re.match(r"^\d+[.)]\s+", stripped):
            _add_runs(document.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", stripped))
        else:
            _add_runs(document.add_paragraph(), stripped)
        index += 1
    document.save(str(target))
    return len(document.paragraphs)


def _plain(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|`(.+?)`", lambda match: next(group for group in match.groups() if group), text)


def _add_runs(paragraph: Any, text: str) -> None:
    for part in re.split(r"(\*\*.+?\*\*)", text):
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part:
            paragraph.add_run(_plain(part))


_PDF_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.45; color: #1a1a1a; }
h1 { font-size: 20pt; margin-bottom: 8pt; } h2 { font-size: 15pt; margin-top: 14pt; } h3 { font-size: 12.5pt; }
table { border-collapse: collapse; } td, th { border: 1px solid #999; padding: 4pt; }
code { font-family: monospace; }
"""


def _markdown_to_pdf(content: str, target: Path, title: str) -> int:
    import fitz
    import markdown

    html = markdown.markdown(content, extensions=["tables", "fenced_code", "sane_lists"])
    if title:
        html = f"<h1>{title}</h1>{html}"
    story = fitz.Story(html, user_css=_PDF_CSS)
    writer = fitz.DocumentWriter(str(target))
    mediabox = fitz.paper_rect("a4")
    area = mediabox + (54, 54, -54, -54)
    pages, more = 0, 1
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(area)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages > 500:
            break
    writer.close()
    return pages


# Small models sometimes save a promise of content instead of content.
_PLACEHOLDER = re.compile(r"\[[^\]\n]*\b(?:will be|to be|goes here|go here|placeholder|insert)\b[^\]\n]*\]", re.IGNORECASE)


@tool("create_document", "Write a formatted document to a file. Supports docx, pdf, html, md and txt. Content is Markdown.", risk=WRITE, terminal=True, group="Documents", doing="Creating a document",
      keywords="create write make draft document report letter essay word docx pdf save resume memo",
      path=arg("string", "Where, including the extension, e.g. desktop/report.docx", required=True), content=arg("string", "The full text as Markdown", required=True), title=arg("string", "Optional title"))
def create_document(ctx, path: str, content: str, title: str = "") -> Result:
    if _PLACEHOLDER.search(content):
        raise ToolError("That text contains a placeholder, not real content, so nothing was saved.", "Write the real content. If you don't have it, tell the user plainly instead of saving a file.")
    if checks.looks_like_filler(content, ctx.request):
        raise ToolError("That text describes a document instead of being one, so nothing was saved.", "Get the real content first (research reads the web), then save it.")
    target = resolve_output(path, default_name="Document.docx")
    suffix = target.suffix.lower()
    if suffix == ".docx":
        import docx

        _markdown_to_docx(content, target, title)
        words = sum(len(paragraph.text.split()) for paragraph in docx.Document(str(target)).paragraphs)
        check = f"Reopened the file: {words} words."
    elif suffix == ".pdf":
        import fitz

        _markdown_to_pdf(content, target, title)
        with fitz.open(target) as pdf:
            check = f"Reopened the file: {pdf.page_count} page(s)."
    elif suffix in {".html", ".htm"}:
        import markdown

        target.write_text(f"<!doctype html><meta charset='utf-8'><title>{title}</title><style>{_PDF_CSS}</style>{markdown.markdown(content, extensions=['tables', 'fenced_code'])}", encoding="utf-8")
        check = "File written."
    else:
        target.write_text((f"# {title}\n\n" if title else "") + content, encoding="utf-8")
        check = f"Read back {target.stat().st_size} bytes."
    return Result(f"Created {paths.pretty(target)}.", files=[target], verified=check, undo={"kind": "delete_created", "path": str(target)})


# ---------------------------------------------------------------------- conversion
def _office_to_pdf(source: Path, target: Path, ctx) -> None:
    suffix = source.suffix.lower()
    try:
        import pythoncom
        from win32com import client
    except ImportError:
        client = None
    if client is not None:
        pythoncom.CoInitialize()
        try:
            if suffix in {".doc", ".docx", ".rtf", ".odt", ".txt"}:
                app = client.DispatchEx("Word.Application")
                app.Visible = False
                app.DisplayAlerts = 0
                document = None
                try:
                    document = app.Documents.Open(str(source), ReadOnly=True, ConfirmConversions=False, AddToRecentFiles=False)
                    document.ExportAsFixedFormat(str(target), 17)
                finally:
                    if document is not None:
                        document.Close(False)
                    app.Quit()
                return
            if suffix in {".ppt", ".pptx"}:
                app = client.DispatchEx("PowerPoint.Application")
                deck = None
                try:
                    deck = app.Presentations.Open(str(source), True, False, False)
                    deck.SaveAs(str(target), 32)
                finally:
                    if deck is not None:
                        deck.Close()
                    app.Quit()
                return
            if suffix in {".xls", ".xlsx"}:
                app = client.DispatchEx("Excel.Application")
                app.Visible = False
                app.DisplayAlerts = False
                book = None
                try:
                    book = app.Workbooks.Open(str(source), ReadOnly=True)
                    book.ExportAsFixedFormat(0, str(target))
                finally:
                    if book is not None:
                        book.Close(False)
                    app.Quit()
                return
        except Exception as exc:  # no Office, or Office failed: try LibreOffice below
            logger.warning("Office conversion failed: %s", exc)
            if not shutil.which("soffice"):
                raise ToolError(f"Microsoft Office could not convert {source.name}: {str(exc)[:200]}") from exc
        finally:
            pythoncom.CoUninitialize()
    soffice = shutil.which("soffice") or (r"C:\Program Files\LibreOffice\program\soffice.exe" if Path(r"C:\Program Files\LibreOffice\program\soffice.exe").exists() else None)
    if not soffice:
        raise ToolError("Converting to PDF needs Microsoft Office or LibreOffice, and neither was found.")
    outdir = paths.temp_file(".dir")
    outdir.unlink()
    outdir.mkdir()
    done = subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(source)], capture_output=True, text=True, timeout=180, creationflags=_NO_WINDOW)
    made = outdir / f"{source.stem}.pdf"
    if done.returncode or not made.exists():
        raise ToolError(f"LibreOffice could not convert {source.name}: {done.stderr.strip()[:200]}")
    shutil.move(str(made), str(target))


@tool("convert_document", "Convert a file to another format: Word/PowerPoint/Excel to PDF, PDF to text or images, CSV to Excel and back, images between formats, Markdown to docx or PDF.", risk=WRITE, terminal=True, group="Documents", doing="Converting",
      keywords="convert export save as turn into change format pdf docx word xlsx csv png jpg image text",
      path=arg("string", "The source file", required=True), to=arg("string", "Target format, e.g. pdf, docx, txt, png, jpg, xlsx, csv", required=True), destination=arg("string", "Where to save; defaults to next to the source"))
def convert_document(ctx, path: str, to: str, destination: str = "") -> Result:
    source = resolve_input(path)
    fmt = to.strip().lower().lstrip(".")
    suffix = source.suffix.lower()
    if suffix == f".{fmt}":
        return Result(f"{source.name} is already a {fmt} file.")
    target = resolve_output(destination or str(source.with_suffix(f".{fmt}")), default_name=f"{source.stem}.{fmt}", beside=source)
    verified = ""
    if fmt == "pdf" and suffix in {".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".xls", ".xlsx"}:
        ctx.progress("Converting with Office")
        _office_to_pdf(source, target, ctx)
    elif fmt == "pdf" and suffix in IMAGE_EXTS:
        from PIL import Image

        Image.open(source).convert("RGB").save(target, "PDF")
    elif fmt == "pdf" and suffix in {".md", ".markdown", ".txt"}:
        _markdown_to_pdf(extract_text(source)[0], target, "")
    elif fmt == "docx" and suffix in {".md", ".markdown", ".txt"}:
        _markdown_to_docx(extract_text(source)[0], target, "")
    elif fmt == "docx" and suffix == ".pdf":
        _markdown_to_docx(extract_text(source)[0], target, "")
        verified = "Text only: PDF layout and images are not carried over."
    elif fmt in {"txt", "md"}:
        target.write_text(extract_text(source)[0], encoding="utf-8")
    elif fmt in {"xlsx", "csv", "tsv", "json", "parquet"} and suffix in {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}:
        frame = _load_table(source)
        if fmt == "xlsx":
            _write_xlsx(frame, target)
        elif fmt == "csv":
            frame.write_csv(target)
        elif fmt == "tsv":
            frame.write_csv(target, separator="\t")
        elif fmt == "json":
            frame.write_json(target)
        else:
            frame.write_parquet(target)
    elif fmt in {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff"} and suffix in IMAGE_EXTS:
        from PIL import Image

        image = Image.open(source)
        (image.convert("RGB") if fmt in {"jpg", "jpeg", "bmp"} else image).save(target)
    elif fmt in {"png", "jpg", "jpeg"} and suffix == ".pdf":
        import fitz

        with fitz.open(source) as pdf:
            made = []
            for index, page in enumerate(pdf):
                if index >= 50:
                    break
                image_path = target.with_name(f"{source.stem} page {index + 1}.{fmt}")
                page.get_pixmap(dpi=170).save(image_path)
                made.append(image_path)
        return Result(f"Saved {len(made)} page image(s) next to {source.name}.", files=made, verified="Each page image exists on disk.")
    else:
        raise ToolError(f"Converting {suffix or 'this file'} to {fmt} is not supported.", "For media use convert_media. Otherwise run_python may be able to do it.")
    if not target.exists() or target.stat().st_size == 0:
        raise ToolError(f"The conversion produced no output for {source.name}.")
    if not verified and fmt == "pdf":
        import fitz

        with fitz.open(target) as pdf:
            verified = f"Reopened the PDF: {pdf.page_count} page(s)."
    return Result(f"Converted {source.name} to {paths.pretty(target)}.", files=[target], verified=verified or f"Output is {target.stat().st_size // 1024 + 1} KB.", undo={"kind": "delete_created", "path": str(target)})


# ---------------------------------------------------------------------- tables
def _load_table(path: Path):
    import polars as pl

    suffix = path.suffix.lower()
    try:
        if suffix in {".csv", ".txt"}:
            return pl.read_csv(path, infer_schema_length=2000, ignore_errors=True, truncate_ragged_lines=True)
        if suffix == ".tsv":
            return pl.read_csv(path, separator="\t", infer_schema_length=2000, ignore_errors=True, truncate_ragged_lines=True)
        if suffix in {".xlsx", ".xls", ".xlsm"}:
            return pl.read_excel(path, engine="openpyxl")
        if suffix == ".json":
            return pl.read_json(path)
        if suffix == ".parquet":
            return pl.read_parquet(path)
    except Exception as exc:
        raise ToolError(f"Could not read {path.name} as a table: {str(exc)[:200]}") from exc
    raise ToolError(f"{path.name} is not a table format this tool reads.", "Supported: csv, tsv, xlsx, json, parquet.")


def _write_xlsx(frame, target: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet("Sheet1")
    sheet.append(frame.columns)
    for row in frame.iter_rows():
        sheet.append(list(row))
    book.save(target)


def _table_text(frame, rows: int = 40) -> str:
    head = frame.head(rows)
    lines = [",".join(head.columns)]
    for row in head.iter_rows():
        lines.append(",".join("" if value is None else (f"{value:.4g}" if isinstance(value, float) else str(value)) for value in row))
    if frame.height > rows:
        lines.append(f"[{frame.height - rows} more rows]")
    return "\n".join(lines)


def _fix_sql(sql: str, columns: list[str], stem: str) -> str:
    """Forgive a wrong table name or column casing."""
    lookup = {column.lower(): column for column in columns}

    def fix_identifier(match: re.Match) -> str:
        word = match.group(0)
        real = lookup.get(word.lower())
        if real is None or real == word:
            return word
        return f'"{real}"' if re.search(r"\W", real) else real

    sql = re.sub(r"(?i)\b(from|join)\s+(\"[^\"]+\"|'[^']+'|`[^`]+`|[\w.-]+)", lambda match: f"{match.group(1)} t", sql)
    return re.sub(r'(?<!["\w])[A-Za-z_]\w*(?!["\w])', fix_identifier, sql).replace(";", "")


@tool("query_table", "Answer questions about a CSV or Excel file. Without sql it shows columns, stats and first rows. With sql it runs a query on the table named t.", group="Data", doing="Analysing a table",
      keywords="csv excel xlsx spreadsheet table data rows columns highest lowest total average sum count sales sql query which most analyse analyze statistics",
      path=arg("string", "CSV or Excel file", required=True), sql=arg("string", "SQL over table t, e.g. SELECT month FROM t ORDER BY sales DESC LIMIT 1"))
def query_table(ctx, path: str, sql: str = "") -> Result:
    import polars as pl

    source = resolve_input(path, kinds=(".csv", ".xlsx", ".xls", ".tsv"))
    frame = _load_table(source)
    if not sql:
        numeric = [column for column, kind in frame.schema.items() if kind.is_numeric()]
        stats = []
        for column in numeric[:12]:
            series = frame[column]
            stats.append(f"{column}: min {series.min()}, max {series.max()}, mean {series.mean():.4g}, sum {series.sum()}")
        schema = ", ".join(f"{name} ({kind})" for name, kind in frame.schema.items())
        body = f"Columns: {schema}\n" + ("Stats:\n" + "\n".join(stats) + "\n" if stats else "") + "Rows:\n" + _table_text(frame, 30)
        return Result(f"{source.name} has {frame.height} rows and {frame.width} columns.", body)
    query = _fix_sql(sql, frame.columns, source.stem)
    try:
        with pl.SQLContext(t=frame, eager=True) as context:
            result = context.execute(query)
    except Exception as exc:
        raise ToolError(f"The query failed: {str(exc).splitlines()[0][:200]}. Columns are: {', '.join(frame.columns)}. The table is named t.", "Fix the SQL and try again.") from exc
    return Result(f"The query returned {result.height} row(s).", _table_text(result, 60))


@tool("make_chart", "Draw a chart and save it as a PNG. Give a CSV/Excel file with x and y columns, or labels and values directly.", risk=WRITE, terminal=True, group="Data", doing="Drawing a chart",
      keywords="chart graph plot bar line pie scatter histogram visualize visualise draw diagram figure",
      kind=arg("string", "Chart type", enum=["bar", "line", "pie", "scatter", "hist"]),
      file=arg("string", "CSV or Excel file with the data"), x=arg("string", "Column for the horizontal axis or labels"), y=arg("string", "Column for the values"),
      labels=arg("array", "Labels, when not using a file"), values=arg("array", "Numbers, when not using a file", items="number"),
      title=arg("string", "Chart title"), path=arg("string", "Where to save the PNG"))
def make_chart(ctx, kind: str = "bar", file: str = "", x: str = "", y: str = "", labels: list | None = None, values: list | None = None, title: str = "", path: str = "") -> Result:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from PIL import Image

    if file:
        source = resolve_input(file, kinds=(".csv", ".xlsx", ".xls", ".tsv"))
        frame = _load_table(source)
        columns = {column.lower(): column for column in frame.columns}
        numeric = [column for column, data_type in frame.schema.items() if data_type.is_numeric()]
        others = [column for column in frame.columns if column not in numeric]
        y_column = columns.get(y.lower()) if y else (numeric[0] if numeric else None)
        x_column = columns.get(x.lower()) if x else (others[0] if others else None)
        if y_column is None:
            raise ToolError(f"No column named '{y}' to plot. Columns are: {', '.join(frame.columns)}." if y else "That table has no numeric column to plot.")
        if x and x_column is None:
            raise ToolError(f"No column named '{x}'. Columns are: {', '.join(frame.columns)}.")
        numbers = [float(value) if value is not None else 0.0 for value in frame[y_column].to_list()]
        names = [str(value) for value in frame[x_column].to_list()] if x_column else [str(index + 1) for index in range(len(numbers))]
        xlabel, ylabel = x_column or "", y_column
    else:
        if not values:
            raise ToolError("Give a file, or labels and values.", "Never invent data for a chart.")
        try:
            numbers = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise ToolError("Every value must be a number.") from exc
        names = [str(label) for label in (labels or [])] or [str(index + 1) for index in range(len(numbers))]
        if len(names) != len(numbers):
            raise ToolError(f"Got {len(names)} labels but {len(numbers)} values.")
        xlabel, ylabel = x, y
    names, numbers = names[:200], numbers[:200]
    ink, accent = "#26221f", "#c8481f"
    figure = Figure(figsize=(9, 5.2), dpi=150)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    if kind == "line":
        axes.plot(names, numbers, color=accent, linewidth=2.2, marker="o", markersize=4)
    elif kind == "pie":
        palette = ["#c8481f", "#2f6f73", "#d99a2b", "#6b5b95", "#7a8b5a", "#b5646e", "#4a6fa5", "#a0a0a0"]
        axes.pie(numbers, labels=names, autopct="%1.0f%%", colors=[palette[index % len(palette)] for index in range(len(numbers))], textprops={"color": ink})
    elif kind == "scatter":
        axes.scatter(range(len(numbers)) if not all(_is_number(name) for name in names) else [float(name) for name in names], numbers, color=accent, s=28)
    elif kind == "hist":
        axes.hist(numbers, bins=min(24, max(5, len(numbers) // 4)), color=accent)
    else:
        axes.bar(names, numbers, color=accent, width=0.65)
    if kind != "pie":
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
        axes.tick_params(colors=ink)
        axes.grid(axis="y", alpha=0.18)
        axes.set_axisbelow(True)
        axes.set_xlabel(xlabel, color=ink)
        axes.set_ylabel(ylabel, color=ink)
        if len(names) > 12:
            axes.tick_params(axis="x", rotation=45)
    if title:
        axes.set_title(title, color=ink, loc="left", fontsize=13, fontweight="semibold")
    figure.tight_layout()
    default = re.sub(r"[^\w -]", "", title)[:40].strip() or f"Chart {dt.datetime.now():%Y-%m-%d %H%M%S}"
    target = resolve_output(path, kind="chart", default_name=f"{default}.png")
    figure.savefig(target)
    with Image.open(target) as check:
        size = check.size
    return Result(f"Saved a {kind} chart of {len(numbers)} values to {paths.pretty(target)}.", files=[target], verified=f"Valid image on disk: {size[0]}x{size[1]}.", undo={"kind": "delete_created", "path": str(target)})


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------- privacy
_PII_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<![\w.])(?:\+?\d(?:[ -]?\d){9,12}|\(\d{3}\)[ -]?\d{3}[ -]?\d{4})(?![\w])")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for label, pattern in _PII_PATTERNS:
        text, count = pattern.subn(f"[{label}]", text)
        if count:
            counts[label] = count
    return text, counts


@tool("redact_personal_info", "Mask emails, phone numbers, card numbers and similar details in text or a file. A file is saved as a new copy.", risk=WRITE, group="Documents", doing="Redacting personal details",
      keywords="redact mask hide personal info information pii anonymize anonymise scrub privacy email phone sensitive censor",
      text=arg("string", "Text to clean"), path=arg("string", "Or: a file to clean"))
def redact_personal_info(ctx, text: str = "", path: str = "") -> Result:
    if path:
        source = resolve_input(path)
        cleaned, counts = redact(extract_text(source)[0])
        target = resolve_output(str(source.with_name(f"{source.stem} (redacted).txt")), default_name="redacted.txt")
        target.write_text(cleaned, encoding="utf-8")
        total = sum(counts.values())
        return Result(f"Masked {total} item(s) and saved {paths.pretty(target)}. The original was not changed.", files=[target], verified=f"Masked: {counts or 'nothing found'}.", undo={"kind": "delete_created", "path": str(target)})
    if not text:
        raise ToolError("Give text or a file path.")
    cleaned, counts = redact(text)
    return Result(f"Masked {sum(counts.values())} item(s): {counts or 'nothing found'}.", cleaned)


# ---------------------------------------------------------------------- vision
def _image_b64(path: Path, longest: int = 1280) -> str:
    import io

    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((longest, longest))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def _look(ctx, image_path: Path, question: str) -> Result:
    vision = ctx.llm.vision_model()
    if vision:
        ctx.progress("Looking at the image")
        answer = ctx.llm.complete(question or "Describe what this image shows. Include any important text exactly.", images=[_image_b64(image_path)], model=vision, max_tokens=420)
        return Result(f"Looked at the image with {vision}.", answer.strip(), verified="The answer comes from the local vision model and was not independently checked.")
    text = ocr_image(image_path)
    return Result("No vision model is installed, so this is only the text Windows OCR could read.", text or "(no text found)", unverified=True)


@tool("describe_image", "Look at an image file and describe it or answer a question about it.", group="Vision", doing="Looking at an image", direct=True,
      keywords="image picture photo describe look see what showing diagram read",
      path=arg("string", "Image file", required=True), question=arg("string", "What to find out"))
def describe_image(ctx, path: str, question: str = "") -> Result:
    target = resolve_input(path, kinds=tuple(IMAGE_EXTS))
    if target.suffix.lower() not in IMAGE_EXTS:
        raise ToolError(f"{target.name} is not an image.")
    return _look(ctx, target, question)


@tool("look_at_screen", "See what is on the user's screen right now and answer a question about it. Use for 'what's on my screen', 'read this', 'what does this error mean'.", group="Vision", doing="Looking at the screen", direct=True,
      keywords="screen see look showing display error this reading page window current looking visible read",
      question=arg("string", "What to find out"))
def look_at_screen(ctx, question: str = "") -> Result:
    image, _ = capture("screen", ctx.shell)
    temp = paths.temp_file(".png")
    try:
        image.convert("RGB").save(temp)
        return _look(ctx, temp, question)
    finally:
        temp.unlink(missing_ok=True)
