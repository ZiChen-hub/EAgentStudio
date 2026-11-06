"""Office 文档读写：docx/xlsx 使用标准库 OOXML，pptx 读取用标准库、写入用 PowerPoint COM。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_READ_CHARS = 60000
MAX_PART_BYTES = 20 * 1024 * 1024
MAX_CELLS = 200_000
MAX_COLUMNS = 16_384  # Excel 单表最大列数（XFD）
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value) -> str:
    return escape(_ILLEGAL_XML.sub("", str(value)))


def _xml_attr(value) -> str:
    return escape(_ILLEGAL_XML.sub("", str(value)), {'"': "&quot;"})


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise ValueError(f"文档缺少部件: {name}") from error
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"文档部件过大: {name}")
    return archive.read(name)


def _read_zip_text(path: Path, name: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            return _read_member(archive, name).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"无法读取 {path.name}: {error}") from error


def _write_zip(path: Path, parts: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode("utf-8"))


# ---------------------------------------------------------------- docx

def _docx_paragraph(text: str, size: int = 22, bold: bool = False) -> str:
    lines = str(text).split("\n")
    runs = ""
    for index, line in enumerate(lines):
        if index:
            runs += "<w:br/>"
        if line:
            runs += f'<w:t xml:space="preserve">{_xml_text(line)}</w:t>'
    props = f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
    return (
        f'<w:p><w:pPr><w:spacing w:after="120"/></w:pPr>'
        f"<w:r>{props}{runs}</w:r></w:p>"
    )


def _docx_table(rows: list, columns: int) -> str:
    width = max(1, 9000 // max(1, columns))
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))
    body = ""
    for row in rows:
        cells = ""
        for index in range(columns):
            value = row[index] if index < len(row) else ""
            cells += (
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                f'<w:p><w:r><w:t xml:space="preserve">{_xml_text(value)}</w:t></w:r></w:p>'
                "</w:tc>"
            )
        body += f"<w:tr>{cells}</w:tr>"
    borders = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>{borders}</w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"
    )


def read_docx(path: Path) -> str:
    text = _read_zip_text(path, "word/document.xml")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"docx 内容不是合法 XML: {error}") from error
    body = root.find(f"{{{W}}}body")
    if body is None:
        return ""
    lines: list[str] = []
    for child in list(body):
        if child.tag == f"{{{W}}}p":
            content = "".join(node.text or "" for node in child.iter(f"{{{W}}}t"))
            style = child.find(f"{{{W}}}pPr/{{{W}}}pStyle")
            level = 0
            if style is not None:
                match = re.search(r"(?i)heading\s*(\d)", style.get(f"{{{W}}}val") or "")
                if match:
                    level = min(int(match.group(1)), 4)
            lines.append("#" * level + " " + content if level else content)
        elif child.tag == f"{{{W}}}tbl":
            for row in child.findall(f"{{{W}}}tr"):
                cells = []
                for cell in row.findall(f"{{{W}}}tc"):
                    pieces = [
                        "".join(node.text or "" for node in paragraph.iter(f"{{{W}}}t"))
                        for paragraph in cell.findall(f"{{{W}}}p")
                    ]
                    cells.append(" ".join(piece for piece in pieces if piece))
                lines.append(" | ".join(cells))
    return "\n".join(lines)[:MAX_READ_CHARS]


def write_docx(path: Path, blocks: list) -> None:
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph")
        if kind == "table":
            rows = block.get("rows") if isinstance(block.get("rows"), list) else []
            columns = max((len(row) for row in rows if isinstance(row, list)), default=0)
            if columns > 0:
                parts.append(_docx_table([row for row in rows if isinstance(row, list)], columns))
        elif kind == "heading":
            level = min(max(int(block.get("level") or 1), 1), 4)
            parts.append(_docx_paragraph(str(block.get("text") or ""), size=32 - (level - 1) * 2, bold=True))
        else:
            parts.append(_docx_paragraph(str(block.get("text") or "")))
    if not parts:
        parts.append(_docx_paragraph(""))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        + "".join(parts)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    _write_zip(
        path,
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Types xmlns="{CONTENT_TYPES}">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Relationships xmlns="{PKG_RELS}">'
                f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": document,
        },
    )


# ---------------------------------------------------------------- xlsx

def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _column_index(reference: str) -> int:
    letters = re.match(r"([A-Z]+)", reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(1):
        value = value * 26 + (ord(char) - 64)
    return value - 1


def _sheet_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "-", str(name or "")).strip() or fallback
    return cleaned[:31]


def _cell_xml(row_index: int, column: int, value) -> str:
    reference = f"{_column_name(column)}{row_index}"
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = str(value)
    if text.startswith("=") and len(text) > 1:
        return f'<c r="{reference}"><f>{_xml_text(text[1:])}</f></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(text)}</t></is></c>'


def read_xlsx(path: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            workbook = ET.fromstring(_read_member(archive, "xl/workbook.xml"))
            rels = ET.fromstring(_read_member(archive, "xl/_rels/workbook.xml.rels"))
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ET.fromstring(_read_member(archive, "xl/sharedStrings.xml"))
                for item in shared_root.findall(f"{{{S}}}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{{{S}}}t")))
            targets = {
                element.get("Id"): element.get("Target") or ""
                for element in rels.findall(f"{{{PKG_RELS}}}Relationship")
            }
            sheets = []
            cells_seen = 0
            for sheet in workbook.findall(f"{{{S}}}sheets/{{{S}}}sheet"):
                rel_id = sheet.get(f"{{{R}}}id")
                target = targets.get(rel_id, "")
                member = target.lstrip("/")
                if not member.startswith("xl/"):
                    member = "xl/" + member
                if member not in names:
                    continue
                root = ET.fromstring(_read_member(archive, member))
                rows = []
                for row in root.findall(f"{{{S}}}sheetData/{{{S}}}row"):
                    values: dict[int, str] = {}
                    for cell in row.findall(f"{{{S}}}c"):
                        cells_seen += 1
                        if cells_seen > MAX_CELLS:
                            raise ValueError("表格单元格过多，已中止读取")
                        index = _column_index(cell.get("r") or "A1")
                        if index >= MAX_COLUMNS:
                            # 伪造的超大列号会把 rows 撑成巨大的稀疏列表
                            continue
                        kind = cell.get("t")
                        if kind == "inlineStr":
                            values[index] = "".join(
                                node.text or "" for node in cell.iter(f"{{{S}}}t")
                            )
                        else:
                            value_node = cell.find(f"{{{S}}}v")
                            raw = value_node.text if value_node is not None else ""
                            if kind == "s":
                                try:
                                    position = int(raw or 0)
                                    values[index] = (
                                        shared[position] if 0 <= position < len(shared) else ""
                                    )
                                except (TypeError, ValueError):
                                    values[index] = ""
                            elif kind == "b":
                                values[index] = "TRUE" if raw == "1" else "FALSE"
                            else:
                                formula = cell.find(f"{{{S}}}f")
                                if formula is not None and not raw:
                                    values[index] = "=" + (formula.text or "")
                                else:
                                    values[index] = raw or ""
                    if values:
                        width = max(values) + 1
                        rows.append([values.get(index, "") for index in range(width)])
                sheets.append({"name": sheet.get("name") or "", "rows": rows})
            return sheets
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError(f"无法读取 xlsx: {error}") from error


def write_xlsx(path: Path, sheets: list) -> None:
    normalized = []
    used_names: set[str] = set()
    for index, sheet in enumerate(sheets or []):
        if not isinstance(sheet, dict):
            continue
        name = _sheet_name(sheet.get("name"), f"Sheet{index + 1}")
        base = name
        suffix = 2
        # Excel 的工作表名不区分大小写，Data 与 data 也算重名
        while name.lower() in used_names:
            name = f"{base[:26]}-{suffix}"[:31]
            suffix += 1
        used_names.add(name.lower())
        rows = sheet.get("rows") if isinstance(sheet.get("rows"), list) else []
        normalized.append((name, rows))
    if not normalized:
        normalized = [("Sheet1", [])]

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Types xmlns="{CONTENT_TYPES}">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    for index in range(len(normalized)):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index + 1}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    sheet_tags = []
    rel_tags = []
    parts: dict[str, str] = {}
    for index, (name, rows) in enumerate(normalized, start=1):
        sheet_tags.append(
            f'<sheet name="{_xml_attr(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        rel_tags.append(
            f'<Relationship Id="rId{index}" Type="{R}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            if not isinstance(row, list):
                continue
            if len(row) > MAX_COLUMNS:
                raise ValueError(f"单行最多 {MAX_COLUMNS} 列，当前有 {len(row)} 列")
            cells = "".join(
                _cell_xml(row_index, column, value) for column, value in enumerate(row)
            )
            if cells:
                row_xml.append(f'<row r="{row_index}">{cells}</row>')
        parts[f"xl/worksheets/sheet{index}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{S}"><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
        )
    rel_tags.append(f'<Relationship Id="rId{len(normalized) + 1}" Type="{R}/styles" Target="styles.xml"/>')

    parts["[Content_Types].xml"] = "".join(content_types)
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_RELS}">'
        f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>{"".join(sheet_tags)}</sheets></workbook>'
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_RELS}">{"".join(rel_tags)}</Relationships>'
    )
    parts["xl/styles.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{S}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    _write_zip(path, parts)


# ---------------------------------------------------------------- pptx

def read_pptx(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            slides = []
            for name in archive.namelist():
                match = re.match(r"ppt/slides/slide(\d+)\.xml$", name)
                if match:
                    slides.append((int(match.group(1)), name))
            slides.sort()
            results = []
            total = 0
            for _, name in slides:
                root = ET.fromstring(_read_member(archive, name))
                texts = [node.text or "" for node in root.iter(f"{{{A}}}t")]
                content = "\n".join(text for text in texts if text.strip())
                remaining = MAX_READ_CHARS - total
                if remaining <= 0:
                    results.append("...（内容过长，已截断）")
                    break
                if len(content) > remaining:
                    content = content[:remaining] + "\n...（内容过长，已截断）"
                results.append(content)
                total += len(content)
                if total >= MAX_READ_CHARS:
                    break
            return results
    except (OSError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError(f"无法读取 pptx: {error}") from error


PPTX_SCRIPT = r"""
param([string]$JsonPath, [string]$OutPath)
$ErrorActionPreference = "Stop"
$data = Get-Content -Raw -Encoding UTF8 $JsonPath | ConvertFrom-Json
$ppt = New-Object -ComObject PowerPoint.Application
$pres = $null
try {
  $pres = $ppt.Presentations.Add($false)
  $index = 1
  foreach ($slide in $data.slides) {
    $item = $pres.Slides.Add($index, 2)
    if ($item.Shapes.Count -ge 1) {
      $item.Shapes.Item(1).TextFrame.TextRange.Text = [string]$slide.title
    }
    if ($item.Shapes.Count -ge 2) {
      $body = (@($slide.bullets) -join "`r")
      if ($body) { $item.Shapes.Item(2).TextFrame.TextRange.Text = $body }
    }
    $index++
  }
  $pres.SaveAs($OutPath, 24)
} finally {
  if ($pres) { $pres.Close() }
  $ppt.Quit()
}
"""


def write_pptx(path: Path, slides: list) -> None:
    if os.name != "nt":
        raise ValueError("PPTX 写入需要 Windows 上的 PowerPoint")
    payload = {"slides": []}
    for slide in slides or []:
        if not isinstance(slide, dict):
            continue
        bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
        payload["slides"].append(
            {
                "title": str(slide.get("title") or ""),
                "bullets": [str(item) for item in bullets],
            }
        )
    if not payload["slides"]:
        payload["slides"] = [{"title": "空白演示", "bullets": []}]
    with tempfile.TemporaryDirectory(prefix="eagent-pptx-") as folder:
        json_path = Path(folder) / "slides.json"
        script_path = Path(folder) / "build.ps1"
        json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        script_path.write_text(PPTX_SCRIPT, encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script_path), str(json_path), str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"启动 PowerPoint 失败：{error}") from error
        if completed.returncode != 0 or not path.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()[:400]
            raise ValueError(f"生成 PPTX 失败：{detail or 'PowerPoint COM 不可用'}")
