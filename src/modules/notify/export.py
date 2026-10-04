"""**导出为 Excel** —— 推送模块的一个能力。

用户 2026-09-21 原话：

> 「区长账号有**导出为 excel** 功能，这个**落在数据推送模块**吧，
>   功能模块**调用「导出为 excel」**来把自己的数据生成为 excel **到本地**」

| 谁 | 干什么 |
|---|---|
| **功能模块**（报量查询 / POS / 达成 / 历史 …） | 把自己的数据整理成「**表头 + 行**」（+ 可选口径说明）交出去 |
| **本模块** | 写成 xlsx → 落到本机 `out/exports/` → **把路径回给调用方** → 记一笔 |

⚠ 三条边界，都别越：

1. **这不是"导出并推送"**（那是 C7 的 `features/inventory/push.py`）——
   本模块**只写本地文件，一个网络请求都不发**。"推出去"是隔壁 `send()` 的事。
2. **功能模块不许各自写 xlsx 逻辑** —— 真写盘走 `src/xlsx_io.write_sheets`
   （差异清单一直在用它）。现在仓库里已经有**三份**各自写的
   （`features/compliance/pos/pos_export.py`、`features/compliance/comparison/export.py`、
   前端 `web/inventory/xlsx.js`），别再出第四份。
3. **落盘目录是 `out/exports/`，不是 `out/` 根** —— 根目录里是**每天的差异清单**
   和一堆 json（`report.list_reports()` 就在那儿 glob），导出件混进去分不清
   哪些是"我自己点的"。（库存盘点是同一个规矩：`out/inventory/`。）

## 用法（功能模块侧）

```python
from ...modules import notify

res = notify.export_xlsx(
    {"总览": (["门店", "达成率"], [[r["store"], r["total"]]])},
    name="周度达成-2026-W38",                 # 文件名（时间戳由本模块补）
    meta={"期间": "2026-W38", "数据截至": "2026-09-17"},
    root=root, who="杨英梅")
if res["ok"]:
    告诉用户(res["rel"])        # out/exports/周度达成-2026-W38-20260921-1042.xlsx
```

⚠ **`ok=False` 时"为什么"在 `why` 里**（本函数跟 `send()` 一样**绝不抛**）——
   调用方**必须**把它说出来。导出是"用户点了按钮在等一个文件"，
   静默失败比报错难查得多。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...storage import runlog
from ...storage.export_paths import EXPORT_DIR, export_dir

#: 结局 —— 和 `send()` 那两个一个风格（`state` 恒在，调用方按它分支）。
WRITTEN = "written"
FAILED = "failed"

#: 文件名 / 表名里不能出现的字符。
#: ⚠ Windows 比 POSIX 严得多：`\ / : * ? " < > |` 一个都不行，
#:   而且**文件名不能以点或空格结尾**（`"达成.xlsx."` 在 Windows 上直接建不出来）。
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 文件名主干最长多少 —— 加上时间戳和 `.xlsx`，给 Windows 的 260 字符路径留足余量
#: （门店的路径本来就长：`D:\cbg-reconcile\out\exports\…`）。
MAX_STEM = 60

#: Excel 的表名上限（硬限制），超了 openpyxl 直接抛。
MAX_SHEET = 31


def safe_stem(name: str) -> str:
    """把任意一句话变成一个**这台机器上建得出来**的文件名主干。

    ⚠ 空名字要兜底成「导出」：`name=""` 会让最终文件名变成 `-20260921-1042.xlsx`，
      看着像一个坏掉的文件。
    """
    s = _BAD.sub("-", str(name or ""))
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip(" .-")                      # ⚠ Windows：结尾的点和空格要去掉
    return s[:MAX_STEM].strip(" .-") or "导出"


def safe_sheet(name: str, fallback: str = "数据") -> str:
    """表名同样要洗（Excel 不许 `[]:*?/\\`，且 ≤31 字）。"""
    s = _BAD.sub("-", str(name or "")).strip() or fallback
    return s[:MAX_SHEET]


def unique_path(directory, stem: str, ext: str = ".xlsx") -> Path:
    """同名的**不覆盖，往后编号**（`-2` / `-3` …）。

    ⚠ 为什么不覆盖：① 门店/办公室点两次是常事，第二次把第一次盖掉等于
      "我上一份呢"；② **Windows 上文件正被 Excel 打开着就覆盖不了**
      （`PermissionError: [WinError 32]`）—— 那会变成一个"偶尔失败"的导出。
      编号不覆盖，这两种情况都不用管。
    """
    d = Path(directory)
    p = d / (stem + ext)
    if not p.exists():
        return p
    for i in range(2, 100):
        cand = d / ("%s-%d%s" % (stem, i, ext))
        if not cand.exists():
            return cand
    # 100 份同名（一天之内点一百次）—— 秒级时间戳兜底，别在这里抛
    return d / ("%s-%s%s" % (stem, datetime.datetime.now().strftime("%H%M%S"), ext))


def _header_pairs(headers):
    """表头是不是「键 + 标题」那种写法？是就返回 `[(键, 标题), …]`，不是返回 None。

    两种都收（`DETAIL_COLS` 那个项目里一直用的就是 `[(键, 标题)]`）：

    * `[("store", "门店"), ("qty", "台量")]` —— ⚠ **元素必须是 tuple**
    * `{"store": "门店", "qty": "台量"}`

    ⚠⚠ **键/标题用 `tuple`、表头行用 `list` —— 这是这两种写法唯一的区别，别混**：
      `[["门店", "达成"], ["", "目标"]]` 既可以读成"两行表头"，也可以读成"两个键值对"，
      而读错**不会报错**（只会把列接错）。所以这里只认 tuple，list 一律当**表头行**。
    """
    if isinstance(headers, dict):
        return [(k, v) for k, v in headers.items()]
    if headers and all(isinstance(h, tuple) and len(h) == 2 for h in headers):
        return [(h[0], h[1]) for h in headers]
    return None


def _normalize(headers, rows) -> Tuple[list, list]:
    """表头 + 行 → `(表头, 行 list[list])`。**表头可能是一维（一行）也可能是二维（多行）**。

    ⚠ **行是 dict 而表头没给键 —— 必须抛，不许猜**：`list(r.values())` 出来的
      列序是"字典的插入顺序"，接错了**不会报错**，只会把两列数字换个个儿
      （而 Excel 上完全看不出来）。这正是本项目最忌讳的那类失败。
    """
    pairs = _header_pairs(headers)
    out = []
    if pairs is not None:
        labels = [str(lbl) for _k, lbl in pairs]
        keys = [k for k, _lbl in pairs]
        for r in list(rows or ()):
            if isinstance(r, dict):
                out.append([_cell(r.get(k)) for k in keys])
            else:
                out.append([_cell(v) for v in r])
        return labels, out
    # 多行表头（第一行分组、后面几行叶子）—— 元素是 **list** 就是这种写法，
    # 写法见 `xlsx_io.write_sheets` 的 docstring（`None` = 横着合并，`""` = 竖着合并）。
    if headers and all(isinstance(h, (list, tuple)) for h in headers):
        for r in list(rows or ()):
            if isinstance(r, dict):
                raise ValueError("多行表头 + 字典行：字典行要配 [(键, 标题)] 那种表头"
                                 "（多行表头是按位置取的）")
            out.append([_cell(v) for v in r])
        return [list(h) for h in headers], out
    labels = [str(h) for h in (headers or ())]
    for r in list(rows or ()):
        if isinstance(r, dict):
            raise ValueError("表头只给了文字，行却是字典（表头写成 [(键, 标题)] 或 {键: 标题} "
                             "才能按字典取值）—— 按位置取值会把列接错，所以这里直接不干")
        out.append([_cell(v) for v in r])
    return labels, out


def _cell(v):
    """单元格值统一过一道：`None` → 空串（Excel 里 `None` 是空单元格，空串也是，
    但**别的类型**（比如 `Decimal` / `datetime`）留着更好 —— 它们是 Excel 认的原生类型）。"""
    return "" if v is None else v


def _resolve_formats(formats, labels) -> Dict[int, str]:
    """列格式的键允许写**列号（从 1 数）**或**表头文字**。

    ⚠ 多行表头按**最后一行（叶子）**认名字 —— 分组那行里写的是产品名，
      真正对得上"达成率"这三个字的是下面那行。
    """
    leaf = labels[-1] if labels and isinstance(labels[0], (list, tuple)) else labels
    out = {}
    for k, fmt in (formats or {}).items():
        if isinstance(k, int):
            out[k] = str(fmt)
        else:
            if str(k) not in leaf:
                raise ValueError("列格式里的「%s」不在这张表的表头里：%s"
                                 % (k, " / ".join(str(x) for x in leaf)))
            out[leaf.index(str(k)) + 1] = str(fmt)
    return out


def _as_book(sheets):
    """`sheets` → `({表名: (表头, 行, 列格式)}, 总行数)`。

    两种写法都收：

    * `(表头, 行)` / `(表头, 行, 列格式[, 样式])` —— **一张表**，表名叫「数据」
    * `{"表名": (表头, 行[, 列格式[, 样式]]), …}` —— 多张表

    `列格式` = `{列号或表头文字: Excel 的数字格式}`，例如 `{"达成率": "0.0%"}`。

    第 4 项可选 `样式` dict（表头色 / 边框 / 合计行反白 / **条件标色 `cf`**）
    —— 见 `xlsx_io.write_sheets`。
    """
    if isinstance(sheets, dict):
        items = list(sheets.items())
    else:
        items = [("数据", sheets)]
    book = {}
    total = 0
    for raw_name, spec in items:
        if not isinstance(spec, (tuple, list)) or len(spec) < 2:
            raise ValueError("每张表要写成 (表头, 行)：%s 那项不是" % raw_name)
        labels, rows = _normalize(spec[0], spec[1])
        fmt = _resolve_formats(spec[2] if len(spec) > 2 else None, labels)
        style = spec[3] if len(spec) > 3 and spec[3] else None
        # style 是可选的第 4 项 —— 没有就还是三元组（老调用点一个字不用改）
        book[safe_sheet(raw_name)] = ((labels, rows, fmt, style) if style
                                       else (labels, rows, fmt))
        total += len(rows)
    return book, total


def export_xlsx(sheets, *, name: str, meta=None, root=None, subdir: str = EXPORT_DIR,
                who: str = "", kind: str = "export", now=None) -> dict:
    """把「表头 + 行」写成 xlsx **落到本机**，返回路径。

    | 参数 | 说明 |
    |---|---|
    | `sheets` | 见 `_as_book`：一张表给 `(表头, 行)`，多张给 `{表名: (表头, 行[, 列格式[, 样式]])}` |
    | `name` | 文件名（不带扩展名、不带时间戳）—— **本模块补 `-YYYYMMDD-HHMM`** |
    | `meta` | 可选的口径说明 `{项目: 值}`，写进最后一张「说明」表 |
    | `root` | 安装根（测试必须传临时目录，别往真 `out/` 里写） |
    | `who` | 谁导的（进运行记录 —— "谁/何时/导了哪份"） |
    | `now` | 只给测试用：固定时间戳 |

    返回（**绝不抛**，跟 `send()` 一个规矩）：

    ```python
    {"ok": True, "state": "written", "why": "已导出 …",
     "path": "/…/out/exports/x.xlsx", "file": "x.xlsx",
     "rel": "out/exports/x.xlsx", "dir": "out/exports",
     "rows": 28, "sheets": ["总览", "说明"]}
    ```

    ⚠ `ok=False` 时 `path` / `file` 都是空串 —— 别把 `why` 丢掉直接说"导出成功"。
    """
    try:
        book, total = _as_book(sheets)
        if not book:
            raise ValueError("没有可导出的表（sheets 是空的）")
        d = export_dir(root, subdir)
        d.mkdir(parents=True, exist_ok=True)
        when = now or datetime.datetime.now()
        stem = safe_stem("%s-%s" % (name, when.strftime("%Y%m%d-%H%M")))
        path = unique_path(d, stem)
        # 「说明」表**放最后**：Excel 打开时停在第 1 张表，那张应该是数据本身。
        book[safe_sheet("说明", "说明")] = _meta_sheet(
            name, meta, who=who, when=when, rows=total, sheets=list(book))
        from ...xlsx_io import write_sheets
        write_sheets(path, book)
        rel = _rel(path, root)
        why = "已导出 %s（%d 行 · %d 张表）" % (rel, total, len(book))
        runlog.record(kind, True, note="%s%s" % (rel, " · " + who if who else ""), root=root)
        return {"ok": True, "state": WRITTEN, "why": why, "path": str(path),
                "file": path.name, "rel": rel, "dir": _rel(d, root),
                "rows": total, "sheets": list(book)}
    except Exception as e:                                     # noqa: BLE001
        why = "%s: %s" % (type(e).__name__, e)
        # 记一笔 —— 导出失败**必须留痕**：用户那边看到的是"点了没反应"，
        # 而运行记录是唯一能查的地方。记不上不影响返回值（`runlog` 本来就不抛）。
        runlog.record(kind, False, why=why, note="%s%s" % (name, " · " + who if who else ""),
                      root=root)
        return {"ok": False, "state": FAILED, "why": why, "path": "", "file": "",
                "rel": "", "dir": _rel(export_dir(root, subdir), root),
                "rows": 0, "sheets": []}


def _meta_sheet(name, meta, *, who: str, when, rows: int, sheets) -> tuple:
    """「说明」表 —— 这份文件**自己说清自己是什么**。

    ⚠ 这不是装饰：导出件一离开这个程序（发微信 / 拷 U 盘 / 塞进 PPT），
      就再也没人知道"数据截至哪天""达成率是怎么算的"。页面上那些提示
      （`⚠ 数据截至 09-17`）**不会跟着文件走**，所以得写进文件里。
    """
    items = [("导出内容", str(name or "")), ("生成时间", when.strftime("%Y-%m-%d %H:%M")),
             ("导出人", who or "（不知道是谁）"), ("共", "%d 行 · %d 张表" % (rows, 1 + len(sheets)))]
    if meta:
        # ⚠ **重名的跳过**（只留上面那几条）—— 实测踩到：达成那份自己又写了一遍
        #   「导出人」，于是说明表里出现两行「导出人」，看着像哪儿算重了。
        #   这四条是**能力层**的事实（谁导的/什么时候），业务侧不用也不该再写一遍。
        seen = {k for k, _v in items}
        for k, v in (meta.items() if isinstance(meta, dict) else meta):
            if str(k) in seen:
                continue
            seen.add(str(k))
            items.append((str(k), _cell(v)))
    return (["项目", "值"], [[k, v] for k, v in items], {})


def _rel(path, root=None) -> str:
    """给人看 / 给界面用的相对路径 —— **一律正斜杠**。

    ⚠ 别用 `str(Path)`：Windows 上是反斜杠，写进 JSON 再回到前端就成了
      `"out\\exports\\x.xlsx"`（JS 里 `\\e` 是转义），页面上显示成乱码
      （AGENTS.md 坑 1 的同一个根子）。
    """
    try:
        from ...paths import ROOT
        return Path(path).resolve().relative_to(Path(root or ROOT).resolve()).as_posix()
    except Exception:                                          # noqa: BLE001
        return Path(path).as_posix()


def recent_exports(root=None, limit: int = 20, subdir: str = EXPORT_DIR) -> List[dict]:
    """已经导出去的那些文件（新 → 旧）—— 界面上列「最近导出」用。

    ⚠ 只是**列目录**，不建表：文件本身就是记录（用户要的是"文件到本地"，
      再维护一张索引表只会多一处对不上的地方）。
    """
    d = export_dir(root, subdir)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.xlsx"):
        try:
            st = p.stat()
        except OSError:                                        # noqa: PERF203
            continue
        out.append({"name": p.name, "rel": _rel(p, root), "size": st.st_size,
                    "mtime": st.st_mtime,
                    "at": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


__all__ = ["EXPORT_DIR", "WRITTEN", "FAILED", "export_dir", "export_xlsx",
           "recent_exports", "safe_stem", "safe_sheet", "unique_path"]
