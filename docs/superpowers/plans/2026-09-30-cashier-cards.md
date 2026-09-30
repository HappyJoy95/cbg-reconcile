# 收银订单卡改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把收银页改成「吸顶录入 + 当日汇总 + 每笔订单一张卡（卡内编辑）」，并新增 SN/配件/组合支付字段与一键导入玲珑销售单。

**Architecture:** 原地重排 `#subpanel-cashier`（方案 A，不抽 iframe）；后端 `sale_entries` 迁移补 5 列，导入 = `dump.load_orders` 合并 orders 表 → `entries_from_orders` 纯读库生成卡片；前端 `renderCashierTable` 换成 `renderCashierCards`，卡内编辑走同一个 `entry-save`。

**Tech Stack:** Python 3.8+ / sqlite3（迁移框架）/ 原生 JS + CSS（无构建步骤）/ unittest + pytest。

**Spec:** `docs/superpowers/specs/2026-09-30-cashier-cards-design.md`（已获批）

**测试命令:** `.venv/bin/python -m pytest tests/test_cashier.py -q`（单文件）；每个任务收尾跑受影响文件；最后全量 `.venv/bin/python -m pytest tests/ -q`。

---

### Task 1: 开发目标文档（项目规矩，开工前必写）

**Files:**
- Create: `.dsh/docs/2026-09-30-收银订单卡-开发目标.md`

- [ ] **Step 1: 写开发目标文档**

按 AGENTS.md 十节格式（照抄 `.dsh/docs/2026-09-16-2.0.0-开发目标.md` 的骨架），内容从已获批 spec 浓缩。**十节全部要有，第八节「本版不做的」必填**：

```markdown
# 2026-09-30 收银订单卡 · 开发目标

## 一、一句话目标
收银页从「单笔录入表单+扁平表格」改成「吸顶录入+当日汇总+订单卡（卡内编辑）」，
并加一键导入玲珑销售单。

## 二、已定的
布局/字段/导入/删除语义全部见已获批 spec：
`docs/superpowers/specs/2026-09-30-cashier-cards-design.md`（五节设计 + 后端/前端/测试/不做）。

## 三、边界与项目红线
- 方案 A：原地重排 #subpanel-cashier，不抽 iframe；
- 后端接口形状不破坏：现有 /api/cashier/* 五个路由的行为不变；
- 测试必须传 root= 临时目录；.secrets/ 只写 cashier-import.json；
- Python 3.8 兼容（不用 match、不用 X | None 运行期注解）。

## 四、里程碑
1. 后端：迁移 → 字段存取 → 黑名单/排除 → 导入（各自独立可测）
2. 前端：HTML/CSS 重排 → 卡片渲染+编辑 → 导入/黑名单 UI
3. 全量回归

## 五、要用户给的
（无 —— 参考图与支付方式清单已给）

## 六、验收标准
见 spec「测试与验收」节。

## 七、风险
- 导入是网络操作（会话失效要给人话）；
- 前端改动面大（app.js 收银段整段重写），node --check + 接线测试必须绿。

## 八、本版不做的
见 spec「本版不做」：客户信息真接入 / 独立页 iframe / 扫码自动识别商品码与 SN /
支付清单后端可配置 / 响应式断点 / 配件金额并入实收。

## 九、进展
（实施时逐条勾）
```

- [ ] **Step 2: Commit**

```bash
git add .dsh/docs/2026-09-30-收银订单卡-开发目标.md
git commit -m "docs(收银): 订单卡改造开发目标 —— 十节齐全，本版不做的照 spec 收口"
```

---

### Task 2: 迁移 m007 —— sale_entries 补 5 列

**Files:**
- Modify: `src/storage/migrate.py`（`_m006` 之后加 `_m007`，`MIGRATIONS` 列表加一行）
- Test: `tests/test_cashier.py`（文末 `Test迁移新列`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_cashier.py` 文末追加（`import sqlite3` 已在文件头）：

```python
# ────────────────────────────────────── 迁移：SN/配件/支付/来源/排除 列
class Test迁移新列(_RootCase):
    def test_五列都在_老行读出来兜底(self):
        path = store.ensure(self.root)
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sale_entries)")}
        # 模拟"迁移前写进去的老行"：只给老列，新列是 NULL
        conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, created_at, updated_at)"
            " VALUES ('2026-09-30 10:00:00','c1','老货',1,99,'小张','',"
            " 'manual','2026-09-30 10:00:00','2026-09-30 10:00:00')")
        conn.commit()
        conn.close()
        self.assertTrue(
            {"sn", "accessories", "payments", "external_id", "excluded"} <= cols,
            "m007 没把新列补上：%s" % sorted(cols))
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["sn"], "")
        self.assertEqual(r["accessories"], [])
        self.assertEqual(r["payments"], [])
        self.assertEqual(r["external_id"], "")
        self.assertEqual(r["excluded"], 0)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test迁移新列 -q
```
Expected: FAIL（KeyError `'sn'` 或列断言失败 —— m007 还没写，且 `list_entries` 还没兜底）

- [ ] **Step 3: 实现 m007**

`src/storage/migrate.py`：`_m006` 函数之后追加：

```python
def _m007(conn) -> None:
    """收银流水补列（2026-09-30 订单卡改造）—— SN / 配件 / 组合支付 / 玲珑来源。

    ⚠ `external_id` 上建唯一索引（玲珑 `document_no`）：SQLite 的 UNIQUE 索引
      **放行多个 NULL** ⇒ 手工单（external_id 空）互不冲突，玲珑单天然幂等。
    ⚠ `excluded` 不给 DEFAULT（ALTER 加列 + 非空默认在老 SQLite 上有坑）——
      读侧一律 `excluded IS NULL OR =0` 兜底（`list_entries` 负责）。
    """
    conn.execute("ALTER TABLE sale_entries ADD COLUMN sn TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN accessories TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN payments TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN external_id TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN excluded INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sale_entries_external"
                 " ON sale_entries(external_id)")
```

`MIGRATIONS` 列表末尾加：

```python
    Migration(n=7, name="收银流水补列 SN/配件/支付/玲珑", apply=_m007),
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test迁移新列 tests/test_migrate.py tests/test_profit_table.py -q
```
Expected: PASS（migrate/profit_table 那两条钉「编号不重不倒」的也必须绿）

- [ ] **Step 5: Commit**

```bash
git add src/storage/migrate.py tests/test_cashier.py
git commit -m "feat(收银): 迁移 m007 —— sale_entries 补 SN/配件/支付/external_id/excluded 五列"
```

---

### Task 3: store 字段存取 —— _clean_entry / save_entry / list_entries

**Files:**
- Modify: `src/features/cashier/store.py`（`import json`、`_clean_entry`、`save_entry`、`list_entries`）
- Test: `tests/test_cashier.py`（`Test新字段存取`）

- [ ] **Step 1: 写失败测试**

```python
# ────────────────────────────────────── 新字段：SN / 配件 / 支付
class Test新字段存取(_RootCase):
    def test_存改查一条龙(self):
        body = {
            "sold_at": "2026-09-30 14:32", "goods_code": "6901", "goods_name": "MatePad",
            "quantity": 1, "amount": 1899, "seller": "张三", "note": "老客户",
            "sn": "HXR123",
            "accessories": [{"name": "原装保护壳", "amount": 199}],
            "payments": [{"method": "现金", "amount": 1000},
                          {"method": "微信直连", "amount": 899}],
        }
        res = store.save_entry(self.root, body)
        self.assertTrue(res.get("ok"), res)
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["sn"], "HXR123")
        self.assertEqual(r["accessories"],
                         [{"name": "原装保护壳", "amount": 199.0}])
        self.assertEqual([p["method"] for p in r["payments"]], ["现金", "微信直连"])
        # 改
        body.update({"id": r["id"], "sn": "HXR456", "accessories": []})
        self.assertTrue(store.save_entry(self.root, body, entry_id=r["id"]).get("ok"))
        r2 = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r2["sn"], "HXR456")
        self.assertEqual(r2["accessories"], [])

    def test_坏明细回why不抛(self):
        base = {"sold_at": "2026-09-30 14:32", "amount": 100}
        for bad, frag in (
                ({"accessories": [{"amount": 5}]}, "名字"),
                ({"accessories": [{"name": "壳", "amount": "abc"}]}, "数字"),
                ({"payments": [{"method": "现金", "amount": -1}]}, "负数"),
                ({"payments": "不是列表"}, "列表")):
            with self.subTest(frag=frag):
                res = store.save_entry(self.root, dict(base, **bad))
                self.assertFalse(res.get("ok"))
                self.assertIn(frag, res.get("why", ""))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test新字段存取 -q
```
Expected: FAIL（新字段没存进去 / why 断言失败）

- [ ] **Step 3: 实现**

`src/features/cashier/store.py`：

1. 文件头 `import time` 附近加 `import json`。
2. `_clean_entry` 里 `now = ...` 之前加：

```python
    sn = str(data.get("sn") or "").strip()
    acc, why = _clean_items(data.get("accessories"), "配件", "name")
    if why:
        return None, why
    pay, why = _clean_items(data.get("payments"), "支付", "method")
    if why:
        return None, why
```

3. `_clean_entry` 返回的 dict 里追加三个键（跟在 `"source"` 后面）：

```python
        "sn": sn,
        "accessories": json.dumps(acc, ensure_ascii=False),
        "payments": json.dumps(pay, ensure_ascii=False),
```

4. `_clean_entry` 之前加明细校验函数：

```python
def _clean_items(val, what: str, name_key: str):
    """配件/支付明细 `[{名, 金额}]` → `(list, None)`；坏的回 `(None, why)`。

    金额两位小数、不许负；名字不许空。**不校验支付加总**（用户定：软提醒不拦截）。
    """
    if val in (None, ""):
        return [], None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            return None, "%s不是合法 JSON" % what
    if not isinstance(val, list):
        return None, "%s得是列表" % what
    out = []
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            return None, "%s第 %d 条不是对象" % (what, i + 1)
        name = str(item.get(name_key) or "").strip()
        if not name:
            return None, "%s第 %d 条没有名字" % (what, i + 1)
        try:
            amt = round(float(item.get("amount")), 2)
        except (TypeError, ValueError):
            return None, "%s「%s」的金额得是数字" % (what, name)
        if amt < 0:
            return None, "%s「%s」的金额不能是负数" % (what, name)
        out.append({name_key: name, "amount": amt})
    return out, None
```

5. `save_entry` 的 UPDATE 与 INSERT 都带上三列 —— UPDATE 改为：

```python
                cur = conn.execute(
                    "UPDATE sale_entries SET sold_at=?, goods_code=?, goods_name=?,"
                    " quantity=?, amount=?, seller=?, note=?, source=?, sn=?,"
                    " accessories=?, payments=?, updated_at=?"
                    " WHERE id=?",
                    (row["sold_at"], row["goods_code"], row["goods_name"],
                     row["quantity"], row["amount"], row["seller"], row["note"],
                     row["source"], row["sn"], row["accessories"],
                     row["payments"], now, int(entry_id)))
```

INSERT 改为：

```python
        cur = conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, sn, accessories, payments,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["sold_at"], row["goods_code"], row["goods_name"], row["quantity"],
             row["amount"], row["seller"], row["note"], row["source"], row["sn"],
             row["accessories"], row["payments"], now, now))
```

6. `list_entries` 加排除过滤 + 出口归一。在函数上方加：

```python
def _row_out(r) -> dict:
    """行 → 给界面的形状：JSON 列拆开、NULL 兜底（老行没有新列的值）。"""
    d = dict(r)
    d["sn"] = d.get("sn") or ""
    d["external_id"] = d.get("external_id") or ""
    d["excluded"] = int(d.get("excluded") or 0)
    for k in ("accessories", "payments"):
        try:
            v = json.loads(d.get(k) or "[]")
        except ValueError:
            v = []
        d[k] = v if isinstance(v, list) else []
    return d
```

`list_entries` 两条 SQL 都改成（**排除软排除的行**）：

```python
        if day:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE (excluded IS NULL OR excluded=0) AND sold_at LIKE ?"
                " ORDER BY sold_at DESC, id DESC", (str(day).strip() + "%",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE excluded IS NULL OR excluded=0"
                " ORDER BY sold_at DESC, id DESC").fetchall()
        return [_row_out(r) for r in rows]
```

- [ ] **Step 4: 跑测试确认通过（连同既有流水测试）**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
```
Expected: PASS（含 Task 2 的迁移测试与原有全部收银测试）

- [ ] **Step 5: Commit**

```bash
git add src/features/cashier/store.py tests/test_cashier.py
git commit -m "feat(收银): 流水存 SN/配件/组合支付 —— 明细校验回 why，读侧 JSON 拆开+NULL 兜底"
```

---

### Task 4: 黑名单设置存取（`.secrets/cashier-import.json`）

**Files:**
- Create: `src/features/cashier/import_cfg.py`
- Test: `tests/test_cashier.py`（`Test导入黑名单设置`）

- [ ] **Step 1: 写失败测试**

```python
# ────────────────────────────────────── 导入黑名单（本机设置）
class Test导入黑名单设置(_RootCase):
    def test_默认空_存读往返(self):
        from src.features.cashier import import_cfg
        self.assertEqual(import_cfg.load(self.root), [])
        self.assertTrue(import_cfg.save(self.root, ["样机", "展示机", "样机"]))
        self.assertEqual(import_cfg.load(self.root), ["样机", "展示机"])

    def test_空串过滤_坏文件当空(self):
        from src.features.cashier import import_cfg
        import_cfg.save(self.root, ["  ", "已退货", ""])
        self.assertEqual(import_cfg.load(self.root), ["已退货"])
        p = self.root / ".secrets" / "cashier-import.json"
        p.write_text("{{{", encoding="utf-8")
        self.assertEqual(import_cfg.load(self.root), [], "坏文件当没设置，不抛")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test导入黑名单设置 -q
```
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**

新建 `src/features/cashier/import_cfg.py`：

```python
# -*- coding: utf-8 -*-
"""玲珑导入的本机设置 —— `.secrets/cashier-import.json`（这台机器自己的）。

⚠ 放 `.secrets/`：selfupdate.NEVER_TOUCH 有它，升级不带走门店的黑名单。
⚠ 坏文件当"没设置"（回 []）—— 黑名单坏了不该让导入按钮整个报错；
   丢了顶多多导几单，人工删一下就行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from ...paths import ROOT

REL = ".secrets/cashier-import.json"


def path(root=None) -> Path:
    return Path(root if root is not None else ROOT) / REL


def load(root=None) -> List[str]:
    p = path(root)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    out = []
    for w in (raw.get("blacklist") or []):
        w = str(w).strip()
        if w and w not in out:
            out.append(w)
    return out


def save(root=None, words=None) -> dict:
    clean = []
    for w in (words or []):
        w = str(w).strip()
        if w and w not in clean:
            clean.append(w)
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"blacklist": clean}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "blacklist": clean}


__all__ = ["REL", "path", "load", "save"]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test导入黑名单设置 -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/features/cashier/import_cfg.py tests/test_cashier.py
git commit -m "feat(收银): 导入黑名单设置 —— .secrets/cashier-import.json，坏文件当没设置"
```

---

### Task 5: 玲珑卡软排除 + 导入生成流水

**Files:**
- Modify: `src/features/cashier/store.py`（新增 `exclude_entry`、`entries_from_orders`）
- Test: `tests/test_cashier.py`（`Test排除与导入`）

- [ ] **Step 1: 写失败测试**

```python
# ────────────────────────────────────── 软排除 / 玲珑导入生成流水
def _seed_order(conn, dn, day="2026-09-30", remark="", name="MatePad Air",
                qty=1, sn="HXR001", amount=1899.0, guide="张三"):
    """直接喂 orders / order_lines 两行（导入的读侧只认这两张表）。"""
    conn.execute(
        "INSERT INTO orders (document_no, doc_create_time, included_tax_amount,"
        " remark, consumer_guide_name) VALUES (?,?,?,?,?)",
        (dn, "%s 14:32:00" % day, amount, remark, guide))
    conn.execute(
        "INSERT INTO order_lines (document_no, line_no, sn, ean, item_name,"
        " quantity) VALUES (?,?,?,?,?,?)", (dn, 1, sn, "6901234567890", name, qty))


class Test排除与导入(_RootCase):
    def _mk_orders_table(self, path):
        # orders/order_lines 是 dump.SCHEMA 建的（m001 之前就存在），测试里手动补
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS orders (document_no TEXT PRIMARY KEY,"
                     " doc_create_time TEXT, included_tax_amount REAL, remark TEXT,"
                     " consumer_guide_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS order_lines (document_no TEXT,"
                     " line_no INTEGER, sn TEXT, ean TEXT, item_name TEXT,"
                     " quantity REAL)")
        conn.commit()
        conn.close()

    def test_导入生成玲珑卡_再导不重复(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN001")
        _seed_order(conn, "DN002", name="Watch GT5", qty=2, sn="HXR002")
        conn.commit()
        conn.close()
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res["ok"], res["imported"]), (True, 2))
        rows = store.list_entries(self.root, day="2026-09-30")
        self.assertEqual(len(rows), 2)
        r = {x["external_id"]: x for x in rows}["DN001"]
        self.assertEqual((r["source"], r["sn"], r["seller"]),
                         ("linglong", "HXR001", "张三"))
        self.assertEqual(r["amount"], 1899.0)
        # 再导一次：幂等
        res2 = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res2["imported"], res2["skipped_dup"]), (0, 2))

    def test_黑名单命中的单不入卡(self):
        from src.features.cashier import import_cfg
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN003", remark="样机勿售")
        _seed_order(conn, "DN004", remark="正常单")
        conn.commit()
        conn.close()
        import_cfg.save(self.root, ["样机"])
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["skipped_blacklist"], 1)
        ids = [x["external_id"] for x in store.list_entries(self.root, day="2026-09-30")]
        self.assertEqual(ids, ["DN004"])

    def test_多行单聚合_名称等N件编码拼接(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN005")
        conn.execute("INSERT INTO order_lines (document_no, line_no, sn, ean,"
                     " item_name, quantity) VALUES (?,?,?,?,?,?)",
                     ("DN005", 2, "HXR002", "69999", "碎屏险", 1))
        conn.commit()
        conn.close()
        store.entries_from_orders(self.root, "2026-09-30")
        r = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertEqual(r["quantity"], 2)
        self.assertIn("等2件", r["goods_name"])
        self.assertIn("6901234567890", r["goods_code"])
        self.assertIn("69999", r["goods_code"])
        self.assertEqual(r["sn"], "HXR001|HXR002")

    def test_软排除后列表不见_再导不回来_手工单删不掉排除标记依赖(self):
        path = store.ensure(self.root)
        self._mk_orders_table(path)
        import sqlite3
        conn = sqlite3.connect(str(path))
        _seed_order(conn, "DN006")
        conn.commit()
        conn.close()
        store.entries_from_orders(self.root, "2026-09-30")
        row = store.list_entries(self.root, day="2026-09-30")[0]
        self.assertTrue(store.exclude_entry(self.root, row["id"]).get("ok"))
        self.assertEqual(store.list_entries(self.root, day="2026-09-30"), [])
        res = store.entries_from_orders(self.root, "2026-09-30")
        self.assertEqual((res["imported"], res["skipped_dup"]), (0, 1),
                         "已排除的 external_id 也要挡住重新导入")
        # 手工单不允许走软排除（语义分叉：手工 ✕ = 真删）
        mid = store.save_entry(self.root,
                               {"sold_at": "2026-09-30 10:00", "amount": 1})["id"]
        bad = store.exclude_entry(self.root, mid)
        self.assertFalse(bad.get("ok"))
        self.assertIn("手工", bad.get("why", ""))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test排除与导入 -q
```
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现**

`src/features/cashier/store.py`：

1. 文件顶部 `from ... import dump as _dump` 旁加 `from . import import_cfg`。
2. `delete_entry` 之后加：

```python
def exclude_entry(root=None, entry_id=None) -> dict:
    """玲珑卡的 ✕ —— **软排除**（记标记，不删 dump 库的原单，再导入也不回来）。

    手工单（source=manual）不许走这儿：它的 ✕ 是真删（`delete_entry`）。
    """
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "id 不对"}
    with _db.tx(str(ensure(root))) as conn:
        row = conn.execute("SELECT source FROM sale_entries WHERE id=?",
                           (eid,)).fetchone()
        if not row:
            return {"ok": False, "why": "没有这条流水（id=%s）" % eid}
        if (row[0] or "manual") != "linglong":
            return {"ok": False, "why": "手工流水请用删除，不是排除"}
        conn.execute("UPDATE sale_entries SET excluded=1, updated_at=? WHERE id=?",
                     (datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), eid))
        return {"ok": True, "id": eid}
```

3. `sellers` 函数之后加（`entries_from_orders` —— 只读本机库，不碰网络）：

```python
def entries_from_orders(root=None, day: str = "") -> dict:
    """把 `orders` 表里那天的销售单变成当日卡片（`source='linglong'`）。

    ⚠ **只读本机库**：拉网络在 web.App.cashier_import 那层做（先合并 orders，
      再调这里）—— 这样本函数纯读、零网络，好测。
    ⚠ 过滤两道：① 备注命中黑名单（`import_cfg.load`）跳过；
      ② `external_id` 已存在（**含软排除的** —— excluded 行不删）跳过 ⇒ 幂等。
    ⚠ **单据一卡**：多行聚合（数量=Σ、名称=首行+等N件、编码/SN 拼接）。
    """
    day = str(day or "").strip()
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return {"ok": False, "why": "日期格式不对（要 2026-09-30）"}
    path = ensure(root)
    blacklist = import_cfg.load(root)
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    imported = skipped_black = skipped_dup = 0
    conn = _db.open_db(str(path))
    try:
        try:
            orders = conn.execute(
                "SELECT document_no, doc_create_time, included_tax_amount, remark,"
                " consumer_guide_name FROM orders WHERE doc_create_time LIKE ?"
                " ORDER BY doc_create_time",
                (day + "%",)).fetchall()
        except Exception:                                   # noqa: BLE001
            return {"ok": False, "why": "还没有 orders 表（先点一次导入拉单）"}
        have = {r[0] for r in conn.execute(
            "SELECT external_id FROM sale_entries"
            " WHERE external_id IS NOT NULL AND external_id != ''").fetchall()}
        for o in orders:
            dn = o["document_no"]
            remark = str(o["remark"] or "")
            if any(w and w in remark for w in blacklist):
                skipped_black += 1
                continue
            if dn in have:
                skipped_dup += 1
                continue
            lines = conn.execute(
                "SELECT sn, ean, item_name, quantity FROM order_lines"
                " WHERE document_no=? ORDER BY line_no", (dn,)).fetchall()
            qty = sum(float(ln["quantity"] or 0) for ln in lines) or 1
            names = [str(ln["item_name"] or "") for ln in lines if ln["item_name"]]
            name = (names[0] if names else "") + (
                " 等%d件" % len(lines) if len(lines) > 1 else "")
            codes = [str(ln["ean"] or "") for ln in lines if ln["ean"]]
            sns = [str(ln["sn"] or "") for ln in lines if ln["sn"]]
            conn.execute(
                "INSERT INTO sale_entries (sold_at, goods_code, goods_name,"
                " quantity, amount, seller, note, source, sn, external_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["doc_create_time"], "|".join(codes), name.strip(),
                 qty, float(o["included_tax_amount"] or 0),
                 str(o["consumer_guide_name"] or ""), remark,
                 "linglong", "|".join(sns), dn, now, now))
            have.add(dn)
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": imported,
            "skipped_blacklist": skipped_black, "skipped_dup": skipped_dup}
```

4. `__all__` 加 `"exclude_entry", "entries_from_orders"`。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/features/cashier/store.py tests/test_cashier.py
git commit -m "feat(收银): 玲珑卡软排除 + 按天导入生成流水 —— 黑名单过滤/单据一卡/external_id 幂等"
```

---

### Task 6: 后端接口 —— 导入 / 排除 / 黑名单读写

**Files:**
- Modify: `src/web.py`（`App` 里加 `cashier_import` / `cashier_exclude` / `cashier_import_settings`；`/api/cashier` 路由块加三路）
- Test: `tests/test_cashier.py`（`Test导入接口`）

- [ ] **Step 1: 写失败测试**

```python
# ────────────────────────────────────── 导入 / 排除 / 黑名单 接口
class Test导入接口(_RootCase):
    def setUp(self):
        super().setUp()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_黑名单读写往返(self):
        st, d = self.srv.request("PUT", "/api/cashier/import-settings",
                                 {"blacklist": ["样机", " 展示 "]})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/cashier/import-settings")
        self.assertEqual((st, d["blacklist"]), (200, ["样机", "展示"]))

    def test_导入把网络那步mock掉_只验编排(self):
        def fake_import(self, body):
            return {"ok": True, "day": body.get("day"),
                    "imported": 3, "skipped_blacklist": 1, "skipped_dup": 2}
        with mock.patch.object(web.App, "cashier_import", fake_import):
            st, d = self.srv.request("POST", "/api/cashier/import",
                                     {"day": "2026-09-30"})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["imported"], d["skipped_blacklist"], d["skipped_dup"]),
                         (3, 1, 2))

    def test_导入日期不对_400带error(self):
        with mock.patch.object(web.App, "cashier_import",
                               lambda self, b: {"ok": False, "why": "日期格式不对"}):
            st, d = self.srv.request("POST", "/api/cashier/import", {"day": "x"})
        self.assertEqual(st, 400)
        self.assertIn("日期", d["error"])

    def test_排除接口_软排除走通(self):
        store.save_entry(self.root, {"sold_at": "2026-09-30 10:00",
                                     "amount": 1, "source": "linglong"})
        _, rows = self.srv.request("GET", "/api/cashier/entries?day=2026-09-30")
        eid = rows["rows"][0]["id"]
        st, d = self.srv.request("POST", "/api/cashier/exclude", {"id": eid})
        self.assertEqual(st, 200, d)
        _, rows2 = self.srv.request("GET", "/api/cashier/entries?day=2026-09-30")
        self.assertEqual(rows2["rows"], [])

    def test_区长平台照样403(self):
        with mock.patch.object(web, "role_scope", lambda _a: _scope("manager")):
            st, d = self.srv.request("POST", "/api/cashier/import",
                                     {"day": "2026-09-30"})
        self.assertEqual(st, 403)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_cashier.py::Test导入接口 -q
```
Expected: FAIL（404 接口不存在 / 405）

- [ ] **Step 3: 实现 App 方法**

`src/web.py` 的 `App` 类里，`cashier_policy_refresh` 之后加：

```python
    def cashier_import(self, body: dict) -> dict:
        """一键导入：拉**当天**玲珑销售单合并进库，再生成当日卡片。

        两段分开的原因：合并 orders 是网络+写库（慢、会话会掉），
        生成卡片是纯读库（幂等、好测）—— 中途失败重按一次即可（external_id 挡重）。
        会话掉了把 CbgAuthError 的原文丢出去（跟其它 CBG 接口同一口径）。
        """
        from . import dump as _dump
        from .features.cashier import store as cashier_store
        day = str((body or {}).get("day") or "").strip()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return {"ok": False, "why": "日期格式不对（要 2026-09-30）"}
        path = cashier_store.ensure(self.root)
        cfg = config_io.load_raw(self.config_path)
        start, end = _day_window(day)
        conn = _dump.connect(path)
        try:
            conn.executescript(_dump.SCHEMA)
            client = self.cbg_client()
            _dump.load_orders(conn, client, cfg.get("store_code") or None,
                              start, end, verbose=False)
            conn.commit()
        except (CbgAuthError, CbgError) as e:
            return {"ok": False, "why": str(e) or "会话失效，先抓一次会话"}
        except Exception as e:                                     # noqa: BLE001
            return {"ok": False,
                    "why": str(e) or ("%s: %s" % (type(e).__name__, e))}
        finally:
            conn.close()
        return cashier_store.entries_from_orders(self.root, day)

    def cashier_exclude(self, entry_id) -> dict:
        from .features.cashier import store as cashier_store
        return cashier_store.exclude_entry(self.root, entry_id)

    def cashier_import_settings(self, body: dict = None, save: bool = False) -> dict:
        from .features.cashier import import_cfg
        if save:
            words = (body or {}).get("blacklist")
            if not isinstance(words, list):
                return {"ok": False, "why": "blacklist 得是列表"}
            return import_cfg.save(self.root, words)
        return {"ok": True, "blacklist": import_cfg.load(self.root)}
```

`src/web.py` 顶部工具函数区（`_shown_step_cmds` 附近）加日期窗口助手：

```python
def _day_window(day: str) -> tuple:
    """`YYYY-MM-DD` → 那天 CST 的 [start, end] 秒级时间戳（照 dump 的窗口口径）。"""
    import datetime as _dt
    d = _dt.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    return int(d.timestamp()), int((d + _dt.timedelta(days=1)).timestamp()) - 1
```

⚠ 确认 `src/web.py` 文件头是否已 `import datetime`（`grep -n "^import datetime" src/web.py`）——若已有则 `_day_window` 直接用，不留局部 import。

- [ ] **Step 4: 加路由**

`src/web.py` `/api/cashier` 路由块内、`policy-refresh` 分支之后、404 兜底之前加：

```python
            if path == "/api/cashier/import" and method == "POST":
                res = app.cashier_import(self._read_json() or {})
                if not res.get("ok"):
                    return self._json(dict(res, error=res.get("why") or "导入失败"), 400)
                return self._json(res)
            if path == "/api/cashier/exclude" and method == "POST":
                res = app.cashier_exclude((self._read_json() or {}).get("id"))
                if not res.get("ok"):
                    return self._json(dict(res, error=res.get("why") or "排除失败"), 400)
                return self._json(res)
            if path == "/api/cashier/import-settings" and method == "GET":
                return self._json(app.cashier_import_settings())
            if path == "/api/cashier/import-settings" and method == "PUT":
                res = app.cashier_import_settings(self._read_json() or {}, save=True)
                if not res.get("ok"):
                    return self._json(dict(res, error=res.get("why") or "保存失败"), 400)
                return self._json(res)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/web.py tests/test_cashier.py
git commit -m "feat(收银): 导入/软排除/黑名单三接口 —— 拉单合并与生成卡片分两段，403 沿用前缀门禁"
```

---

### Task 7: HTML 重排 —— 吸顶录入 + 汇总条 + 卡片容器

**Files:**
- Modify: `web/index.html`（`#subpanel-cashier` 整段重写，约 956-996 行）
- Test: `tests/test_cashier.py`（`Test页面接线` 补新 id 断言）

- [ ] **Step 1: 写失败测试**

`tests/test_cashier.py` 的 `Test页面接线` 类里，在 `test_表单关键控件在` 之后加：

```python
    def test_订单卡新结构的控件在(self):
        """2026-09-30 改版：吸顶录入 + 汇总条 + 卡片容器 + 导入/黑名单入口。"""
        for cid in ("cashier-scan", "cashier-summary", "cashier-count",
                    "cashier-qty-sum", "cashier-amount-sum", "cashier-acc-sum",
                    "cashier-cards", "cashier-import", "cashier-blacklist-open",
                    "cashier-blacklist-row", "cashier-blacklist-input",
                    "cashier-blacklist-save", "cashier-sn", "cashier-cancel"):
            with self.subTest(id=cid):
                self.assertIn('id="%s"' % cid, INDEX_HTML)
        # 吸顶容器：录入区和汇总条都得在 .cashier-top 里
        i = INDEX_HTML.index('id="subpanel-cashier"')
        blk = INDEX_HTML[i:i + 4000]
        self.assertIn('class="cashier-top"', blk)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_订单卡新结构的控件在" -q
```
Expected: FAIL

- [ ] **Step 3: 重写 HTML**

`web/index.html` 把 `#subpanel-cashier` 的两个 card 整段替换为：

```html
<div class="subpanel" id="subpanel-cashier">
  <!-- 吸顶区：录入 + 汇总（滚动卡片列表时这两段钉在顶上） -->
  <div class="cashier-top">
    <div class="card">
      <div class="card-head">
        <h2>收银录入 <span class="hint" id="cashier-meta">政策表：读取中…</span></h2>
        <div class="form-row" style="margin:0">
          <button class="btn ghost small" id="cashier-refresh"
                  title="从华为 pmall 拉最新「价格与返利政策」；会话掉了会弹窗要人工登录">刷新政策数据</button>
        </div>
      </div>
      <div class="cashier-scan">
        <input id="cashier-scan" placeholder="扫码或输入商品编码，回车反查"
               autocomplete="off">
        <button class="btn ghost" id="cashier-scan-ok">确定</button>
      </div>
      <div class="form-row">
        <label>销售时间 <input type="datetime-local" id="cashier-sold-at"></label>
        <label>商品名称 <input id="cashier-name" placeholder="编码查不到就手输"></label>
        <label>数量 <input type="number" id="cashier-qty" value="1" min="0.01" step="any"
                      style="width:5.5em"></label>
        <label>实收金额 <input type="number" id="cashier-amount" step="0.01"
                        placeholder="手动匹配" style="width:8em"></label>
        <label>SN <input id="cashier-sn" placeholder="可空" style="width:10em"></label>
        <label>销售员 <input id="cashier-seller" list="cashier-sellers"
                        placeholder="选一个或新输">
          <datalist id="cashier-sellers"></datalist></label>
        <label>备注 <input id="cashier-note" placeholder="可空" style="width:11em"></label>
        <button class="btn primary" id="cashier-save">保存一笔</button>
        <button class="btn ghost" id="cashier-cancel" hidden>取消修改</button>
      </div>
    </div>

    <div class="card cashier-summary" id="cashier-summary">
      <span class="cs-date" id="cashier-day-label"></span>
      <span>今日 <b id="cashier-count">0</b> 笔</span>
      <span><b id="cashier-qty-sum">0</b> 件</span>
      <span>实收 ¥<b id="cashier-amount-sum">0.00</b></span>
      <span>配件 ¥<b id="cashier-acc-sum">0.00</b></span>
      <span class="cs-gap"></span>
      <button class="btn primary small" id="cashier-import"
              title="拉当天玲珑销售单合并入库并生成卡片（已导过的不会重复）">导入玲珑单</button>
      <button class="btn ghost small" id="cashier-blacklist-open">排除关键词</button>
      <input type="date" id="cashier-day" title="看哪天的流水">
    </div>
    <div class="card cashier-blacklist" id="cashier-blacklist-row" hidden>
      <label>备注含这些词的单不进卡（逗号分隔）
        <input id="cashier-blacklist-input" placeholder="样机, 展示机"
               style="width:28em"></label>
      <button class="btn small" id="cashier-blacklist-save">保存</button>
      <span class="hint" id="cashier-blacklist-hint"></span>
    </div>
  </div>

  <!-- 滚动区：每笔订单一张卡 -->
  <div id="cashier-cards" class="cashier-cards">
    <p class="hint">加载中…</p>
  </div>
  <!-- ⚠ 旧表格容器 id 保留为空占位：tests/test_cashier.py 的老断言与
       test_web_ia 的 nav 对照都还认它，删了会红 -->
  <div id="cashier-table" hidden></div>
</div>
```

⚠ **不要删旧 id**：`cashier-code`（原扫码框）改成 `cashier-scan` 了——所以同时把
老断言 `test_表单关键控件在` 里的 `"cashier-code"` 改成 `"cashier-scan"`，并把
`web/app.js` 里所有 `$('#cashier-code')` 改名为 `$('#cashier-scan')`（Task 9 统一改，
这一步先让 HTML/测试对上）。**本步只改测试断言里的这一个名字，JS 留给 Task 9**
—— 所以本步跑测试时 JS 还没改，`test_扫码枪回车直接进金额`（只查字符串）仍绿。

- [ ] **Step 4: 同步改老断言里的 id 名**

`tests/test_cashier.py` `test_表单关键控件在` 列表里 `"cashier-code"` → `"cashier-scan"`。

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线" -q
```
Expected: PASS（新旧断言都绿）

- [ ] **Step 6: Commit**

```bash
git add web/index.html tests/test_cashier.py
git commit -m "feat(收银): 页面重排为吸顶录入+汇总条+卡片容器 —— 旧 id 保留，扫码框改名 cashier-scan"
```

---

### Task 8: CSS —— 吸顶、卡片两区、参考图风格分组标题、支付方块

**Files:**
- Modify: `web/style.css`（文末追加收银段）
- Test: `tests/test_cashier.py`（`Test页面接线` 加样式断言）

- [ ] **Step 1: 写失败测试**

```python
    def test_收银订单卡样式段在(self):
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        for token in (".cashier-top", ".cashier-card", ".cc-side",
                      ".cc-group-title", ".pay-block", ".cc-close"):
            with self.subTest(token=token):
                self.assertIn(token, css)
        self.assertIn("position: sticky",
                      css[css.index(".cashier-top"):css.index(".cashier-top") + 400])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_收银订单卡样式段在" -q
```
Expected: FAIL

- [ ] **Step 3: 实现 CSS**

`web/style.css` 文末追加：

```css
/* ──────────────────────── 收银 · 订单卡（2026-09-30） ────────────────────────
   布局照华为 POS 参考图：吸顶「录入+汇总」，下面每笔订单一张卡（左数据右信息栏）。 */
.cashier-top { position: sticky; top: 0; z-index: 20;
  background: var(--bg); padding-bottom: 6px; }
.cashier-scan { display: flex; gap: 8px; margin-bottom: 10px; }
.cashier-scan input { flex: 1; font-size: 15px; padding: 9px 12px; }
.cashier-summary { display: flex; align-items: center; gap: 16px;
  font-size: 13.5px; color: var(--muted); }
.cashier-summary b { color: var(--text); font-size: 15px; }
.cashier-summary .cs-date { font-weight: 650; color: var(--text); }
.cashier-summary .cs-gap { flex: 1; }
.cashier-blacklist { display: flex; align-items: center; gap: 10px; }
.cashier-blacklist label { min-width: 0; display: flex; align-items: center; gap: 8px; }

.cashier-cards { display: flex; flex-direction: column; gap: 12px; }
.cashier-card { display: flex; gap: 0; padding: 0; overflow: hidden;
  position: relative; }
.cc-main { flex: 1 1 auto; min-width: 0; padding: 14px 16px; }
.cc-time { font-size: 12.5px; color: var(--muted); }
.cc-name { font-size: 15.5px; font-weight: 650; margin: 2px 0 6px; }
.cc-meta { font-size: 12.5px; color: var(--muted); line-height: 1.7;
  word-break: break-all; }
.cc-amount-row { display: flex; justify-content: space-between; align-items: baseline;
  margin-top: 8px; }
.cc-amount-row .cc-qty { font-size: 13px; color: var(--muted); }
.cc-amount-row .cc-amount { font-size: 21px; font-weight: 700; }
.cc-acc { margin-top: 10px; border-top: 1px dashed var(--line);
  padding-top: 8px; font-size: 13px; }
.cc-acc-row { display: flex; justify-content: space-between; gap: 12px;
  padding: 2px 0; }
.cc-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
.cc-editing { outline: 2px solid var(--brand); }

/* 右侧信息栏 —— 参考图的蓝竖条分组标题 */
.cc-side { flex: 0 0 260px; border-left: 1px solid var(--line);
  padding: 12px 14px; background: var(--field); font-size: 13px; }
.cc-group + .cc-group { margin-top: 12px; }
.cc-group-title { font-weight: 650; margin-bottom: 6px; padding-left: 8px;
  border-left: 3px solid var(--brand); line-height: 1.2; }
.cc-row { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; }
.cc-row .k { color: var(--muted); flex: 0 0 auto; }
.cc-row .v { text-align: right; word-break: break-all; }
.cc-muted { color: var(--muted); }
.cc-wait { display: inline-block; font-size: 11.5px; border: 1px dashed var(--line);
  border-radius: 4px; padding: 1px 6px; margin-left: 6px; }

/* 支付方块（参考图彩色块）：块 + 下方金额框；软提醒不拦截 */
.pay-blocks { display: flex; flex-wrap: wrap; gap: 8px; }
.pay-block { position: relative; border: 1px solid var(--line); border-radius: 6px;
  padding: 6px 8px 6px; background: var(--card); min-width: 86px; }
.pay-block .pb-name { font-size: 12.5px; font-weight: 600; padding-right: 14px; }
.pay-block input { width: 76px; margin-top: 4px; }
.pay-block .pb-x { position: absolute; top: 2px; right: 3px; border: 0;
  background: none; cursor: pointer; color: var(--muted); font-size: 13px; }
.pay-block:nth-child(6n+1) { background: color-mix(in srgb, var(--brand) 10%, var(--card)); }
.pay-block:nth-child(6n+2) { background: #eef6ff; }
.pay-block:nth-child(6n+3) { background: #fff7e8; }
.pay-block:nth-child(6n+4) { background: #eefaf0; }
.pay-block:nth-child(6n+5) { background: #fdeeee; }
.pay-block:nth-child(6n+6) { background: #f3f0ff; }
.pay-sum { font-size: 12.5px; margin-top: 8px; color: var(--muted); }
.pay-warn { color: #b45309; background: #fffbeb; border: 1px solid #fde68a;
  border-radius: 5px; padding: 3px 8px; display: inline-block; margin-top: 6px; }

/* 卡右侧垂直居中的 ✕（两类卡统一入口，语义在 JS 分叉） */
.cc-close { position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  border: 0; background: none; font-size: 17px; line-height: 1; cursor: pointer;
  color: var(--muted); padding: 6px; }
.cc-close:hover { color: var(--bad); }
.cc-side { padding-right: 28px; }
```

⚠ `color-mix` 老 Chromium 不认 —— 门店浏览器版本未知。把 `nth-child(6n+1)` 那条
改成纯色 `#eef2ff`（与整体一致，去掉 color-mix）。**实现时直接写 `#eef2ff`，
不写 color-mix**。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线" -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/style.css tests/test_cashier.py
git commit -m "feat(收银): 订单卡样式 —— 吸顶区/两区卡片/蓝竖条分组标题/彩色支付方块/居中✕"
```

---

### Task 9: JS 卡片渲染 + 汇总条 + 扫码框改名

**Files:**
- Modify: `web/app.js`（收银段 358-600 行：`renderCashierTable` → `renderCashierCards`；`cashier-code` → `cashier-scan`；加汇总）
- Test: `tests/test_cashier.py`（页面接线断言）

- [ ] **Step 1: 写失败测试**

`Test页面接线` 加：

```python
    def test_卡片渲染和汇总接上了(self):
        for fn in ("renderCashierCards", "cashierRenderSummary",
                   "cashierCardHtml"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        self.assertNotIn("renderCashierTable", APP_JS, "旧表格渲染要删干净")
        self.assertNotIn("cashier-code", APP_JS, "扫码框改名没跟上")
        # 汇总四要素：笔数/件数/实收/配件 都有落点
        for cid in ("cashier-count", "cashier-qty-sum",
                    "cashier-amount-sum", "cashier-acc-sum"):
            self.assertIn("'%s'" % cid, APP_JS)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_卡片渲染和汇总接上了" -q
```
Expected: FAIL

- [ ] **Step 3: 实现 JS**

`web/app.js` 收银段改动清单（按顺序做）：

1. **全局改名**：段内所有 `$('#cashier-code')` → `$('#cashier-scan')`（约 4 处：
   `cashierResetForm` 清空列表、`cashierLookup`、`bindCashierEvents` 两处绑定）。
   `cashierLookup` 里的 `const codeEl = $('#cashier-code')` 同步改。
   用 `grep -n "cashier-code" web/app.js` 确认 0 处残留。

2. **删旧渲染，换卡片渲染**。`renderCashierTable` 整函数删除，替换为：

```js
function esc2(s) { return esc(s == null ? '' : String(s)); }

function cashierCardHtml(r) {
  const acc = (r.accessories || []);
  const accSum = acc.reduce((a, x) => a + (Number(x.amount) || 0), 0);
  const sold = String(r.sold_at || '').slice(5, 16);
  const editing = _cashierEditing === r.id;
  const src = r.source === 'linglong'
    ? '<span class="cc-wait" title="修改只影响本机视图，不动华为原单">玲珑 '
      + esc2(r.external_id || '') + '</span>' : '';
  const accHtml = acc.length
    ? '<div class="cc-acc"><b>配件</b>'
      + acc.map((x) => `<div class="cc-acc-row"><span>${esc2(x.name)}</span>`
        + `<span>¥${(Number(x.amount) || 0).toFixed(2)}</span></div>`).join('')
      + '</div>'
    : '';
  return `<div class="cashier-card card${editing ? ' cc-editing' : ''}" data-id="${r.id}">`
    + `<div class="cc-main">`
    + `<div class="cc-time">${esc2(sold)} ${src}</div>`
    + `<div class="cc-name">${esc2(r.goods_name) || '（没填名称）'}</div>`
    + `<div class="cc-meta">商品编码 ${esc2(r.goods_code) || '—'}`
    + `<br>SN ${esc2(r.sn) || '—'}</div>`
    + `<div class="cc-amount-row"><span class="cc-qty">数量 ${r.quantity}</span>`
    + `<span class="cc-amount">¥${(Number(r.amount) || 0).toFixed(2)}</span></div>`
    + accHtml
    + `<div class="cc-actions">`
    + `<button class="btn ghost small" data-cc-edit="${r.id}">改</button>`
    + `</div>`
    + `</div>`
    + `<div class="cc-side">`
    + `<div class="cc-group"><div class="cc-group-title">销售信息</div>`
    + `<div class="cc-row"><span class="k">销售员</span>`
    + `<span class="v">${esc2(r.seller) || '—'}</span></div>`
    + `<div class="cc-row"><span class="k">备注</span>`
    + `<span class="v">${esc2(r.note) || '—'}</span></div></div>`
    + `<div class="cc-group cc-muted"><div class="cc-group-title">客户信息</div>`
    + `会员/手机号<span class="cc-wait">待接入</span></div>`
    + `<div class="cc-group"><div class="cc-group-title">支付方式</div>`
    + cashierPayViewHtml(r)
    + `</div>`
    + `</div>`
    + `<button class="cc-close" data-cc-close="${r.id}" title="删除这笔">✕</button>`
    + `</div>`;
}

function cashierPayViewHtml(r) {
  const pays = (r.payments || []);
  if (!pays.length) return '<div class="cc-muted">未记录</div>';
  const sum = pays.reduce((a, x) => a + (Number(x.amount) || 0), 0);
  const amt = Number(r.amount) || 0;
  const warn = Math.abs(sum - amt) > 0.009
    ? `<div class="pay-warn">已付 ¥${sum.toFixed(2)} ≠ 实收 ¥${amt.toFixed(2)}</div>` : '';
  return '<div class="cc-row">'
    + pays.map((p) => `<span>${esc2(p.method)} ¥${(Number(p.amount) || 0).toFixed(2)}</span>`)
      .join('、')
    + '</div>' + warn;
}

function cashierRenderSummary(rows, day) {
  const n = rows.length;
  const qty = rows.reduce((a, r) => a + (Number(r.quantity) || 0), 0);
  const amt = rows.reduce((a, r) => a + (Number(r.amount) || 0), 0);
  const acc = rows.reduce((a, r) => a + (r.accessories || [])
    .reduce((b, x) => b + (Number(x.amount) || 0), 0), 0);
  const set = (id, v) => { const el = $('#' + id); if (el) el.textContent = v; };
  set('cashier-count', n);
  set('cashier-qty-sum', Math.round(qty * 100) / 100);
  set('cashier-amount-sum', amt.toFixed(2));
  set('cashier-acc-sum', acc.toFixed(2));
  const lab = $('#cashier-day-label');
  if (lab) lab.textContent = day || cashierToday();
}

function renderCashierCards(rows) {
  const host = $('#cashier-cards');
  _cashierRows = {};
  (rows || []).forEach((r) => { _cashierRows[r.id] = r; });
  if (!rows || !rows.length) {
    if (host) host.innerHTML = '<div class="empty">这天还没有流水</div>';
    return;
  }
  if (host) host.innerHTML = rows.map(cashierCardHtml).join('');
}
```

3. **反查提示行取消（spec：「反查提示没必要」）**：
   - `cashierLookup` 重写 —— 去掉对 `$('#cashier-lookup']` 的所有引用和价格/返利
     展示，成功只做「带出名称 + `focusAmount` 时跳实收」，失败 toast：

```js
async function cashierLookup(focusAmount) {
  const codeEl = $('#cashier-scan');
  const code = (codeEl.value || '').trim();
  if (!code) return;
  try {
    const d = await api('/api/cashier/lookup?code=' + encodeURIComponent(code));
    if (!d.found || !d.row) {
      toast(`编码 ${code} 不在政策表 —— 手输商品名也能存`, 'bad');
      return;
    }
    const name = String(d.row['商品名称'] || '');
    const nameEl = $('#cashier-name');
    if (name && (!nameEl.value || nameEl.value === _cashierAutoName)) {
      nameEl.value = name;
      _cashierAutoName = name;
    }
    if (focusAmount) $('#cashier-amount').focus();
  } catch (e) {
    toast('反查失败：' + e.message, 'bad');
  }
}
```

   - `cashierResetForm` 删掉 `$('#cashier-lookup')` 那段（hint 元素已不在 HTML）。
   - `bindCashierEvents`：回车绑定改到 `$('#cashier-scan')`（保持在函数最前，
     `cashierLookup(true)` 字符串位置不动，`test_扫码枪回车直接进金额` 才绿）；
     补「确定」按钮：`$('#cashier-scan-ok').addEventListener('click', () => cashierLookup(true));`

4. **`loadCashier` 里**：`renderCashierTable(d.rows || [])` 改为
   `renderCashierCards(d.rows || []); cashierRenderSummary(d.rows || [], cashierDayValue());`

4. **删掉旧表格的事件委托**（`bindCashierEvents` 里 `$('#cashier-table').addEventListener(...)`），
   换成卡片容器委托（**编辑/删除的动作本步先 `toast('下个任务接')` 占位是不允许的 —— 
   直接在本步写完整分发**，调用的 `cashierCardEdit` / `cashierCardClose` 在 Task 10/11 落地，
   所以**本步先写分发骨架，函数声明用 `function cashierCardEdit(id) {...}` 空实现并在
   Task 10/11 填充吗？不行（半成品）⇒ 调整顺序：本步与 Task 10 合并做的部分见 Step 5**。

- [ ] **Step 5: 处理事件分发（与 Task 10/11 的依赖）**

**依赖关系调整**：卡片点击分发需要 `cashierCardEdit`（Task 10）和 `cashierCardClose`
（Task 11）。为避免半成品，本任务的 Step 3 只做渲染/汇总/改名；`bindCashierEvents`
里旧表格委托先**删除**，卡片点击分发留到 Task 11 一次性接上（Task 10 的编辑态
渲染函数 `cashierCardEditHtml` 直接改 `renderCashierCards` 的输入）。
⚠ 这意味着 Task 9 完成后「改/删」按钮点了没反应 —— **测试照跑（接线测试只查字符串），
但 Task 9 与 Task 10/11 必须连续做完、最后一起验收**（见 Task 12 的手工验收清单）。

- [ ] **Step 6: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
.venv/bin/node --check web/app.js
```
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/app.js tests/test_cashier.py
git commit -m "feat(收银): 表格换订单卡渲染 + 汇总条四要素 + 扫码框改名 + 反查提示行取消改 toast"
```

---

### Task 10: JS 卡内编辑 —— 含配件区与新字段

**Files:**
- Modify: `web/app.js`
- Test: `tests/test_cashier.py`（页面接线断言）

- [ ] **Step 1: 写失败测试**

```python
    def test_卡内编辑接上了(self):
        for fn in ("cashierCardEdit", "cashierCardSave", "cashierCardCancel",
                   "cashierAccAdd", "cashierAccDel", "cashierEditState"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        # 卡内编辑读的是卡里的字段，不再回填顶部表单
        self.assertNotIn("cashierFill(", APP_JS, "回填顶部表单的老路要拆")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_卡内编辑接上了" -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

`web/app.js`：

1. **状态**：`let _cashierEditing = 0;` 语义不变（正在改的 id）；新增
   `let _cashierEditAcc = [];`（编辑中的配件草稿）。

2. **编辑态渲染**：`cashierCardHtml` 开头分叉 —— `editing` 时左区字段变输入框、
   底部按钮变「保存修改/取消」，右区销售员/备注变输入框，配件区可增删。
   实现为独立函数 `cashierCardEditHtml(r)`，`renderCashierCards` 里
   `editing ? cashierCardEditHtml(r) : cashierCardHtml(r)`（原 `cashierCardHtml`
   保留为视图态）。编辑态输入框统一 `data-f="字段名"`（`sold_at/goods_code/
   goods_name/quantity/amount/sn/seller/note`），配件行 `data-acc-i="下标"`。

```js
function cashierCardEditHtml(r) {
  const acc = _cashierEditAcc;
  const inp = (f, v, type, w) => `<input data-f="${f}" type="${type || 'text'}"`
    + ` value="${esc2(v == null ? '' : v)}"${w ? ` style="width:${w}"` : ''}>`;
  return `<div class="cashier-card card cc-editing" data-id="${r.id}">`
    + `<div class="cc-main">`
    + `<div class="cc-time">编辑中</div>`
    + `<div class="cc-name">${inp('goods_name', r.goods_name)}</div>`
    + `<div class="cc-meta">商品编码 ${inp('goods_code', r.goods_code)}`
    + `<br>SN ${inp('sn', r.sn)}</div>`
    + `<div class="cc-amount-row"><span>数量 ${inp('quantity', r.quantity, 'number', '5em')}</span>`
    + `<span>实收 ${inp('amount', r.amount, 'number', '8em')}</span></div>`
    + `<div class="cc-acc"><b>配件区</b>`
    + acc.map((x, i) => `<div class="cc-acc-row" data-acc-i="${i}">`
        + `<input data-acc-f="name" value="${esc2(x.name)}" placeholder="配件名">`
        + `<input data-acc-f="amount" type="number" step="0.01" value="${x.amount}">`
        + `<button class="btn ghost small" data-acc-del="${i}">删</button></div>`).join('')
    + `<button class="btn ghost small" data-acc-add="1">+ 添加配件</button></div>`
    + `<div class="cc-actions">`
    + `<button class="btn primary small" data-cc-save="${r.id}">保存修改</button>`
    + `<button class="btn ghost small" data-cc-cancel="1">取消</button>`
    + `</div></div>`
    + `<div class="cc-side">`
    + `<div class="cc-group"><div class="cc-group-title">销售信息</div>`
    + `<div class="cc-row"><span class="k">销售员</span>${inp('seller', r.seller)}</div>`
    + `<div class="cc-row"><span class="k">备注</span>${inp('note', r.note)}</div>`
    + `<div class="cc-row"><span class="k">销售时间</span>`
    + `${inp('sold_at', String(r.sold_at || '').slice(0, 16).replace(' ', 'T'), 'datetime-local')}</div>`
    + `</div>`
    + `<div class="cc-group cc-muted"><div class="cc-group-title">客户信息</div>`
    + `会员/手机号<span class="cc-wait">待接入</span></div>`
    + `<div class="cc-group"><div class="cc-group-title">支付方式</div>`
    + `<div class="pay-blocks" data-pay-blocks>`
    + (r.payments || []).map((p, i) => cashierPayEditBlock(p, i)).join('')
    + `</div><button class="btn ghost small" data-pay-add="1">+ 添加支付</button>`
    + `<div class="pay-sum" data-pay-sum></div></div>`
    + `</div>`
    + `<button class="cc-close" data-cc-close="${r.id}" title="删除这笔">✕</button>`
    + `</div>`;
}
```

3. **动作函数**：

```js
function cashierEditState() {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card) return null;
  const r = _cashierRows[card.dataset.id] || {};
  const out = { id: r.id, source: r.source, external_id: r.external_id };
  card.querySelectorAll('[data-f]').forEach((el) => { out[el.dataset.f] = el.value; });
  out.accessories = _cashierEditAcc;
  out.payments = cashierPayRead(card);
  return out;
}

function cashierCardEdit(id) {
  const r = _cashierRows[id];
  if (!r) return;
  _cashierEditing = id;
  _cashierEditAcc = JSON.parse(JSON.stringify(r.accessories || []));
  renderCashierCards(Object.values(_cashierRows));
}

function cashierCardCancel() {
  _cashierEditing = 0;
  _cashierEditAcc = [];
  renderCashierCards(Object.values(_cashierRows));
}

function cashierAccAdd() {
  _cashierEditAcc.push({ name: '', amount: 0 });
  renderCashierCards(Object.values(_cashierRows));
}

function cashierAccDel(i) {
  _cashierEditAcc.splice(Number(i), 1);
  renderCashierCards(Object.values(_cashierRows));
}

async function cashierCardSave(id) {
  const body = cashierEditState();
  if (!body) return;
  body.id = Number(id);
  if (!String(body.amount).trim()) {
    toast('实收金额还没填', 'bad');
    return;
  }
  try {
    await api('/api/cashier/entry-save', { method: 'POST', body });
    toast('已修改', 'good');
    _cashierEditing = 0;
    _cashierEditAcc = [];
    await loadCashier();
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
  }
}
```

⚠ `sold_at` 的 `datetime-local` 值是 `YYYY-MM-DDTHH:MM`，后端 `_clean_entry`
已做 `replace('T', ' ')`，直接传即可。
⚠ `cashierPayRead` / `cashierPayEditBlock` / `cashierPayAdd` / `cashierPayDel`
在 Task 11 实现 —— 本任务先把支付 DOM 结构与 `data-` 属性定下来（上面已定），
`cashierEditState` 调用的 `cashierPayRead` **在本步实现一个最小版**（读
`[data-pay-blocks]` 里的 `select/inputs`），避免半成品：

```js
function cashierPayRead(card) {
  const out = [];
  card.querySelectorAll('[data-pay-block]').forEach((b) => {
    const method = (b.querySelector('[data-pay-method]') || {}).value || '';
    const amount = Number((b.querySelector('[data-pay-amount]') || {}).value);
    if (method && amount) out.push({ method, amount });
  });
  return out;
}
```

4. **顶部表单分工**：`cashierFill` 函数**删除**（改在卡内）；`bindCashierEvents`
   里旧表格委托换成卡片委托（本步接编辑动作）：

```js
  $('#cashier-cards').addEventListener('click', (e) => {
    const t = e.target;
    if (t.dataset.ccEdit) { cashierCardEdit(t.dataset.ccEdit); return; }
    if (t.dataset.ccSave) { cashierCardSave(t.dataset.ccSave); return; }
    if (t.dataset.ccCancel) { cashierCardCancel(); return; }
    if (t.dataset.accAdd) { cashierAccAdd(); return; }
    if (t.dataset.accDel !== undefined && t.dataset.accDel !== '') {
      cashierAccDel(t.dataset.accDel); return;
    }
    // ⚠ data-cc-close / data-pay-* 的分发在 Task 11 才接（✕ 与支付块到那时才有反应）
  });
```

  ⚠ `cashierCardClose` 在 Task 11 才实现 —— **本步委托不含 `ccClose` 分支**
  （✕ 按钮到 Task 11 才有反应，同 Step 5 的连续交付约定；不要提前写
  `cashierRemove` 的临时桩 —— 语义分叉必须一次写对）。

5. **顶部表单**（新录一笔）保持原样，但 `cashierSave` 的 body 加
   `sn: ($('#cashier-sn').value || '').trim()`（`cashier-reset` 里清空 `cashier-sn`）。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
.venv/bin/node --check web/app.js
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.js tests/test_cashier.py
git commit -m "feat(收银): 卡内编辑（含配件增删/支付块草稿）—— 拆掉回填顶部的老路"
```

---

### Task 11: JS 组合支付块 + ✕ 删除语义分叉

**Files:**
- Modify: `web/app.js`
- Test: `tests/test_cashier.py`（页面接线断言）

- [ ] **Step 1: 写失败测试**

```python
    def test_支付块和删除分叉接上了(self):
        for fn in ("cashierPayEditBlock", "cashierPayAdd", "cashierPayDel",
                   "cashierPaySync", "cashierCardClose", "cashierPAY_METHODS"):
            with self.subTest(fn=fn):
                self.assertIn(fn, APP_JS)
        # 支付清单写死但存原始字符串（清单变动不坏老数据）
        self.assertIn("支付宝直连", APP_JS)
        self.assertIn("微信直连", APP_JS)
        # 删除语义分叉：manual → 删除确认；linglong → 软排除
        i = APP_JS.index("function cashierCardClose")
        blk = APP_JS[i:i + 700]
        self.assertIn("exclude", blk)
        self.assertIn("entry-delete", blk)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_支付块和删除分叉接上了" -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

`web/app.js`：

1. **支付方式清单（写死、存原始字符串）**：

```js
//: 支付方式块的名字（照用户给的彩色块图）。**存的是字符串不是枚举** ——
//: 以后增删名字不坏老数据（老行里的名字照原样显示，不在清单也能显示）。
const cashierPAY_METHODS = ['助手', 'C扫B', 'POS', '现金', '公对公',
  '付以旧换新(旧)', '付以旧换新(新)', '预收款', '企业微信', '支付宝直连', '微信直连'];

function cashierPayEditBlock(p, i) {
  const opts = cashierPAY_METHODS.map((m) => `<option value="${esc2(m)}"`
    + `${m === p.method ? ' selected' : ''}>${esc2(m)}</option>`).join('');
  return `<div class="pay-block" data-pay-block="${i}">`
    + `<button class="pb-x" data-pay-del="${i}" title="去掉这种方式">✕</button>`
    + `<div class="pb-name"><select data-pay-method>${opts}</select></div>`
    + `<input data-pay-amount type="number" step="0.01" placeholder="金额"`
    + ` value="${p.amount != null ? p.amount : ''}">`
    + `</div>`;
}

function cashierPayAdd() {
  const r = _cashierRows[_cashierEditing] || {};
  const pays = (r.payments || []).slice();
  // 编辑态的支付草稿跟配件一样放编辑态内存里 —— 复用 editState 的读取即可：
  // 每次增删都基于**卡里当前 DOM** 重读 → 追加 → 重渲
  const card = document.querySelector('.cashier-card.cc-editing');
  const cur = card ? cashierPayRead(card) : [];
  cur.push({ method: cashierPAY_METHODS[0], amount: '' });
  r.payments = cur;
  renderCashierCards(Object.values(_cashierRows));
}

function cashierPayDel(i) {
  const r = _cashierRows[_cashierEditing];
  if (!r) return;
  const card = document.querySelector('.cashier-card.cc-editing');
  const cur = card ? cashierPayRead(card) : [];
  cur.splice(Number(i), 1);
  r.payments = cur;
  renderCashierCards(Object.values(_cashierRows));
}

//: 软提醒：已付合计 ≠ 实收 ⇒ 黄条；**不拦截保存**（用户定）。
function cashierPaySync(card) {
  const r = _cashierRows[card.dataset.id] || {};
  const pays = cashierPayRead(card);
  const sum = pays.reduce((a, x) => a + (Number(x.amount) || 0), 0);
  const amt = Number(card.querySelector('[data-f="amount"]')
    ? card.querySelector('[data-f="amount"]').value : r.amount) || 0;
  const el = card.querySelector('[data-pay-sum]');
  if (!el) return;
  const warn = (pays.length && Math.abs(sum - amt) > 0.009)
    ? `<div class="pay-warn">已付 ¥${sum.toFixed(2)} ≠ 实收 ¥${amt.toFixed(2)}（仍可保存）</div>`
    : '';
  el.innerHTML = `已付 ¥${sum.toFixed(2)} / 应付 ¥${amt.toFixed(2)}` + warn;
}
```

2. **input/change 事件**：卡片委托上补 `input` 监听（单独绑一次）——
   金额或支付金额变化时调 `cashierPaySync(card)`：

```js
  $('#cashier-cards').addEventListener('input', (e) => {
    const card = e.target.closest && e.target.closest('.cashier-card.cc-editing');
    if (card) cashierPaySync(card);
  });
```

3. **卡片委托补分发**（加在 Task 10 的委托里）：

```js
    if (t.dataset.payAdd) { cashierPayAdd(); return; }
    if (t.dataset.payDel !== undefined && t.dataset.payDel !== '') {
      cashierPayDel(t.dataset.payDel); return;
    }
    if (t.dataset.ccClose) { cashierCardClose(t.dataset.ccClose); return; }
```

4. **✕ 删除语义分叉**：

```js
async function cashierCardClose(id) {
  const r = _cashierRows[id];
  if (!r) return;
  if (r.source === 'linglong') {
    if (!window.confirm('从当日视图排除这张玲珑单？\n（不会删华为那边的原单，'
      + '再点「导入」也不会把它加回来）')) return;
    try {
      await api('/api/cashier/exclude', { method: 'POST', body: { id: r.id } });
      toast('已排除', 'good');
      if (_cashierEditing === r.id) cashierCardCancel();
      await loadCashier();
    } catch (e) {
      toast('排除失败：' + e.message, 'bad');
    }
    return;
  }
  await cashierRemove(id);          // 手工单：真删（确认弹窗在 cashierRemove 里）
}
```

5. **编辑态渲染时支付块数据来源**：`cashierCardEditHtml(r)` 里 `r.payments`
   在增删过程中被 `cashierPayAdd/Del` 直接写回 `_cashierRows[id].payments`
   （上面已这么做）——保存时 `cashierEditState` 的 `cashierPayRead` 读 DOM，
   两处口径一致（读 DOM 为准）。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
.venv/bin/node --check web/app.js
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.js tests/test_cashier.py
git commit -m "feat(收银): 组合支付方块（软提醒不拦截）+ 卡片✕按来源分叉删除/排除"
```

---

### Task 12: JS 一键导入按钮 + 黑名单设置入口

**Files:**
- Modify: `web/app.js`
- Test: `tests/test_cashier.py`（页面接线断言）

- [ ] **Step 1: 写失败测试**

```python
    def test_导入和黑名单入口接上了(self):
        for fn in ("cashierImport", "cashierBlacklistOpen",
                   "cashierBlacklistSave"):
            with self.subTest(fn=fn):
                self.assertIn("function %s" % fn, APP_JS)
        for ep in ("/api/cashier/import", "/api/cashier/import-settings",
                   "/api/cashier/exclude"):
            with self.subTest(ep=ep):
                self.assertIn(ep, APP_JS)
        # 导入按钮跑的时候要禁用（拉单可能几十秒，防连点）
        i = APP_JS.index("function cashierImport")
        self.assertIn("disabled", APP_JS[i:i + 900])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest "tests/test_cashier.py::Test页面接线::test_导入和黑名单入口接上了" -q
```
Expected: FAIL

- [ ] **Step 3: 实现**

`web/app.js`：

```js
async function cashierImport() {
  const btn = $('#cashier-import');
  if (!btn || btn.disabled) return;
  const day = cashierDayValue();
  if (!window.confirm(`导入 ${day} 的玲珑销售单？\n已导过的不会重复，`
    + `备注命中排除词的不进卡。`)) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '导入中…（拉单要一会儿）';
  try {
    const d = await api('/api/cashier/import', { method: 'POST', body: { day } });
    toast(`导入完成：新增 ${d.imported} 笔`
      + (d.skipped_blacklist ? `，排除词命中 ${d.skipped_blacklist}` : '')
      + (d.skipped_dup ? `，已存在 ${d.skipped_dup}` : ''), 'good');
    await loadCashier();
  } catch (e) {
    toast('导入失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function cashierBlacklistOpen() {
  const row = $('#cashier-blacklist-row');
  if (!row) return;
  row.hidden = !row.hidden;
  if (!row.hidden) {
    api('/api/cashier/import-settings').then((d) => {
      const inp = $('#cashier-blacklist-input');
      if (inp) inp.value = (d.blacklist || []).join(', ');
      const hint = $('#cashier-blacklist-hint');
      if (hint) hint.textContent = '改完点保存，立即生效';
    }).catch((e) => { toast('读设置失败：' + e.message, 'bad'); });
  }
}

async function cashierBlacklistSave() {
  const inp = $('#cashier-blacklist-input');
  const words = String(inp && inp.value || '').split(/[,，]/)
    .map((s) => s.trim()).filter(Boolean);
  try {
    const d = await api('/api/cashier/import-settings',
      { method: 'PUT', body: { blacklist: words } });
    toast(`已保存 ${d.blacklist.length} 个排除词`, 'good');
    const row = $('#cashier-blacklist-row');
    if (row) row.hidden = true;
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
  }
}
```

`bindCashierEvents`（只绑一次）补三个绑定：

```js
  $('#cashier-import').addEventListener('click', cashierImport);
  $('#cashier-blacklist-open').addEventListener('click', cashierBlacklistOpen);
  $('#cashier-blacklist-save').addEventListener('click', cashierBlacklistSave);
```

同时确认 `bindCashierEvents` 头部（`test_扫码枪回车直接进金额` 断言的
`cashierLookup(true)` 在函数前 1600 字符内）没被挤出去 —— 回车绑定保持在函数最前。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/test_cashier.py -q
.venv/bin/node --check web/app.js
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/app.js tests/test_cashier.py
git commit -m "feat(收银): 一键导入按钮（防连点）+ 排除关键词行内设置 —— 完成卡片页闭环"
```

---

### Task 13: 全量回归 + 验收

**Files:**
- Test: 全量

- [ ] **Step 1: 跑受影响的测试文件**

```bash
.venv/bin/python -m pytest tests/test_cashier.py tests/test_migrate.py tests/test_web_ia.py -q
```
Expected: PASS

- [ ] **Step 2: 全量测试**

```bash
.venv/bin/python -m pytest tests/ -q
```
Expected: 全绿（AGENTS.md 规矩：三个头都要绿 —— 本机至少跑通 `.venv` 这一头；
`test_web_ia.py` 若有对收银 HTML 结构的对照断言，按新结构更新断言而不是放水）

- [ ] **Step 3: 手工验收（起 serve 走一遍）**

```bash
.venv/bin/python -m src.cli serve   # → http://127.0.0.1:8787
```

金路径逐条点：
1. 顶部扫码 → 回车反查 → 名称带出 → 填实收 → 「保存一笔」→ 卡片出现、汇总数字跳动；
2. 卡片「改」→ 卡内变编辑态（含配件 + 添加配件、支付 + 添加支付）→ 金额改了黄条提醒 → 保存修改；
3. 手工卡 ✕ → 确认弹窗「删除这笔流水」；玲珑卡 ✕ → 确认文案是「排除」；
4. 「导入玲珑单」→ 确认弹窗 → 按钮禁用转圈 → toast 报新增/排除/已存在 → 再点一次不重复；
5. 「排除关键词」→ 填词保存 → 导入 → 命中的单不进卡；
6. 汇总条：笔数/件数/实收/配件四项与卡片对账一致；
7. 吸顶：滚动卡片列表时录入区和汇总条钉在顶部。

- [ ] **Step 4: 按验收结果修复并重跑（如有红）**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit（如有修正）**

```bash
git add -A
git commit -m "fix(收银): 订单卡改造验收修正 —— <按实际问题写>"
```
