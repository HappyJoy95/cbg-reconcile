"""读腾讯文档（周度目标「任务目标分配」+ 映射表）—— 3.0.0 新加。

**为什么要有这个模块**：周度销售目标在腾讯文档里，门店电脑得自己读得到它。
试过三条路，只有这条成（2026-09-18 实测）：

1. ❌ 文档主表那条（skill 的 `sync_mapping_from_tencent_docs.py` → WorkBuddy 票据）——
   门店电脑上**不可能有 WorkBuddy**。
2. ❌ 官方开放平台 —— 要注册应用 + OAuth，token 绑账号、会过期，
   14 家门店各授权一次不可持续。
3. ✅ **匿名只读这条**：目标表是「只能查看」，不登录就能读。

## 怎么读（纯 HTTP + 标准库的解压，不用浏览器、不用登录、不用 cookie）

```
① GET https://docs.qq.com/sheet/<doc>
      → 从 HTML 抠出 preload 的 dop-api/opendoc URL（带 t 令牌 + xsrf + tab=）
② GET 那个 URL → JSONP → clientVars...initialAttributedText.text[0]
③ text[0].workbook          → base64+zlib → protobuf：所有 tab 的 id + 名字
   text[0].block_datas[0].related_sheet → base64+zlib → protobuf：**该 tab 的格子**
```

## ⚠⚠ 这是**未公开接口**，读到读不到都要有交代

* 参数、`t` 令牌、protobuf 结构**都可能随腾讯文档前端版本变**。
* 所以：**读不到一律抛 `TdocError`，绝不返回空/零** ——
  3.0.0 最忌讳的失败模式就是"算出来一份达成率全 0、看着很合理的错数据"。
* 界面上要能说清是"网不通 / 文档改版 / 目标没填"，而不是笼统一句失败。

## 单元格编码（规则全是实测出来的，别凭直觉改）

```
text[0] → block_datas[i] → related_sheet → zlib → protobuf
  块 → f5 → { f1: 18, f19: X }
  X  → f3 | f4 | **f5 = 值池** | **f6[] = 单元格**
  f6 → f1 = 行,  f2 = 列（缺省 0）,  f3 = { f1 = 种类, f2 = {f1: …}, f4 = {f1: 样式} }
```

| 种类 `f3.f1` | 含义 | 值怎么取 |
|---|---|---|
| **4** | 文本 | `值池[f3.f2.f1]`（**下标**；缺省 0） |
| **2** | 数字 —— ⚠ **两个子类，靠 `f4.f1` 分** | 见下表 |
| **6** | 富文本（**多行**单元格，如 `X6\nX7\nPura x max`） | 本模块**不解**，见 `decode_grid` 的 rich |
| 缺省 | 空 | — |

**种类 2 的两个子类**（⚠ 踩过：把特殊数值当成普通数字，日期读成 129）：

| `f4.f1` | 含义 | 值在哪 |
|---|---|---|
| **1** | **特殊数值**（日期 / 百分比） | **值池里的 double 序列，按出现顺序一一对应**；`f2.f1` 只是列 id，**不是值** |
| 其它 | 普通数字 | `f2.f1` **就是值本身** |

值池里**只有 `f1` 字段的条目才是纯文本**，后面混着字体(`f2`)、double(`f3`)、富文本(`f4`/`f5`）
—— 取值必须**按字段号挑**，不能按下标数。

## 三个真踩过的坑

1. **别把数字当下标**。`6,3,3,3,3,1,1,2,1` 就是台量本身 ——
   当成 ID 去查了一圈表，是拿第 2 行跟页面截图**逐格比对**才反应过来的。
2. **别拿小表的规则推大表**。映射表里「值池与单元格一一对应」成立，
   目标表就不成立（456 格 vs 81 项）。**每张表都要单独验**。
3. **同一字段两套语义**：`种类 2` 底下还有普通数字 / 日期·百分比两套。
   → `_SPECIAL` 的配对**两边个数必须相等，不等就抛**，不许静默错配。

## 频率

重复请求会被腾讯限流（实测踩到 `SSLZeroReturnError`）。
**抓一次存本地，之后离线分析**；生产路径每天只读 1~2 次。
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import struct
import zlib

import requests

#: 默认文档（「任务目标分配」）。换个文档就换这个 id。
DEFAULT_DOC = "DTE90WnJBWUhtUHhS"
#: 目标 tab 与映射 tab 的名字 —— **按名字找，不写死 tab id**
TARGET_TAB = "周度重点产品"
MAPPING_TAB = "周度重点映射表"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

#: Excel 序列号的纪元（1900 体系，含那个著名的闰年 bug）
XL_EPOCH = datetime.date(1899, 12, 30)


class TdocError(RuntimeError):
    """读腾讯文档失败。**一律抛这个，不返回空值** —— 见模块头部。"""


# =============================================================== protobuf
def _varint(buf, i):
    """读一个 varint。返回 `(值, 下一个位置)`。"""
    val = shift = 0
    while True:
        if i >= len(buf):
            raise TdocError("varint 读越界（数据被截断？）")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return val, i


def parse_message(buf, depth=0):
    """无 schema 的 protobuf 走查。返回 `(字段列表, 消费字节数)`。

    字段列表的每项是 `(字段号, 值)`；值是 int（varint）或**子列表**（嵌套消息）
    或 bytes/str（定长内容）。

    ⚠ **递归判据是「子解析恰好消费完整块」** —— 一开始写成"子层直接含字符串才算消息"，
    结果整块被当成一个字符串，门店名一个都出不来（2026-09-18 踩过）。

    ⚠⚠ **64 位 / 32 位定长字段一律跳过、不记进结果**。看着像丢数据，其实是必须的：
    单元格文本的字节**可能恰好能被解析成一个合法消息**（`鸿蒙PC(鸿蒙14除外)` 首字节
    `0xE9` = 「字段 29 + wire type 1」，后面凑够 8 字节就能"解析成功"）。
    一旦把定长字段记进结果，这种块就被判成"是消息"、字符串从此取不到 ——
    **表现是那一格解出来是空串，而且不报错**（2026-09-18 踩过：
    映射表最后一列名字变成空，于是只读到 8 列）。
    跳过之后这种块会因为"子解析结果为空"而正确地退回字符串。
    """
    out = []
    i = 0
    n = len(buf)
    while i < n:
        key, j = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, j)
            out.append((field, val))
        elif wire == 2:
            length, j = _varint(buf, j)
            if j + length > n:
                break                      # 尾部残缺，别硬解
            chunk = buf[j:j + length]
            i = j + length
            sub = used = None
            if depth < 12 and length:
                try:
                    sub, used = parse_message(chunk, depth + 1)
                except TdocError:
                    sub = None
            if sub is not None and used == len(chunk) and sub:
                out.append((field, sub))
            else:
                # ⚠ 不是消息就**尽量解成字符串** —— 单元格文本就是这个形态。
                #   一律存 bytes 的话，`leaf_strings`（只收 str）会把文本全漏掉，
                #   表现是**格子解出来全是空串、还不报错**（2026-09-18 踩过）。
                try:
                    out.append((field, chunk.decode("utf-8")))
                except UnicodeDecodeError:
                    out.append((field, chunk))
        elif wire == 1:
            i = j + 8                      # 跳过，**不记**
        elif wire == 5:
            i = j + 4                      # 跳过，**不记**
        else:
            break                          # 3/4/6/7 不是合法 wire type，别猜
        if i > n:
            break
    return out, i


def find_all(nodes, field):
    """把整棵树里所有 `field` 号的**列表值**收上来（深度优先，保序）。"""
    out = []
    for no, val in nodes:
        if isinstance(val, list):
            if no == field:
                out.append(val)
            out.extend(find_all(val, field))
    return out


def pick(msg, field):
    """取一个字段的值；没有就返回 None。"""
    if not isinstance(msg, list):
        return None
    for no, val in msg:
        if no == field:
            return val
    return None


def leaf_strings(nodes):
    """把子树里的字符串按顺序收上来。"""
    out = []
    for _no, val in nodes:
        if isinstance(val, list):
            out.extend(leaf_strings(val))
        elif isinstance(val, str):
            out.append(val)
    return out


def _unzip_b64(text):
    """base64 + zlib → bytes。两个文档都是这么存块的。"""
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
        return zlib.decompress(raw)
    except Exception as exc:                                   # noqa: BLE001
        raise TdocError("块解压失败（腾讯文档换格式了？）：%s" % exc) from exc


# ============================================================== 解码（纯函数）
def text_vars(body):
    """JSONP 响应体 → `clientVars...initialAttributedText.text[0]`。"""
    m = re.match(r"\w+\((.*)\)\s*$", body or "", re.S)
    if not m:
        raise TdocError("不是 JSONP（前 80 字：%r）—— 大概率是登录页或改版了"
                        % (body or "")[:80])
    try:
        data = json.loads(m.group(1))
        return data["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise TdocError("JSONP 结构变了（找不到 text[0]）：%s" % exc) from exc


def sheet_tabs(text0):
    """从 `workbook` 解出 `[(tab_id, tab名), …]`。

    ⚠ **第一个 sheet 不存 id** —— 它是默认表，id 得用 URL 里那个 `tab=` 兜底。
    本函数把这种表的 id 返回成 `None`，由调用方填。
    """
    blob = text0.get("workbook")
    if not blob:
        raise TdocError("block 里没有 workbook（拿不到 tab 列表）")
    nodes, _ = parse_message(_unzip_b64(blob))
    tabs = []
    for sheet in find_all(nodes, 5):
        tab_id = name = None
        for no, val in sheet:
            if no != 2 or not isinstance(val, list):
                continue
            inner = pick(val, 3)           # f3.f1 = tab id
            if isinstance(inner, list):
                got = [x for x in leaf_strings(inner) if re.fullmatch(r"[a-z0-9]{6}", x)]
                if got:
                    tab_id = got[0]
            inner = pick(val, 5)           # f5.f1 = tab 名
            if isinstance(inner, list):
                got = leaf_strings(inner)
                if got:
                    name = got[0]
        if name:
            tabs.append((tab_id, name))
    if not tabs:
        raise TdocError("workbook 里一个 tab 都没解出来（结构变了）")
    return tabs


def _as_double(val):
    """从值池条目里取 double（日期 / 百分比），取不到返回 None。

    ⚠ **一个 double 有两种落地形态，两种都要认**：protobuf 里 `\\t` + 8 字节
    恰好是「field 1, wire type 1」，所以解析器**可能把它当成一个嵌套消息**
    （那就是 `[(1, b'…8字节…')]`），也可能原样给 bytes。
    只认一种的话，`numbers` 会是空的，然后 `_SPECIAL` 那条计数校验直接抛
    —— 2026-09-18 就是这么发现的（映射表的 C1/D1 读不到）。
    """
    if isinstance(val, (bytes, bytearray)) and len(val) == 9 and val[:1] == b"\t":
        return struct.unpack("<d", bytes(val[1:]))[0]
    if isinstance(val, list):
        raw = pick(val, 1)
        if isinstance(raw, (bytes, bytearray)) and len(raw) == 8:
            return struct.unpack("<d", bytes(raw))[0]
    return None


def decode_grid(text0):
    """把一个 tab 的 block 解成网格。返回 `(grid, rich, numbers)`。

    * `grid`    —— `{(行, 列): 值}`，行列都从 **0** 开始；值是 str 或 int/float
    * `rich`    —— `{(行, 列): 富文本编号}`，**多行单元格**（本模块不解，见模块头）
    * `numbers` —— 值池里的 double 序列（日期 / 百分比就在这儿，按序与 `_SPECIAL` 配）

    ⚠ 特殊数值的配对**两边个数不等就抛** —— 宁可报错，不许错配（错配出来的是
    一份看着很合理的错数据）。
    """
    blocks = text0.get("block_datas") or []
    if not blocks:
        raise TdocError("这个 tab 没有 block_datas（空的？还是结构变了）")

    grid, rich, numbers, special = {}, {}, [], []
    for block in blocks:
        blob = block.get("related_sheet")
        if not blob:
            continue
        nodes, _ = parse_message(_unzip_b64(blob))
        found = find_all(nodes, 19)
        if not found:
            continue
        cells_root = max(found, key=len)          # 最大的那个才是格子块
        pool_lists = [v for no, v in cells_root if no == 5 and isinstance(v, list)]
        if not pool_lists:
            continue
        pool = pool_lists[0]

        texts = []
        for no, val in pool:
            if no == 1 and isinstance(val, list):
                got = leaf_strings(val)
                texts.append(got[0] if got else "")
            elif no == 3:
                double = _as_double(val)
                if double is not None:
                    numbers.append(double)

        for no, cell in cells_root:
            if no != 6 or not isinstance(cell, list):
                continue
            row = pick(cell, 1) or 0
            col = pick(cell, 2) or 0
            f3 = pick(cell, 3) or []
            kind = pick(f3, 1)
            idx = pick(pick(f3, 2), 1)
            style = pick(f3, 4)
            if kind == 4:                          # 文本 → 池里第 idx 个
                i = idx or 0
                grid[(row, col)] = texts[i] if 0 <= i < len(texts) else ""
            elif kind == 2:                        # 数字：两个子类
                if isinstance(style, list) and pick(style, 1) == 1:
                    special.append((row, col))     # 特殊数值：值在 numbers 里，按序配
                else:
                    grid[(row, col)] = idx if idx is not None else 0
            elif kind == 6:                        # 富文本（多行）—— 不解
                rich[(row, col)] = idx
    if numbers or special:
        if len(numbers) != len(special):
            raise TdocError(
                "特殊数值对不上：格子里 %d 个、值池里 %d 个 double"
                "（腾讯文档的存储规则变了 —— 宁可报错也不许错配）"
                % (len(special), len(numbers)))
        for (row, col), val in zip(special, numbers):
            grid[(row, col)] = val
    if not grid:
        raise TdocError("解出来是空网格 —— 别把空当『没数据』，多半是结构变了")
    return grid, rich, numbers


# ============================================================== IO（走网络）
def _get(url, referer=None, session=None, timeout=60):
    sess = session or requests
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        resp = sess.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise TdocError("请求失败（网不通 / 被限流）：%s" % exc) from exc
    if resp.status_code != 200:
        raise TdocError("HTTP %s：%s" % (resp.status_code, url[:80]))
    return resp.text


def doc_url(doc=DEFAULT_DOC):
    return "https://docs.qq.com/sheet/%s" % doc


def opendoc_url(html, doc=DEFAULT_DOC):
    """从文档页 HTML 里抠出 preload 的 opendoc URL。

    ⚠ HTML 里**有两个** preload 链接，**只有一个带 `tab=`** ——
    抓错那个的话，后面"换 tab"就变成空操作，7 个 tab 拿回同一份数据
    （2026-09-18 踩过，看着像"tab 参数不生效"）。
    """
    urls = [u.replace("&amp;", "&") for u in
            re.findall(r"(//docs\.qq\.com/dop-api/opendoc\?[^\"'\s<>]+)", html or "")]
    if not urls:
        raise TdocError("HTML 里没有 opendoc preload —— 文档改版了，或者需要登录")
    with_tab = [u for u in urls if "tab=" in u]
    return "https:" + (with_tab or urls)[0]


def fetch_html(doc=DEFAULT_DOC, session=None):
    return _get(doc_url(doc), session=session)


def fetch_tab(doc=DEFAULT_DOC, tab_id=None, html=None, session=None):
    """取某个 tab 的 `text[0]`。`tab_id=None` 就用 URL 里那个默认 tab。"""
    html = html if html is not None else fetch_html(doc, session=session)
    url = opendoc_url(html, doc)
    if tab_id:
        url = re.sub(r"([?&])tab=[^&]*", r"\1tab=" + tab_id, url)
    return text_vars(_get(url, referer=doc_url(doc), session=session))


def default_tab_id(html):
    """HTML 里 preload URL 自带的 `tab=` —— 也就是第一个 sheet 的 id。"""
    m = re.search(r"[?&]tab=([^&]*)", opendoc_url(html))
    return m.group(1) if m else None


def resolve_tab(text0, html, name):
    """按 **tab 名**找 id（找不到就抛，并列出所有现有 tab）。"""
    tabs = sheet_tabs(text0)
    fallback = default_tab_id(html)
    for tab_id, tab_name in tabs:
        if tab_name == name:
            return tab_id or fallback
    raise TdocError("文档里没有叫『%s』的 tab；现有：%s"
                    % (name, "、".join(n for _i, n in tabs)))


def read_tab(name, doc=DEFAULT_DOC, session=None):
    """按名字读一个 tab 的网格。返回 `(grid, rich, numbers)`。"""
    html = fetch_html(doc, session=session)
    text0 = fetch_tab(doc, html=html, session=session)
    tab_id = resolve_tab(text0, html, name)
    if tab_id != default_tab_id(html):
        text0 = fetch_tab(doc, tab_id=tab_id, html=html, session=session)
    return decode_grid(text0)


# ============================================================== 业务层
def xl_date(serial):
    """Excel 序列号 → `datetime.date`（日期在文档里就是这么存的）。"""
    return XL_EPOCH + datetime.timedelta(days=int(serial))


def norm_column(name):
    """产品列名归一化 —— 两边写法天然不同，必须归一。

    ⚠ 表头单元格里是**换行**（`畅享90plus\\n（m plus）`），
    映射表里粘的是**斜杠**（`畅享90plus/（m plus）`）；
    还有全角括号、夹杂空格。不归一就匹不上，而**匹不上会静默算成 0**。
    """
    text = str(name or "").lower()
    for old, new in (("（", "("), ("）", ")"), ("／", "/")):
        text = text.replace(old, new)
    return "".join(ch for ch in text if not ch.isspace() and ch != "/")


def read_mapping(grid, rich=None):
    """映射表 → `{"columns": [(产品列, [编码]), …], "start": date, "end": date}`。

    表结构：A 列产品列、B 列商品编码（`|` 分隔）；
    **C1 / D1 是起始 / 结束日期**（用户 2026-09-18 定，替代"猜当前周"）。
    """
    columns = []
    for row in range(1, 1000):
        name = grid.get((row, 0))
        codes = grid.get((row, 1))
        if not isinstance(name, str) or not name.strip():
            break
        if not isinstance(codes, str):
            raise TdocError("映射表第 %d 行（%s）没有编码 —— 别当空，报出来" % (row + 1, name))
        columns.append((name, [c for c in codes.split("|") if c]))
    if not columns:
        raise TdocError("映射表里一列都没读到（表空？还是结构变了）")

    start = grid.get((0, 2))
    end = grid.get((0, 3))
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise TdocError("映射表 C1/D1 读不到期间 —— 请在 C1/D1 填起始/结束日期")
    return {"columns": columns, "start": xl_date(start), "end": xl_date(end)}


def read_targets(grid, rich=None, mapping_columns=None):
    """目标表 → `{"weights": [...], "columns": [...], "rows": [(门店, [台量…]), …]}`。

    表结构：第 0 行权重、第 1 行产品列名、第 2 行起每行一个门店（第 2 列是门店名）。

    ⚠ **多行单元格（富文本）解不出来**（`X6\\nX7\\nPura x max` 这种，本表 4 格）。
    所以列名是这样定的：**先用已解出来的列名跟映射表逐位核对**，
    位置全对得上，才用映射表的名字**按位置**补齐剩下的 —— 不做无根据的按位对齐。
    """
    weights = []
    for col in range(2, 40):
        val = grid.get((0, col))
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            break
        weights.append(float(val))
    if not weights:
        raise TdocError("目标表第 1 行没有权重 —— 结构变了？")

    cols = list(range(2, 2 + len(weights)))
    names = []
    unknown = []
    for col in cols:
        val = grid.get((1, col))
        if isinstance(val, str) and val.strip():
            names.append(val)
        else:
            names.append(None)
            unknown.append(col)

    if unknown:
        if not mapping_columns:
            raise TdocError(
                "有 %d 个产品列名解不出来（多行单元格），需要传 mapping_columns 才能按位置补"
                % len(unknown))
        if len(mapping_columns) != len(cols):
            raise TdocError("映射表 %d 列 vs 目标表 %d 列，对不上，不敢按位置补"
                            % (len(mapping_columns), len(cols)))
        # 先核对**已解出来的**列名位置对不对 —— 位置对不上就说明列被挪过
        by_norm = {norm_column(n): i for i, n in enumerate(mapping_columns)}
        for i, name in enumerate(names):
            if name is None:
                continue
            if by_norm.get(norm_column(name)) != i:
                raise TdocError(
                    "目标表第 %d 列是『%s』，在映射表里却是第 %d 列 —— "
                    "两边列序不一致，不敢按位置补名字"
                    % (i + 1, name, (by_norm.get(norm_column(name)) or -1) + 1))
        names = [n if n is not None else mapping_columns[i] for i, n in enumerate(names)]

    rows = []
    for row in range(2, 1000):
        store = grid.get((row, 1))
        if not isinstance(store, str) or not store.strip():
            continue
        if store.strip() in ("合计", "总计"):
            continue
        targets = []
        for col in cols:
            val = grid.get((row, col), 0)
            targets.append(int(val) if isinstance(val, (int, float)) else 0)
        rows.append((store, targets))
    if not rows:
        raise TdocError("目标表里一个门店都没读到")
    return {"weights": weights, "columns": names, "rows": rows}
