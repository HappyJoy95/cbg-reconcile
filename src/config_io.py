"""配置文件读写。

写回时**逐行做定点替换，保留原注释** —— 配置文件里的注释就是操作手册
（比如「顺和汇在数据里是小写 s」），被 yaml.dump 冲掉就亏大了。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# 前端允许改的字段（白名单）。其余一律不碰。
EDITABLE = (
    "store_code", "marker", "erp_store_name", "timezone",
    # ⚠ 只留 `cmd_check` **真的还在读**的两个（`cli.py:383/385`）。
    #   `page_size` / `pay_status` / `return_status` 2026-09-17 拿掉了 ——
    #   它们是**死配置**：`cli.py`/`run_check.py`/`bootstrap.py` 一处都没把它们
    #   传进接口，`dump.py` 用的是自己的 `--page-size`（默认 200），
    #   而且明确**不许**传 `payStatus`/`returnStatus`（传了会把已退原单整张滤掉）。
    "check.lookback_days", "check.report_lookahead_days",
    # 邮件推送（密码不在这里，在 .secrets/mail.env）
    "mail.enabled", "mail.host", "mail.port", "mail.security", "mail.sender",
    "mail.recipients", "mail.subject_prefix", "mail.when", "mail.env_file",
    # 企微推送（webhook 不在这里，在 .secrets/wecom.env）
    "wecom.enabled", "wecom.when", "wecom.mention_all", "wecom.send_file",
    "wecom.env_file",
)


def load_raw(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def pick(raw: dict) -> dict:
    """挑出前端要展示/编辑的字段（点号路径）。"""
    out = {}
    for key in EDITABLE:
        node = raw
        for part in key.split("."):
            node = (node or {}).get(part) if isinstance(node, dict) else None
        out[key] = node
    return out


_NEEDS_QUOTE = re.compile(r"""[\s\[\]{}#&*!|>'"%@`,]|^[-?:]|:\s""")


def _fmt(value) -> str:
    """序列化成 YAML 标量。

    ⚠ 该加引号的必须加：`subject_prefix: [报量对账]` 里开头的 `[` 在 YAML 里是**流式列表**，
    不加引号会被解析成 `["报量对账"]` —— 值悄悄变成了列表，很难查。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "":
        return '""'
    if s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return f'"{s}"'
    # 长得像数字的字符串也要加引号，否则 "123" 存进去会变成 int 123
    if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s):
        return f'"{s}"'
    if _NEEDS_QUOTE.search(s) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def update(path, updates: dict) -> dict:
    """定点改值，保留注释。返回改完之后的原始配置 dict。

    updates 的 key 用点号路径（如 `check.lookback_days`）。
    """
    p = Path(path)
    unknown = [k for k in updates if k not in EDITABLE]
    if unknown:
        raise ValueError(f"这些字段不允许从界面改：{unknown}")

    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines()
    section = None
    pending = dict(updates)

    for i, line in enumerate(lines):
        m = re.match(r"^(\S[^:]*):", line)          # 顶格键 = 新 section
        if m:
            section = m.group(1).strip()
        m = re.match(r"^(\s*)([A-Za-z_][\w]*):(\s*)(.*)$", line)
        if not m:
            continue
        indent, key, _, rest = m.groups()
        full = key if not indent else f"{section}.{key}"
        if full not in pending:
            continue
        comment = ""
        cm = re.search(r"\s+#", rest)
        if cm:
            comment = rest[cm.start():]
        lines[i] = f"{indent}{key}: {_fmt(pending.pop(full))}{comment}"

    for key, value in pending.items():              # 文件里没有的键 → 追加
        *parents, leaf = key.split(".")
        if parents:
            lines.append("")
            lines.append(f"{parents[0]}:")
            lines.append(f"  {leaf}: {_fmt(value)}")
        else:
            lines.append(f"{leaf}: {_fmt(value)}")

    p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return load_raw(p)
