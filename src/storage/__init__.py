"""存储层 —— 连接、事务、建表/加列、列缓存，**都在这里面**。

对外只暴露两个模块名（`db` / `schema`），别在这儿再 `from .db import *`：

```python
from ..storage import db, schema

with db.tx(path) as conn:                 # 开连接 + 事务（退出即提交/回滚）
    schema.ensure_columns(conn, "order_lines", row)   # ⚠ 不用先 clear_col_cache
```

## 为什么要这一层（M15 / 阶段 3.4）

以前那条规矩是"**谁自己 `sqlite3.connect()`，谁就得记得清列缓存**" ——
`pools.ensure()` 里那句手写的 `clear_col_cache()`（`pools.py:140`）就是它，
注释还写着"**测试当场抓到的，不是理论问题**"。

根子在于缓存只能用 `(id(conn), 表名)` 当键（`sqlite3.Connection` 既不支持弱引用、
也不能挂属性），而 `id()` 在连接释放后会被**复用** ⇒ "缓存说这列在、新库其实没有"
→ `ALTER` 被跳过 → 写入 `OperationalError`。

这一层的做法：**缓存挂在连接自己身上**（`sqlite3.connect(..., factory=…)` 出来的
`_cbg_cols`）。连接的缓存跟着连接走，**不存在"别人的缓存"这回事**，
于是"清理"这个动作**从 API 里消失**了 —— 不是"记得清"，是**没有可清的东西**。
"""
