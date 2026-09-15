"""xlsx 读写。

读取**不信任 `<dimension>`** —— 实测踩过：ERP 导出的 xlsx 里没有 dimension 标签，
openpyxl 只会读到表头（23,593 行数据全丢，且不报错）。所以这里直接遍历 `<row>`。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_rows(path) -> list[list]:
    """读第一个 sheet 为二维列表。空行会被保留为空列表。"""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))

    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        return []
    root = ET.fromstring(z.read(sheets[0]))
    rows: list[list] = []
    for row in root.iter(_NS + "row"):
        cells: dict[int, object] = {}
        for c in row.findall(_NS + "c"):
            idx = _col_index(c.get("r"))
            t, v, isel = c.get("t"), c.find(_NS + "v"), c.find(_NS + "is")
            if t == "s" and v is not None:
                val: object = shared[int(v.text)] if v.text and int(v.text) < len(shared) else None
            elif t == "inlineStr" and isel is not None:
                val = "".join(x.text or "" for x in isel.iter(_NS + "t"))
            elif v is not None:
                val = v.text
            else:
                val = None
            cells[idx] = val
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i) for i in range(width)])
        else:
            rows.append([])
    return rows


def read_sheets(path) -> dict:
    """读**全部** sheet，返回 {sheet 名: [行]}。前端展示报告用。

    用 openpyxl（我们自己写的报告 dimension 是完整的）；
    读 ERP 导出的 xlsx 请用 read_rows()，那个不能信 dimension。
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out = {}
    for name in wb.sheetnames:
        ws = wb[name]
        out[name] = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return out


def write_sheets(path, sheets: dict) -> Path:
    """sheets: {sheet 名: (表头 list, 数据行 list[list])}。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4472C4")

    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(title=str(name)[:31])
        if header:
            ws.append(list(header))
            for cell in ws[1]:
                cell.font = head_font
                cell.fill = head_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
        for r in rows:
            ws.append(list(r))
        for i, col in enumerate(ws.columns, start=1):
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 44)
        ws.freeze_panes = "A2"
    wb.save(path)
    return path
