"""**推送 / 导出模块** —— 管「东西从程序里出去」的那两条路：**发出去**、**落到本机**。

用户 2026-09-19 定的分工：

| 谁 | 管什么 |
|---|---|
| **业务模块** | **该不该推**（开关、"仅有差异才发"、这次有没有内容…）—— 业务自己最清楚 |
| **本模块** | **① 收到「渠道编码 + 推送内容」→ 发出去**；**② 记录各个推送渠道**（有哪些、配没配、上次成没成） |

用户 2026-09-21 又加了一条（原话：「区长账号有**导出为 excel** 功能，这个
**落在数据推送模块**吧，功能模块**调用「导出为 excel」**来把自己的数据
生成为 excel **到本地**」）：

| 谁 | 管什么 |
|---|---|
| **业务模块** | 把**自己的数据**整理成「表头 + 行」 |
| **本模块** | **③ 写成 xlsx → 落到本机 `out/exports/` → 把路径回给调用方**（`export_xlsx`，见 `export.py`） |

⇒ 好处：业务不用再记"哪个通道的 should_send 签名是什么"（历史上一个收 `has_diff`、
   一个收 `has_diff + ignore_when`，每个新功能都照抄一遍）；
   而"有哪些渠道、各自发得怎么样"是**全局**的事，集中在这儿才看得见全貌。
   "导出为 Excel"同理：三处各写一遍 xlsx 的时代到此为止（`export.py` 顶上写了边界）。

⚠ **对外入口，不搬家实现**：真正发送的还是 `integrations` 那层的 `mailer.py` / `wecom.py`，
   真正写 xlsx 的还是 `src/xlsx_io.write_sheets`。

用户 2026-09-21 再加一条（原话：「设计一个加密算法，**所有走邮件渠道的推送都用这个
加密算法加密**。解密密钥**随着大版本的安装包走，不进入小版本推包**」）：

| 谁 | 管什么 |
|---|---|
| **本模块** | **④ 邮件附件发出去之前先加密**（`seal_attachment()`）—— 往外发的东西怎么保护，问这儿 |
| **`modules/fetch`** | **反过来的那一半**：收进来的附件解密（`fetch.unseal_attachment()`） |

⚠ **算法本体不在这儿**，在 `src/mailcrypto.py`（`integrations` 那层，和 `mailer.py`
  同级）—— 密文格式只能有**一份定义**，加解密拆成两份迟早走散（这个项目为
  "两份定义"栽过好几次）。这儿是**推送侧的对外入口**。
⚠ 加密**只挂在邮件那条路上**（用户说的是"走邮件渠道的推送"）：企微是 webhook，
  通道本身是加密的，不套这一层。
⚠ **它防的是"顺手看"，不是门店**：密钥就在门店机器上（`.secrets/mail-key.json`）。
  要防门店得走非对称，那是另一版的事。

## 用法（业务侧）

```python
from ..modules import notify

# ① 该不该推 —— **业务自己判**
if 该推:
    res = notify.send("wecom", {"head": head, "lines": lines, "ctx": ctx}, cfg=cfg)
    if res["state"] != notify.SENT:
        记一笔(res["why"])

# ② 导出到本地 —— 把自己的数据交出去就行（该不该给这个按钮，也是业务/角色的事）
res = notify.export_xlsx({"总览": (表头, 行)}, name="周度达成-2026-W38", root=root)
if not res["ok"]:
    告诉用户(res["why"])
```

## 渠道编码

| 编码 | 是什么 | 配置在哪 |
|---|---|---|
| `mail` | 邮件（SMTP） | `.secrets/mail.env` / 设置页 |
| `wecom` | 企业微信机器人 webhook | `.secrets/wecom.env` / 设置页 |

⚠ 加一个渠道 = 在 `CHANNELS` 里加一行 + 在 `_SENDERS` 里加一个发送函数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...storage import runlog
# 导出为 Excel（用户 2026-09-21）—— **同一件东西的另一条出口**，所以放在这个模块里。
# ⚠ 它是 `__all__` 之外的子模块，`notify.export_xlsx` / `notify.EXPORT_DIR` /
#   `notify.recent_exports()` 三个名字在这儿转出去（业务侧只 import notify）。
from .export import EXPORT_DIR, MAX_SHEET, MAX_STEM                                 # noqa: F401
from .export import FAILED as EXPORT_FAILED, WRITTEN as EXPORT_WRITTEN              # noqa: F401
from .export import export_dir, export_xlsx, recent_exports, safe_sheet, safe_stem  # noqa: F401
from . import prefs as _prefs  # noqa: F401
from .prefs import all_prefs as notify_prefs, enabled as push_enabled, set_enabled as set_push_enabled  # noqa: F401

#: 发送结局 —— **只有这两个是"发送"本身的结果**。
#: "没配"/"主动关掉"是**业务**的判断结果，不在这个模块里（见模块头那段分工）。
SENT = "sent"
FAILED = "failed"

#: 渠道编码 → 给人看的说明。**这是渠道登记表**（加渠道就来这儿加一行）。
CHANNELS: Dict[str, dict] = {
    "mail": {"label": "邮件", "config": ".secrets/mail.env",
             "how": "SMTP；收件人在「通用设置 → 邮件」里配"},
    "wecom": {"label": "企业微信", "config": ".secrets/wecom.env",
              "how": "群机器人 webhook；在「通用设置 → 企业微信」里配"},
}


def seal_attachment(path, *, root=None):
    """**把一份附件加密好再交出去** —— 往外发的东西都走这儿。

    返回 `((文件名, 字节), 说明)`；说明来自 `mailcrypto.seal()`
    （`state` 是 `sealed`（加了）或 `plain`（没加））。

    ⚠ **没密钥时给的是原文 + `state="plain"`**：业务连续性优先（上报是门店的日常），
      但**必须说出来** —— `_send_mail` 会往正文尾部追一行，不许静默降级
      （AGENTS 坑 13/15 都是"静默"栽的）。
    ⚠ 文件名**保持原样**（`.db` / `.json` / `.xlsx`）：收信侧靠**密文魔数**分流，
      不靠后缀 —— 那样明文老包才能照旧收（升级是渐进的，两边版本会不一致）。
    """
    from ... import mailcrypto
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        # 老行为：文件不在就跳过（`mailer.build_message` 里那句 `is_file()` 同一件事）
        return (p.name, b""), {"state": "plain", "key_id": "",
                               "why": "附件读不出来：%s" % p.name}
    data, how = mailcrypto.seal(raw, root=root)
    return (p.name, data), how


def mail_key(*, root=None) -> dict:
    """邮件附件加密用的**密钥状态**（有没有、是哪把）—— 自检 / 设置页用。

    ⚠ 回来的字典里**只有 key_id，没有密钥本体**（`mailcrypto.describe` 就是那么写的：
      它会进自检输出、日志、界面 —— 带出去一次就等于泄露一次）。
    """
    from ... import mailcrypto
    return mailcrypto.describe(root)


def _seal_all(attachments, root):
    """附件逐份加密 → `([(名字, 字节)], [要追进正文的那几行])`。"""
    from ... import mailcrypto
    out, notes = [], []
    for a in attachments:
        (name, data), how = seal_attachment(a, root=root)
        if not data:
            continue                       # 读不出来的跳过（老行为）
        out.append((name, data))
        line = mailcrypto.note_for(how)
        if line and line not in notes:     # 几份附件同一个状态，只说一次
            notes.append(line)
    return out, notes


def _send_mail(content: dict, cfg: dict, root=None) -> str:
    from ... import mailer
    paths = mailer.load_mail_paths(cfg, root)
    if not paths:
        # 中台回落已在 load_mail_paths / load_mail_config 里做过；
        # 还空就是真没配（列表空 = 该渠道没配）
        raise RuntimeError("没有邮件推送路径")
    # ⚠ `to=[…]`：这一封**只发给这些人**（区长名单是门店自己配的，
    #   不能混进"通用设置"里那份收件人 —— 那会把门店目标发给不相干的人）。
    to = content.get("to") or None
    kw = {"to": to} if to else {}     # ⚠ **只在真指定时才传** —— 多传一个参数会让
                                      #   别处那些"只收老签名"的假 send 直接 TypeError
                                      #   （实测：4 条测试同时红，全是这个原因）
    # ⚠⚠ `prefix` 要**区分「没给」和「给了空串」**：`bugreport` 传的是 `prefix=""`
    #   （它主题自己带了 `[bug 上报]`，不想再套一层 `[报量对账]`），
    #   而 `content.get("prefix") or None` 会把空串**吃掉** ⇒ 主题上又套回配置里那个前缀。
    #   改走 `notify.send` 之后这条才露出来（原来 bugreport 是直接调 mailer.send 的）。
    if "prefix" in content:
        kw["prefix"] = content["prefix"]

    atts, notes = _seal_all(content.get("attachments") or (), root)
    body = content.get("body") or "\n".join(content.get("lines") or [])
    if notes:                              # ⚠ 加密 / 没加密，都要让收件人看得见
        body = body.rstrip() + "\n\n——\n" + "\n".join(notes)
    subject = content.get("subject") or content.get("head") or ""
    # 有几条路径发几条；一条失败不拦着别的（每条路径独立收件人/SMTP）
    sent, errs = [], []
    for _pid, mc in paths:
        try:
            mailer.send(mc, subject, body, attachments=atts, **kw)
            sent.append("、".join(to or mc.recipients))
        except Exception as e:                                     # noqa: BLE001
            errs.append("%s：%s" % ("、".join(mc.recipients) or mc.host, e))
    if not sent:
        raise RuntimeError("；".join(errs) or "没有邮件推送路径")
    msg = "已发送到 %s" % "、".join(sent)
    if errs:
        msg += "（%d 条失败：%s）" % (len(errs), "；".join(errs))
    return msg


WECom_TEMPLATES = ("report", "pos", "pools", "attain", "stock")


def _send_wecom(content: dict, cfg: dict, root=None) -> str:
    """企业微信 —— 三套模板，**调用方必须说清用哪套**（`content["template"]`）。

    | 模板 | 内容需要什么 | 谁在用 |
    |---|---|---|
    | `report` | `missing` / `reverse_unshipped` / `matched` / `total` / `report_path` | 报量排查（差异清单） |
    | `pos` | `head` / `lines` / `ctx` | POS 合规（月度指标） |
    | `pools` | `head` / `lines` / `xlsx` / `ctx` | 双平台数据对比 |
    | `attain` | `head` / `lines` / `ctx` | 销售达成（周度，不 @人） |
    | `stock` | `head` / `lines` / `ctx` / `xlsx` | **库存盘点**（M16，人点了按钮才发，不 @人；**清单文件也发群里**，受 `send_file` 控制） |

    ⚠ **三条模板故意不一样**（@不 @人 / 附不附清单 / 措辞），别顺手统一 ——
      用户 2026-09-16 定过，`wecom.py` 里各函数的 docstring 写着原因。
    ⚠⚠ **模板必须显式写**，不许"看有没有 `lines` 猜一个"：
      第一版就是那么猜的，结果 `{"head": "x"}` 被猜成 `report`，
      **静默发出了一条内容完全不对的消息**（本地测试是拿真 webhook 打出来的
      errcode=93000 才发现的）。猜错了不会报错，只会推错 —— 这种默认值最贵。
    ⚠ 这里只做**分发 + 按路径扇出**：内容怎么拼、要不要推，都是业务的事。
    2026-09-22：有几条 webhook 路径就往几个群各推一遍。
    """
    from ... import wecom
    tpl = content.get("template") or ""
    if tpl not in WECom_TEMPLATES:
        raise ValueError("没说清用哪套模板（template=%s；可选：%s）"
                         % (tpl or "空", " / ".join(WECom_TEMPLATES)))
    paths = wecom.load_wecom_paths(cfg, root)
    if not paths:
        raise RuntimeError("没有企微推送路径")
    ctx = content.get("ctx") or {}
    head = content.get("head") or ""
    lines = content.get("lines") or []

    def _one(wc):
        if tpl == "pos":
            return str(wecom.push_pos(wc, ctx, lines, head))
        if tpl == "pools":
            return str(wecom.push_pools(wc, ctx, lines, head, xlsx=content.get("xlsx")))
        if tpl == "attain":
            return str(wecom.push_attain(wc, ctx, lines, head))
        if tpl == "stock":
            return str(wecom.push_stock(wc, ctx, lines, head, xlsx=content.get("xlsx")))
        return str(wecom.push(wc, ctx, content.get("missing") or [],
                              content.get("reverse_unshipped") or [],
                              matched=int(content.get("matched") or 0),
                              total=int(content.get("total") or 0),
                              report_path=content.get("report_path")))

    sent, errs = [], []
    for pid, wc in paths:
        try:
            sent.append(_one(wc))
        except Exception as e:                                     # noqa: BLE001
            errs.append("路径%s：%s" % (pid or "?", e))
    if not sent:
        raise RuntimeError("；".join(errs) or "没有企微推送路径")
    msg = "、".join(sent)
    if len(paths) > 1:
        msg = "×%d 路径：%s" % (len(sent), msg)
    if errs:
        msg += "（%d 条失败：%s）" % (len(errs), "；".join(errs))
    return msg


#: 渠道编码 → 发送函数。**只有这一张表**（业务不再直接 import mailer/wecom）。
_SENDERS: Dict[str, Callable] = {
    "mail": _send_mail,
    "wecom": _send_wecom,
}


def channel_on(code: str, *, cfg: dict = None, root=None) -> bool:
    """那条渠道**在这台机器上配了路径没**（路径列表非空 = 开）。

    ⚠ 2026-09-22：不再看 yaml 的 `enabled` 勾选 —— **有路径就算开**。
    ⚠ 这一条**必须由调用方自己问**：`mailer.send` / `wecom.send_markdown`
      **都不看 enabled**（谁调谁负责）—— 漏了这一步的表现是"没配还在试发"，
      而发出去收不回来（AGENTS.md 坑 15）。
    ⚠ 读配置**失败时当"没开"**：宁可少发一条（界面上会说明白为什么），
      也别因为读不出开关就擅自往外发东西。
    """
    try:
        if code == "mail":
            from ... import mailer
            return bool(mailer.load_mail_paths(cfg, root))
        if code == "wecom":
            from ... import wecom
            return bool(wecom.load_wecom_paths(cfg, root))
    except Exception:                                          # noqa: BLE001
        return False
    return False


def send(code: str, content: dict, *, cfg: dict, root=None, feature: str = "") -> dict:
    """发之前判**功能开关**（2026-09-22：模块只设「要不要推送」）。

    ⚠ 平台（邮件/企微）走全局通道配置，**不在这拦**。
    """
    from . import prefs as P
    if feature:
        why = P.why_off(feature, root)
        if why:
            return {"ok": False, "state": "disabled", "why": why}
    """把 `content` 通过 `code` 这个渠道发出去，并**记一笔**。

    `content` 的形状（按渠道取用，缺的忽略）：

    | 键 | 给谁用 |
    |---|---|
    | `head` | 首行/主题（企微首行、邮件主题兜底） |
    | `subject` / `prefix` | 邮件主题与前缀 |
    | `lines` / `body` | 正文（企微按行、邮件整段） |
    | `ctx` | 推送上下文（门店名等，企微用） |
    | `attachments` | 邮件附件 |

    返回 `{"state": "sent"|"failed", "why": …, "code": …}`。**绝不抛** ——
    推送失败只是"没送出去"，主产物（分数/报告）照样算完了。
    """
    fn = _SENDERS.get(code)
    if fn is None:
        return _done(code, False, "不认识的渠道编码：%s（可选：%s）"
                     % (code, " / ".join(sorted(_SENDERS))), root)
    try:
        why = fn(content, cfg, root)
        return _done(code, True, why or "已发送", root)
    except Exception as e:                                     # noqa: BLE001
        return _done(code, False, "%s: %s" % (type(e).__name__, e), root)


def _done(code: str, ok: bool, why: str, root=None) -> dict:
    """记一笔（`run_record` 里 kind = `notify:<编码>`）—— 记不上不影响返回值。"""
    runlog.record("notify:" + code, ok, why="" if ok else why, note=why, root=root)
    return {"code": code, "state": SENT if ok else FAILED, "ok": ok, "why": why}


def channels(cfg: Optional[dict] = None, root=None) -> List[dict]:
    """**记录各个推送渠道** —— 有哪些、配没配、上次发得怎么样。

    配置那半现读（`mailer` / `wecom` 的 config 对象），历史那半来自 `run_record`
    （kind = `notify:<编码>`）⇒ 不用另建一张表。
    """
    from ... import mailer, wecom
    cfg = cfg or {}
    hist = runlog.summary(root)
    out = []
    for code, meta in sorted(CHANNELS.items()):
        row = dict(meta, code=code, enabled=False, configured=False, detail="")
        try:
            if code == "mail":
                paths = mailer.load_mail_paths(cfg, root)
                row["enabled"] = bool(paths)
                row["configured"] = bool(paths) and all(
                    mc.ready for _i, mc in paths)
                recips = []
                for _i, mc in paths:
                    recips.extend(mc.recipients or [])
                row["detail"] = "、".join(recips) if recips else "没有推送路径"
            elif code == "wecom":
                paths = wecom.load_wecom_paths(cfg, root)
                row["enabled"] = bool(paths)
                row["configured"] = bool(paths) and all(
                    wc.ready for _i, wc in paths)
                row["detail"] = ("%d 条路径" % len(paths)) if paths else "没有推送路径"
        except Exception as e:                                 # noqa: BLE001
            row["detail"] = "配置读不出来：%s" % e
        h = hist.get("notify:" + code) or {}
        row.update(last_ok=h.get("last_ok", ""), last_fail=h.get("last_fail", ""),
                   last_fail_why=h.get("last_fail_why", ""),
                   fail_streak=h.get("fail_streak", 0), sent=h.get("runs", 0))
        out.append(row)
    return out


def history(code: str = "", root=None, limit: int = 20) -> list:
    """某个渠道（或全部）最近发过什么 —— `bugreport` / 健康面板用。"""
    if code:
        return runlog.recent(root, limit=limit, kind="notify:" + code)
    rows = []
    for c in sorted(CHANNELS):
        rows += runlog.recent(root, limit=limit, kind="notify:" + c)
    return sorted(rows, key=lambda r: r.get("finished_at") or "", reverse=True)[:limit]
