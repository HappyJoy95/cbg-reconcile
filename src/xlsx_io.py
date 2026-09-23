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


def _header_rows(header) -> list:
    """表头 → 若干行。**一维 list = 一行**（老写法）；**二维 = 多行**（分组表头）。

    ⚠ 多行时（2026-09-21 加的，导出为 Excel 要"每个品项三格：达成/目标/达成率"）：

    * **第一行是分组名** —— 同一组里后面那几格写 `None`，意思是"跟左边那格合并"；
    * **下面几行是叶子名** —— 留空（`""`）的那几格意思是"跟上面那格合并"。
    """
    if not header:
        return []
    rows = [list(r) for r in header] if isinstance(header[0], (list, tuple)) else [list(header)]
    width = max(len(r) for r in rows)
    return [r + [None] * (width - len(r)) for r in rows]


def _disp_len(s) -> int:
    """**显示宽度** —— 中日韩全角字符按 2 个字算。

    ⚠ Excel 的列宽是"能放几个数字"，而一个汉字要占 2 格。不这么算的话
      「青岛城阳万象汇店」（8 个字）会算成 8 宽，实际要 16 —— 页面上就是
      「青岛城阳万…」，而这一列恰恰是**导出件里最先要看的那一列**。
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _header_merges(head) -> list:
    """表头要合并哪些格子 → `[(首行, 首列, 末行, 末列)]`（行列都从 **1** 数）。

    ① **横向**：第一行里连续的 `None` 归到左边那格（产品名横跨它的三个子列）；
    ② **纵向**：第一行有名字、下面几行全空的那一列，跨整个表头。
    """
    out = []
    row = head[0]
    j = 0
    while j < len(row):
        if row[j] is None:
            j += 1
            continue
        k = j + 1
        while k < len(row) and row[k] is None:
            k += 1
        if k - j > 1:
            out.append((1, j + 1, 1, k))
        j = k
    if len(head) > 1:
        for j, v in enumerate(row):
            if v in (None, ""):
                continue
            if all((head[r][j] in (None, "")) for r in range(1, len(head))):
                out.append((1, j + 1, len(head), j + 1))
    return out


def _merge_header(ws, head) -> None:
    """把表头的分组格合并起来（规则见 `_header_merges`）。"""
    for r1, c1, r2, c2 in _header_merges(head):
        ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)


def _is_total_label(v) -> bool:
    """合计 / 共计 行的标签 —— 源表里红底反白那种。"""
    s = str(v or "").strip()
    return s == "合计" or s.startswith("共计")


def _cf_sqref(col: int, rows: list) -> str:
    """把**非合计**的数据行拼成 Excel 条件格式范围（`H3:H10 H12:H25`）。

    ⚠ 合计/共计行整行已是红底，CF 再盖上去会花 —— 源表也是跳过它们的。
    """
    from openpyxl.utils import get_column_letter
    letter = get_column_letter(int(col))
    ranges = []
    for rn in rows:
        if ranges and rn == ranges[-1][1] + 1:
            ranges[-1][1] = rn
        else:
            ranges.append([rn, rn])
    return " ".join("%s%d:%s%d" % (letter, a, letter, b) for a, b in ranges)


def _apply_cf(ws, head, rows, style) -> None:
    """按 `style["cf"]` 上条件格式 —— 规则跟源表标色一致。

    每条：`{"col": 表头名或列号, "op": "lessThan"|"greaterThan",
          "v": 阈值, "fill": "FFFF99CC", "font": "FF800000"}`
    """
    rules = (style or {}).get("cf") or []
    if not rules:
        return
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    leaf = head[-1] if head else []
    data_start = len(head) + 1
    data_rows = []
    for i, r in enumerate(rows):
        rn = data_start + i
        labels = list(r[:3]) if r else []
        if not any(_is_total_label(v) for v in labels):
            data_rows.append(rn)
    ops = {
        "lt": "lessThan", "lessThan": "lessThan",
        "gt": "greaterThan", "greaterThan": "greaterThan",
        "le": "lessThanOrEqual", "ge": "greaterThanOrEqual",
        "eq": "equal",
    }
    for rule in rules:
        col = rule.get("col", rule.get("column", rule.get("index")))
        if isinstance(col, str) and not col.isdigit():
            if col not in leaf:
                raise ValueError("条件格式里的「%s」不在这张表的表头里：%s"
                                 % (col, " / ".join(str(x) for x in leaf)))
            cidx = leaf.index(col) + 1
        else:
            cidx = int(col)
        op_raw = str(rule.get("op") or rule.get("operator") or "").strip()
        op = ops.get(op_raw, op_raw)
        if op not in ("lessThan", "greaterThan", "lessThanOrEqual",
                      "greaterThanOrEqual", "equal"):
            raise ValueError("不认识的条件格式运算符：%s" % op_raw)
        val = rule.get("v", rule.get("value"))
        fill = None
        if rule.get("fill"):
            fill = PatternFill(start_color=str(rule["fill"]),
                               end_color=str(rule["fill"]), fill_type="solid")
        font = Font(color=str(rule["font"])) if rule.get("font") else None
        sqref = _cf_sqref(cidx, data_rows)
        if not sqref:
            continue
        ws.conditional_formatting.add(
            sqref,
            CellIsRule(operator=op, formula=[str(val)], fill=fill, font=font),
        )


def write_sheets(path, sheets: dict) -> Path:
    """sheets: {sheet 名: (表头, 数据行 list[list])}，后面还可以跟两项（**都可选**）：

    ```python
    write_sheets(p, {
        "总览": ([["门店", None, None],             # 表头**两行**：第一行分组，`None` = 跟左边合并
                  ["", "达成", "目标"]],            # 第二行叶子；`""` = 跟上面那格合并
                 [["A店", 7, 6]]),                  # 数据行
        "差异": (["串号", "金额"], [["1", 2]], {2: "0.00"}),   # 第三项：那一列的 number_format
    })
    ```

    第四项可选 `style` —— 按源表美化（增值业务两份导出用）：

    ```python
    ("数据", rows, fmt, {
        "head_fill": "FFFF0000",      # 表头底色（源表：防护膜纯红 / 汇机保 C00000）
        "font": "微软雅黑",
        "head_size": 11, "body_size": 10,
        "border": True,               # 细边框 + 居中
        "total_fill": "FFFF0000",     # 「合计 / 共计」整行反白
        "total_font_color": "FFFFFFFF",
        "cf": [                       # 条件标色（源表同款阈值）
            {"col": "跟机率", "op": "lessThan", "v": 0.3,
             "fill": "FFFF99CC", "font": "FF800000"},
        ],
    })
    ```

    ⚠ 这几项都是 2026-09-21 为「导出为 Excel」加的，**都可选** ——
      不给 style 时走原来的蓝表头，老调用点一个字都不用改。
    ⚠ 存进去的**仍然是数字**（0.8933），`number_format` 只管"显示成什么"：既能一眼看懂，
      也能拿去求和/排序。反过来（存 "89.3%" 这种文本）就没法算了。
    ⚠ 列宽按**最后一行表头 + 数据**算 —— 分组那行是合并出去的，
      按它算会把每组的第一列撑到 44 字宽（看着像每三列夹一个空列）。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="FFB0B0B0")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    for name, spec in sheets.items():
        header, rows = spec[0], spec[1]
        formats = spec[2] if len(spec) > 2 and spec[2] else {}
        style = dict(spec[3]) if len(spec) > 3 and spec[3] else {}
        font_name = style.get("font") or "微软雅黑"
        head_fill = style.get("head_fill") or "FF4472C4"
        head_color = style.get("head_font_color") or "FFFFFFFF"
        head_size = float(style.get("head_size") or 11)
        body_size = float(style.get("body_size") or 10)
        use_border = bool(style.get("border", True)) if style else False
        align = style.get("align") or "center"
        total_fill = style.get("total_fill")
        total_color = style.get("total_font_color") or "FFFFFFFF"
        # 不给 style 时保持原行为：蓝表头、无边框、默认字号
        if not style:
            head_fill, head_color = "FF4472C4", "FFFFFFFF"
            font_name, head_size, body_size = None, 11, 11
            use_border = False
            align = "center"

        ws = wb.create_sheet(title=str(name)[:31])
        head = _header_rows(header)
        for r in head:
            ws.append(r)
        head_font = Font(name=font_name, size=head_size, bold=True, color=head_color)
        head_fill_obj = PatternFill("solid", fgColor=head_fill)
        for r in range(1, len(head) + 1):
            for cell in ws[r]:
                cell.font = head_font
                cell.fill = head_fill_obj
                cell.alignment = Alignment(horizontal=align, vertical="center",
                                           wrap_text=bool(style))
                if use_border:
                    cell.border = box
        if head:
            _merge_header(ws, head)
        for r in rows:
            ws.append(list(r))
        # 正文/合计只在给了 style 时描 —— 不给就和从前一样（只设 number_format）
        if style:
            body_font = Font(name=font_name, size=body_size, color="FF000000")
            total_font = Font(name=font_name, size=body_size, bold=True, color=total_color)
            total_fill_obj = PatternFill("solid", fgColor=total_fill) if total_fill else None
            data_start = len(head) + 1
            for r_idx in range(data_start, ws.max_row + 1):
                row_vals = [ws.cell(r_idx, c).value for c in range(1, min(ws.max_column, 3) + 1)]
                is_total = bool(total_fill) and any(_is_total_label(v) for v in row_vals)
                for c_idx in range(1, ws.max_column + 1):
                    cell = ws.cell(r_idx, c_idx)
                    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
                    if use_border:
                        cell.border = box
                    if is_total:
                        cell.font = total_font
                        if total_fill_obj is not None:
                            cell.fill = total_fill_obj
                    else:
                        cell.font = body_font
        for col, fmt in formats.items():
            for cell in ws[get_column_letter(int(col))]:
                cell.number_format = fmt
        _apply_cf(ws, head, list(rows), style)
        # ⚠ 宽度只看**叶子表头 + 数据**（见 docstring 最后一条），而且是**显示宽度**
        width_src = ([head[-1]] if head else []) + [list(r) for r in rows]
        widths = {}
        for i in range(1, ws.max_column + 1):
            vals = [str(r[i - 1]) for r in width_src if len(r) >= i and r[i - 1] is not None]
            widths[i] = min(max(max((_disp_len(v) for v in vals), default=8) + 2, 10), 44)
        # ⚠ 合并出去的分组表头（产品名横跨三列）：三列加起来得放得下那个名字，
        #   否则「Pad SE/儿童手表5pro/watch5（10%）」会被截掉半截。
        for r1, c1, r2, c2 in (_header_merges(head) if head else []):
            if r1 != r2 or c2 <= c1:
                continue
            need = _disp_len(head[0][c1 - 1]) + 2
            have = sum(widths[j] for j in range(c1, c2 + 1))
            if have >= need:
                continue
            each = (need - have) / float(c2 - c1 + 1)
            for j in range(c1, c2 + 1):
                widths[j] = min(widths[j] + each, 44)
        for i, w in widths.items():
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A%d" % (len(head) + 1)
    wb.save(path)
    return path
