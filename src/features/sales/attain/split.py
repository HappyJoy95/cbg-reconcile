"""门店**成员的目标拆分** —— 谁分多少台，落在 `.secrets/attain-split.json`。

用户 2026-09-19：「点击门店名的位置，是展开，下面是门店成员的名单，
内容是**门店周度目标拆分与达成**。结构参考上面，但是**目标是可以单独设置的**。」

## 为什么放 `out/`（用户 2026-09-19 定的）

一开始放的是 `.secrets/`（"人填的配置"那一类），用户改到 `out/`：
「**放 out 吧**，我想设置上**邮件自动发送**功能，门店拆的目标**定时发送给他们区长**」——
⇒ 它要跟"这一周算出来的那份"一起被推送，放在**同一处**最好找（`out/attain-<年>.json` 也在那儿）。

⚠ 两个目录都不会被自更新碰：`out/` 和 `.secrets/` 都在 `selfupdate` 的 `NEVER_TOUCH` 里。
⚠ 但它**不是报告**：报告可以重算，这份是**人填的**，删了就得重填 —— 所以
  `save()` 仍是"先写临时文件再 rename"，别让半截 JSON 覆盖掉它。

## 形状

```json
{"青岛城阳万象汇店|2026-W38": {"王俊燕": [2, 1, 0, …], "郭芮志": [1, 0, …]}}
```

* 键 = `门店|期间` —— **按周存**：下周重新分（目标本来就是一周一版）；
* 值 = `{成员: [每列目标台量…]}`，数组长度跟产品列数**一致**（对不上就截/补 0）。

## ⚠ 没设过的成员 = **没有目标**，达成率显示 `—`

门店层的口径是"目标 0 ⇒ 记 100%"，那是**办公室没给这家店定目标**的意思。
但成员层不一样：**"没分给他"不等于"他 100% 达标"** —— 直接显示 100% 会让
"还没分"看着像"干得好"。所以成员层目标缺失时达成率是 `None`（界面显示 `—`）。
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from typing import Dict, List, Optional

#: 落盘位置（相对项目根）
REL = "out/attain-split.json"


def path_of(root) -> Path:
    return Path(root) / REL


def key(store: str, period: str) -> str:
    """一家店一周一把钥匙。"""
    return "%s|%s" % (store or "", period or "")


def load(root) -> dict:
    """读全部（读不到给 `{}` —— 没设过是正常状态，不该报错）。"""
    p = path_of(root)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                          # noqa: BLE001
        return {}


def save(root, data: dict) -> Path:
    """整份写回（**先写临时文件再 rename** —— 中途挂了不会留半截 JSON）。"""
    p = path_of(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def _as_int(v) -> int:
    """`0` 兜底 —— 界面上填了怪东西（空、`x`、负数）也不该让整份配置写不进去。"""
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def targets_of(root, store: str, period: str) -> Dict[str, List[int]]:
    """这家店这一周的成员目标：`{成员: [每列目标…]}`（没设过就是空表）。"""
    return dict(load(root).get(key(store, period)) or {})


def set_targets(root, store: str, period: str, targets: Dict[str, list],
                ncols: int) -> dict:
    """写一家店一周的成员目标。

    ⚠ 每个数组都按列数**规整**（截长补 0），坏数据不进库 ——
      不然界面上会出现"第 12 列"这种按不出来的格子。
    """
    clean: Dict[str, List[int]] = {}
    for who, arr in (targets or {}).items():
        if not who:
            continue
        vals = list(arr or [])[:ncols]
        vals += [0] * (ncols - len(vals))
        clean[str(who)] = [_as_int(v) for v in vals]
    data = load(root)
    k = key(store, period)
    if clean:
        data[k] = clean
    else:
        data.pop(k, None)            # 全清空 ⇒ 这把钥匙也删掉，别留空壳
    save(root, data)
    return clean


def rate_of(target: int, actual: int) -> Optional[float]:
    """成员级达成率 —— **没设目标就是 `None`**（见模块头那段）。

    ⚠ 别套门店那条"目标 0 ⇒ 100%"：成员层"没分给他"和"他达标了"是两件事。
    """
    if not target:
        return None
    return min(float(actual) / float(target), 1.2)      # 封顶跟门店层一致（120%）


# ------------------------------------------------------------------ 发给区长
def managers_of(store: str, root) -> list:
    """这家店的区长（`config/managers.yaml`）—— 可能不止一位。

    ⚠ 配不到就返回**空表**：宁可"没发出去、日志里写清是谁没配"，
      也不能退化成发给某个默认地址（那会把门店目标发给不相干的人）。
    """
    from .... import config_io
    aliases = {store}
    # ⚠ 门店名有**两种写法**：达成表里是腾讯文档的名字（`鲁疆广场`），
    #   云商里是 `青岛鲁疆广场店`（见 `stores.yaml` 的 `tdoc_name`）——
    #   两边都认，免得"区长配了但发不出去、还查不出为什么"。
    try:
        row = config_io.find_store(store, root) or {}
        for k in ("erp_name", "tdoc_name"):
            if row.get(k):
                aliases.add(str(row[k]))
        for r in config_io.stores_table(root):
            names = {str(r.get("erp_name") or ""), str(r.get("tdoc_name") or "")}
            if names & aliases:
                aliases |= {x for x in names if x}
    except Exception:                                          # noqa: BLE001
        pass
    from .... import mailer
    out = []
    rows = []
    try:
        rows = config_io.stores_table(root)
    except Exception:                                          # noqa: BLE001
        rows = []
    for m in config_io.managers_table(root):
        # ⚠ 用**唯一口径** `stores_of_manager()`（老写法 `stores:` + 新写法 `regions:`
        #   都在里面合并）。这里原来只读 `m["stores"]` —— 按区域配的区长会**静默匹配不上**，
        #   表现是「门店点了发送，提示没有配区长」，而配置里明明写着（最难查的一类）。
        mstores = {str(x) for x in config_io.stores_of_manager(m, rows)}
        if not (aliases & mstores):
            continue
        addr = str(m.get("email") or "").strip()
        # ⚠ 用户 2026-09-20：「三个区长邮箱**先默认设置成 439845914@qq.com**？」
        #   ⇒ 没填邮箱时**回落到中台邮箱**（不用去配置里把同一个地址抄三遍），
        #     填上真邮箱之后自动优先用它。`fallback=True` 让日志/界面说清"这封是发去中台的"。
        out.append({"name": str(m.get("name") or ""),
                    "email": addr or mailer.CENTRAL_ADDR,
                    "fallback": not addr})
    return out


#: 拆分包（JSON 附件）的协议版本 —— 收信方**只认它**（不认识的版本要说出来并跳过）。
SPLIT_PROTOCOL = 1

#: 附件与落盘：`split-<门店码>-<2026-W38>.json`
#: ⚠ 门店码为空就退到店名（净化过）—— 但**包里照样标 `store_code: ""`**，
#:   收信方一眼能看出"这封没编码"，而不是当成另一家店。
SPLIT_REL = "out/splits"


def period_range(period: str):
    """`2026-W38` → `(date(2026,9,14), date(2026,9,20))`；认不出来给 `(None, None)`。

    ⚠ **ISO 周**（周一到周日）—— 跟 `metric.period_of()` 用同一套口径。
      包里同时带"第几周"和"起止日期"两个说法（用户 2026-09-21：
      「"第 38 周"换个口径就差一周」）。
    """
    import datetime as _dt
    try:
        y, _, w = str(period or "").partition("-W")
        return (_dt.date.fromisocalendar(int(y), int(w), 1),
                _dt.date.fromisocalendar(int(y), int(w), 7))
    except (TypeError, ValueError):
        return None, None


def package(root, store: str, period: str, *, columns=None, members=None,
            cfg: dict = None, saved_by: str = "") -> dict:
    """把一家店一周的拆分打成**收信方能认出来**的那份 JSON（M21 的协议）。

    口径全在 `.dsh/docs/2026-09-18-3.0.0-开发目标.md` 的「四·八」数据契约里，
    这里逐条对上：

    | 要素 | 用什么 | 为什么 |
    |---|---|---|
    | 哪家店 | `store_code`（机器键）＋ `erp_name` / `tdoc_name`（给人看） | 店名有两种写法，拿名字当键迟早出事 |
    | 哪个周 | `period`（ISO）＋ `start` / `end` | "第 38 周"换个口径就差一周 |
    | 谁 | `saved_by`（**云商登录名**）＋ 姓名只用于显示；没登录名就标 `unverified` | 项目规矩：**按账号认人** |
    | 每项多少台 | **`columns`（列名，顺序）＋ 每人的 `targets` 数组** | 存的就是"按列顺序的数组"，**少了列名数字会整体错位** |
    | 追溯 | `generated_at` / `version` | 复盘 + 去重 |
    """
    import datetime as _dt
    from .... import config_io, version
    vals = cfg or {}
    code = str(vals.get("store_code") or "").strip()
    d = load(root).get(key(store, period)) or {}
    mem = members if members is not None else [{"name": n, "targets": t} for n, t in d.items()]
    cols = columns if columns is not None else (_last_payload(root).get("columns") or [])
    try:
        hit = config_io.find_store(store, root) or {}
    except Exception:                                          # noqa: BLE001
        hit = {}
    start, end = period_range(period)
    by = str(saved_by or vals.get("erp_who") or "").strip()
    pkg = {
        "protocol": SPLIT_PROTOCOL,
        "kind": "split",
        "store_code": code,
        "store_name": store,
        "erp_name": str(vals.get("erp_store_name") or store or ""),
        "tdoc_name": str(hit.get("tdoc_name") or ""),
        "period": str(period or ""),
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": version.VERSION,
        "saved_by": by,
        # ⚠ 没有登录名 ⇒ 只带姓名并**标出来**（收信方就知道"这条身份没核实"）
        "unverified": not bool(by),
        "columns": [str(c) for c in (cols or [])],
        "members": [{"name": str(m.get("name") or ""),
                     "targets": [int(x or 0) for x in (m.get("targets") or [])]}
                    for m in mem],
    }
    return pkg


def write_package(root, pkg: dict) -> Path:
    """落成附件文件（`out/splits/split-<门店码>-<期间>.json`）—— 邮件附件要**真文件**。"""
    code = str(pkg.get("store_code") or "").strip() or "unknown"
    p = pathlib.Path(root) / SPLIT_REL
    p.mkdir(parents=True, exist_ok=True)
    f = p / ("split-%s-%s.json" % (code, pkg.get("period") or "no-period"))
    f.write_text(json.dumps(pkg, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def report_lines(store: str, period: str, columns, members) -> list:
    """邮件正文（**只发目标拆分**，用户选的）—— 纯文本，一行一个人。"""
    lines = ["门店：%s" % store, "期间：%s" % period, "",
             "成员目标（台）—— 列顺序跟达成表一致", ""]
    for m in members:
        tg = [int(x or 0) for x in (m.get("targets") or [])]
        if not any(tg):
            continue                      # 一个人都没分 ⇒ 不占一行
        lines.append("%s：合计 %d 台" % (m.get("name") or "?", sum(tg)))
        for i, c in enumerate(columns or []):
            if tg[i]:
                lines.append("    %s  %d 台" % (c, tg[i]))
    if len(lines) == 4:
        lines.append("（这一周还没有人分到目标）")
    lines += ["", "——", "目标由门店在控制台「销售数据 → 目标拆分」里设置。",
              "本邮件由 cbg-reconcile 自动发送。"]
    return lines


def send_report(root, store: str, period: str, cfg: dict = None, *, emit=None) -> dict:
    """把关卡拆好的目标发给这家店的区长（保存时调它）。

    返回 `{"sent": n, "to": [...], "why": ""}` —— **绝不抛**（保存目标本身不能因为
    邮件失败而失败；邮件只是"顺手告诉区长"）。
    """
    from .... import config_io, mailer
    from ....modules import notify as push
    say = emit or (lambda _s: None)
    who = managers_of(store, root)
    if not who:
        return {"sent": 0, "to": [], "why": "这家店还没配区长（config/managers.yaml）"}
    d = load(root).get(key(store, period)) or {}
    payload = _last_payload(root)
    columns = payload.get("columns") or []
    members = [{"name": n, "targets": t} for n, t in d.items()]
    if not members:
        return {"sent": 0, "to": [], "why": "这一周还没拆过目标"}
    cfg = cfg if cfg is not None else (config_io.load_raw(
        pathlib.Path(root) / "config" / "store-X.yaml") or {})
    # ⚠⚠ **谁调推送谁负责判"这条渠道开着没"**（AGENTS 坑 15）：`mailer.send()`
    #   自己不检查 `enabled` —— 不判的话，门店在「通用设置 › 邮件」里关掉了，
    #   点这个按钮照样发出去（而发出去收不回来）。
    mc = mailer.load_mail_config(cfg, root) if cfg else None
    if mc is not None and not getattr(mc, "enabled", False):
        return {"sent": 0, "to": [], "why": "邮件通道没开 —— 去「通用设置 › 邮件」打开再发"}
    subject = "%s 周度目标拆分（%s）" % (store, period)
    body = "\n".join(report_lines(store, period, columns, members))
    # ⭐ 2026-09-21（M21）：**同时带一份机器能读的 JSON**（用户：
    #   「邮件的发送要足够接收端识别**哪家店、谁、哪个周，每项多少任务**」）——
    #   正文照旧给人看，附件给区长/平台那台机器落库（`app/report_inbox.py`）。
    #   ⚠ 附件失败**不能挡住正文**：收信方读不到 JSON 时至少还有正文。
    attach = []
    try:
        pkg = package(root, store, period, columns=columns, members=members,
                      cfg=cfg, saved_by=payload.get("saved_by") or "")
        attach = [str(write_package(root, pkg))]
    except Exception as e:                                     # noqa: BLE001
        say("[拆分] ⚠ 机器可读的那份没生成（正文照发）：%s: %s" % (type(e).__name__, e))
    sent, fails = [], []
    for m in who:
        r = push.send("mail", {"subject": subject, "body": body,
                               "attachments": tuple(attach),
                               "prefix": mailer.SPLIT_SUBJECT_PREFIX,
                               "to": [m["email"]]}, cfg=cfg, root=root)
        (sent if r.get("ok") else fails).append(m["email"] if r.get("ok") else
                                                "%s(%s)" % (m["email"], r.get("why")))
    say("[拆分] 发给区长：成功 %d、失败 %d" % (len(sent), len(fails)))
    # ⚠ 失败要把**原因**带上（谁、为什么）—— 只写"一个都没发出去"的话，
    #   门店不知道是没配邮箱、还是 SMTP 没配好，只能来问。
    return {"sent": len(sent), "to": sent, "failed": fails,
            "why": "" if sent else ("一个都没发出去：" + "；".join(fails) if fails
                                    else "一个都没发出去")}


def _last_payload(root) -> dict:
    """取达成那份落盘（列名要从那儿来）—— 读不到给空表，别让推送把保存带崩。"""
    try:
        from . import attain as _a
        return _a.load(root) or {}
    except Exception:                                          # noqa: BLE001
        return {}


def should_resend(today, last_sent: str) -> bool:
    """**周一兜底**：今天是不是该再发一次（用户选的"拆完点保存就发 + 每周一兜底重发"）。

    ⚠ 只在**周一**且**这一周还没发过**时补发 —— 周一之后每天都发就成了骚扰。
    """
    if getattr(today, "weekday", lambda: 1)() != 0:
        return False
    return (last_sent or "") < today.isoformat()


def maybe_weekly_send(root, today=None, *, cfg: dict = None, emit=None) -> dict:
    """周一兜底重发（`daily` 跑的时候顺带调一次）。发过就什么都不做。"""
    import datetime as _dt
    today = today or _dt.date.today()
    data = load(root)
    sent_map = dict(data.get("_sent") or {})
    out = {"sent": 0, "did": []}
    for k in list(data.keys()):
        if k.startswith("_"):
            continue
        store, _, period = k.partition("|")
        if not should_resend(today, sent_map.get(k, "")):
            continue
        res = send_report(root, store, period, cfg, emit=emit)
        if res.get("sent"):
            sent_map[k] = today.isoformat()
            out["sent"] += res["sent"]
            out["did"].append(store)
    if out["did"]:
        data["_sent"] = sent_map
        save(root, data)
    return out
