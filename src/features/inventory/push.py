"""盘点结果**落盘 + 推送** —— 「导出 excel 这一步接给推送」（用户 2026-09-20）。

## 这一层为什么在**后端**

前端（`web/inventory/ui.js` 的 `exportXlsx()`）第 1498 行拿到的 `bytes` 就是完整的
xlsx（7 张表，自己手写的 OOXML）。原来它的下一步是 `xlsx.download()` —— 存到本机。
现在多一条路：**把同一份字节 POST 给后端**，后端落盘 + 推出去。

⚠ **推送文案从这份 xlsx 里读**（`read_summary` 读第一张「汇总」表），
  不在后端另算一份口径 —— 否则"推出去的"和"门店看到的那份"迟早对不上，
  而这种不一致**只有肉眼能发现**（这个项目为"两份定义"栽过好几次）。

⚠ 分工照 `modules/notify` 的规矩：**该不该推在业务侧判**（这里是"用户点了按钮"），
  发出去 + 记各渠道是 `notify` 的事。所以这里没有 `should_send` 那套开关：
  用户点「导出并推送」就是要发，被「只有差异才推」挡掉反而莫名其妙。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional, Tuple

#: 导出的 xlsx 落这儿（相对项目根）。⚠ `out/` 是"这台电脑自己的东西"，
#: 自更新一根手指都不碰 —— 门店升级不会把盘点清单清掉。
EXPORT_REL = "out/inventory"

#: 推送正文里要哪几行（标签必须跟前端「汇总」表里那列**逐字一致**）。
#: ⚠ 少了标签就少一行（`build_message` 不编数）；改了前端标签记得同步这儿。
WANT_ROWS = (
    "应盘（有串号）", "已盘到", "其中扫码盘到", "其中手工确认", "未扫到",
    "盘点完成率", "表外码（去重）", "在途（待入库）台数", "其中已扫到（货已到）",
    "无串号商品行数", "无串号已手工盘过行数", "无串号差异（仅已盘部分）",
)


def safe_name(name: str) -> str:
    """文件名净化 —— 跟前端 `fileBase()` 同一个规则（`\\/:*?"<>|` → `_`）。

    ⚠ 后端也要做一遍：前端那个是"给人看的文件名"，**不能当路径信任**。
    """
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .")
    return s[:120] or "库存盘点"


def export_dir(root) -> Path:
    return Path(root) / EXPORT_REL


def save_export(root, name: str, data: bytes) -> Path:
    """把前端传上来的 xlsx 落到 `out/inventory/<净化过的名字>.xlsx`。

    ⚠ 校验**是不是 xlsx**：一个 zip 文件头是 `PK`。不校验的话，
      前端哪天传了半截/传成 JSON，我们会把一个坏文件推给门店，
      而邮件附件坏了**没人会当场告诉你**。
    """
    if not data or data[:2] != b"PK":
        raise ValueError("传上来的不是 xlsx（前 40 字节：%r）" % (data[:40] if data else b""))
    p = export_dir(root) / (safe_name(name) + ".xlsx")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def read_summary(path) -> dict:
    """读导出文件**第一张表**（「汇总」）→ `{项目: 数值}`。

    用 `xlsx_io.read_rows`（**不信 `<dimension>`**）：前端那份 xlsx 是手写 OOXML、
    **压根没写 dimension 标签**，用 openpyxl 只读模式会以为整表只有表头
    （这个坑项目里踩过：ERP 导出的 23,593 行全丢且不报错）。
    """
    from ...xlsx_io import read_rows
    out: dict = {}
    for r in read_rows(path) or []:
        if not r:
            continue
        k = str(r[0] if r[0] is not None else "").strip()
        if k:
            out[k] = r[1] if len(r) > 1 else ""
    return out


def _v(summary: dict, key: str) -> str:
    """取一个数，**取不到就是空串**（不编 0 —— "没有这个数"和"这个数是 0"不一样）。"""
    if key not in summary:
        return ""
    v = summary.get(key)
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip()


def build_message(store: str, date: str, summary: dict) -> Tuple[str, list]:
    """推什么 —— **纯函数**（拿一份「汇总」字典就能单测，不碰网络/文件）。

    口径上只说"盘点的结果"，不评价好坏：未扫到是**盘亏嫌疑**、表外码是
    **窜货/调拨未入账嫌疑**，两者都要人去看，所以并排列出来。
    """
    where = store or _v(summary, "盘点仓库") or "本店"
    day = date or _v(summary, "快照日期")
    should, found = _v(summary, "应盘（有串号）"), _v(summary, "已盘到")
    rate = _v(summary, "盘点完成率")
    head = "库存盘点：%s %s" % (where, day)
    if should:
        head += " —— 应盘 %s 台，已盘到 %s 台%s" % (should, found or "0",
                                                ("（%s）" % rate) if rate else "")

    lines = []
    if _v(summary, "未扫到"):
        lines.append("未扫到 %s 台（账面有串号、没扫到 —— 盘亏嫌疑）" % _v(summary, "未扫到"))
    if _v(summary, "表外码（去重）"):
        lines.append("表外码 %s 个（扫到了但本店账面没有 —— 窜货/调拨未入账嫌疑）"
                     % _v(summary, "表外码（去重）"))
    if _v(summary, "其中手工确认"):
        lines.append("其中手工确认 %s 台" % _v(summary, "其中手工确认"))
    transit, arrived = _v(summary, "在途（待入库）台数"), _v(summary, "其中已扫到（货已到）")
    if transit:
        lines.append("在途待入库 %s 台%s" % (transit, ("，已扫到（货已到）%s 台" % arrived)
                                        if arrived and arrived != "0" else ""))
    ns_rows, ns_done = _v(summary, "无串号商品行数"), _v(summary, "无串号已手工盘过行数")
    if ns_rows:
        lines.append("无串号商品 %s 行（已盘过 %s）" % (ns_rows, ns_done or "0"))
    if _v(summary, "无串号差异（仅已盘部分）"):
        lines.append("无串号差异（仅已盘部分）：%s" % _v(summary, "无串号差异（仅已盘部分）"))
    if _v(summary, "重复扫描次数"):
        lines.append("重复扫描 %s 次" % _v(summary, "重复扫描次数"))
    if not lines:
        lines.append("这份导出里没读到汇总行 —— 明细见附件")
    return head, lines


def body_text(head: str, lines, path=None) -> str:
    """邮件正文（企微那条走 `wecom.push_stock`，不用这个）。"""
    out = [head, ""] + list(lines or [])
    if path:
        out += ["", "完整清单见附件：%s" % Path(path).name]
    return "\n".join(out)


def push(path, *, cfg: Optional[dict] = None, root=None, store: str = "",
         date: str = "", mail: bool = True, wecom: bool = True,
         summary: Optional[dict] = None, who: str = "") -> dict:
    """把导出的那份 xlsx 推出去（邮件带附件 + 企微一条汇总）。

    返回 `{"head", "lines", "path", "mail", "wecom"}` —— 两个渠道各是
    `notify.send()` 的返回值（`{"state": "sent"|"failed", "why": …}`），
    **绝不抛**：推不出去不影响文件已经落盘。

    ⚠ **关掉的渠道不发**：`通用设置 › 邮件 / 企业微信` 那两个开关是"这台机器要不要
      走这条道"，门店关掉了就是不想收 —— 关着的那条返回 `state="skipped"`。
    ⚠ `who`（谁点的这个按钮）会写进运行记录（C7：推送要留痕）——
      它是**外部可见动作**，出了事要能查出"谁在什么时候把哪份推出去的"。
    """
    from ...modules import notify
    from ...storage import runlog
    from ...paths import ROOT

    root = Path(root or ROOT)
    p = Path(path)
    if summary is None:
        summary = read_summary(p)
    head, lines = build_message(store, date, summary)
    ctx = {"门店": store or _v(summary, "盘点仓库") or "本店",
           "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "配置文件": str(cfg.get("_config_path") or "") if isinstance(cfg, dict) else ""}
    out = {"head": head, "lines": lines, "path": str(p),
           # ⚠ 调用方主动关掉某个渠道时，`why` 要**说清是"被关掉的"**，
           #   别停在初始化那句"没推"上 —— 界面上会显示成一句没头没尾的话。
           "mail": {"state": "skipped", "ok": False,
                    "why": "没推" if mail else "这次没走邮件（调用方关掉了）"},
           "wecom": {"state": "skipped", "ok": False,
                     "why": "没推" if wecom else "这次没走企业微信（调用方关掉了）"}}
    c = cfg if isinstance(cfg, dict) else {}

    if mail:
        if not _channel_on(c, root, "mail"):
            out["mail"] = {"state": "skipped", "ok": False,
                           "why": "「通用设置 › 邮件」里没启用"}
        else:
            out["mail"] = notify.send("mail", {"subject": head,
                                               "body": body_text(head, lines, p),
                                               "attachments": (p,)}, cfg=c, root=root)
        # 企微那条尾部要写"清单在哪" —— **按事实写**：邮件没发出去时还说
        # "见邮件附件"就是骗人，而门店会照着去找（见 `wecom.build_stock_markdown`）。
        ctx["附件说明"] = ("完整清单（7 张表）在邮件附件里：%s" % (out["mail"].get("why") or "")
                       if out["mail"].get("ok") else
                       "完整清单已存在控制台那台机器上（邮件没发出去：%s）"
                       % (out["mail"].get("why") or "？"))
    else:
        ctx["附件说明"] = "完整清单已存在控制台那台机器上"
    if wecom:
        if not _channel_on(c, root, "wecom"):
            out["wecom"] = {"state": "skipped", "ok": False,
                            "why": "「通用设置 › 企业微信」里没启用"}
        else:
            # ⚠ `xlsx` 一定要带上：群里那条**也发清单文件**（受 `send_file` 控制）。
            #   2026-09-21 用户指出来的：「推送没问题，**但是没有文件**ok嘛」——
            #   原来只发了摘要，等于只给门店一个数字、不给能对货的清单。
            out["wecom"] = notify.send("wecom", {"template": "stock", "ctx": ctx,
                                                 "head": head, "lines": lines,
                                                 "xlsx": p}, cfg=c, root=root)
    ok = bool(out["mail"].get("ok") or out["wecom"].get("ok"))
    why = "；".join("%s：%s" % (k, out[k].get("why") or "")
                    for k in ("mail", "wecom") if not out[k].get("ok"))
    # ⚠ 记一笔（kind=inventory）—— 运行日志里能看到"谁什么时候推过盘点清单"。
    #   `notify` 自己也会各记一条（kind=notify:mail / notify:wecom），那是渠道口径。
    runlog.record("inventory", ok, why=why, note=head, root=root,
                  file=p.name, lines=len(lines), who=who)
    return out


def _channel_on(cfg: dict, root, code: str) -> bool:
    """那条渠道在这台机器上开着没（`通用设置` 里那两个开关）。

    ⚠ 2026-09-21（M18）：实现搬去了 `modules.notify.channel_on` —— 上报那条链要问
      同一个问题，各写一份就是三份实现（而读的是同一份配置）。这里只留个转发，
      **名字和语义都没变**（调用方和测试不用动）。
    """
    from ...modules import notify
    return notify.channel_on(code, cfg=cfg, root=root)
