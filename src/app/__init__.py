"""按业务的**执行模块**（`app/<业务>.py`）—— 见开发目标 §4.5.4。

```
入口          编排         业务           基础
cli.py  ─┐
web.py  ─┼──→  app/  ──→  features/*/  ──→  integrations/  ──→  storage/
daily   ─┘
```

⚠ 规矩（有测试钉着，`tests/test_module_layout.py`）：
* `app/` **不许** import `cli` / `web` / `startup` / `runner`（入口层）；
* 一个业务一个文件（`app/pos.py`），**参数用关键字、结果用 dataclass** ——
  别再让调用方手工拼一个 `Namespace` 传进来（2026-09-19 那个
  "daily 每天算 POS 却从来不推"的缺口，根子就是那么来的）。
"""
