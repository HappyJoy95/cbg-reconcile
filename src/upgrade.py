"""升级记录 + **大版本升级时把「要做的事」推给门店**。

## 为什么光有控制台弹窗不够（用户 2026-09-16 提的）

弹窗只有**有人打开控制台**才看得到。而门店的日常是"它自己跑，我不看" ——
大版本升级恰恰带着**必须做的事**：2.0.0 那次就是
"删掉改名前的旧定时任务，不删就一天跑两遍"。**看不到 = 没做 = 出事。**

所以大版本升级要**推**出去（邮件 / 企微）——推到门店的人真的会看的地方。

## 判据（`should_push`）

* **大版本变了** → **一定推**。⚠「大版本」**由用户在 `MAJOR_ABOVE` 里手工声明**，
  不看版本号形状（用户 2026-09-23：「大版本靠我定义吧，不靠版本号」）——
  版本号现在是时间戳 `yy.mmdd.hhmmss`，按老办法拆第 1 段会把
  **年份**当年份大版本（26→27），2027 年第一个包就年年误推一条。
* 小版本**不推**（2026-09-17 用户实测定的：「升级提醒不用推送吧」）——
  「要做的事」靠**控制台弹窗**讲，那是每次更新都弹的，不用再占一次推送；
* 同一版只推一次（状态里记 `pushed`）。

## 「升级记录」

每次启动比对一次，变了就记一条 `{from, to, at}`，留最近 N 条。
出问题时能回答"这台机器什么时候升的级、从哪一版升上来的" ——
这在这类"门店自己用、没人管"的工具上很值：报上来的现象经常
跟"它其实还在跑半年前的版本"有关。

## ⚠ 它绝不能把主流程搞挂

所有异常都吞掉（`check` 的签名就说明了这一点）。升级提醒是**锦上添花**，
为了它把每天的对账搞失败是本末倒置。
"""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path

from . import version as vmod
from . import whatsnew
# 比大小只认这一份实现（selfupdate 顶上只有标准库，import 它没有失败面）
from .selfupdate import parse_version

CST = datetime.timezone(datetime.timedelta(hours=8))

#: 状态文件。⚠ 放 `.secrets/`（`selfupdate.NEVER_TOUCH` 里的）——
#: 升级不会碰它，所以"上次跑的是哪一版"这个记忆能跨版本存活。
STATE_REL = ".secrets/upgrade.json"

#: 升级记录留几条。够回答"这台机器什么时候升的级"就行，不用留一輩子。
KEEP_HISTORY = 20

#: **大版本边界清单 —— 只认这里写的，不看版本号形状**（用户 2026-09-23：
#: 「大版本靠我定义吧，不靠版本号」）。
#:
#: 每条 `(边界, 代号)`：`升级前 ≤ 边界 < 升级后` 就算大版本 → 推送，
#: 推送文案带上**代号**（时间戳号对门店没意义，`2.2.0` 才看得懂）。
#:
#: * 边界用能比大小的版本号（走 `selfupdate.parse_version`，老号/时间戳号都能比）；
#: * 首条画在 `2.1.1` 之后：线上还在 `≤2.1.1` 的门店升到时间戳号就触发 ——
#:   2.2.0 就是原按 3.0.0 规划的那轮大改版（号用户 2026-09-23 改叫 2.2.0），
#:   按原规则（1.x → 2.x）本来也该推；
#: * 加新边界 = 改这一行 + 发一版（大版本本来就要发版，跟代码走一份、
#:   自更新同步到每台店，不会像配置文件那样每台漂移）。
MAJOR_ABOVE = [
    ("2.1.1", "2.2.0"),
]


def _path(root) -> Path:
    return Path(root) / STATE_REL


def load(root) -> dict:
    """读状态。**读不到/读坏了都给空 dict，绝不抛。**"""
    try:
        d = json.loads(_path(root).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root, d: dict) -> bool:
    """写状态。写不成返回 False（下次再试），**不抛**。"""
    p = _path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def history(root) -> list:
    """升级记录（最近 N 条，新的在后）。给控制台显示用。

    ⚠ 只有 `from` 是**真版本号**的才算"一次升级" —— 第一条例是
    `from: ""`（刚装上），显示成"从 ? 升到 2.0.0"会让人困惑。
    """
    out = []
    for h in (load(root).get("history") or []):
        if isinstance(h, dict) and h.get("from"):
            out.append(h)
    return out


def _looks_like_version(v) -> bool:
    """能当版本号比的才收。**取不出来就当"不知道"，宁可不推也不误推。**

    状态文件坏了（`""`、`"?"`、`"beta"`、`"v2"`）时不能进比较 ——
    `parse_version` 对这些一律给 `(0, 0, 0)`，会把"垃圾"排到所有边界之前、
    判成"跨了大版本"，于是给门店推一条"你从 ? 升到了 …"。
    （老版 `major()` 被删掉就是干这个活的，判据搬家了、这道闸得跟着搬。）
    """
    return bool(re.match(r"^\d", str(v or "").strip()))


def major_label(frm, to) -> str:
    """这次升级跨过的 `MAJOR_ABOVE` 边界代号；没跨返回 `""`。

    判据：存在边界 B 使 `frm ≤ B < to`（`parse_version` 比大小，
    老号 `2.1.1` 和时间戳号 `26.0923.x` 混着比也没问题）。

    ⚠ 跨一次只命中一次 —— 一台从 1.6.1 直接跳到时间戳号的机器同样会命中
    （`1.6.1 ≤ 2.1.1 < 26.x`），中间隔了几个边界都只算一次"大版本"。
    """
    if not (_looks_like_version(frm) and _looks_like_version(to)):
        return ""
    f, t = parse_version(frm), parse_version(to)
    for bound, name in MAJOR_ABOVE:
        if not _looks_like_version(bound):
            continue                      # 清单手写错了条，跳过别把判据搞崩
        b = parse_version(bound)
        if f <= b < t:
            return str(name or "")
    return ""


def is_major_jump(frm, to) -> bool:
    """是不是**大版本**变了 —— 即 `major_label` 有没有跨过声明的边界。

    ⚠ 只在**两边都拿得到**的时候才算 —— 全新安装（没有 `from`）不是升级，
    更不该推"你从 ? 升到了 …"。同理，`from` 是坏的也不推。
    """
    return bool(major_label(frm, to))


def record(root, current: str) -> "dict | None":
    """比对"上次跑的是哪一版"，变了就记一条。

    返回 `{"from","to","major","first"}`；**没变化就返回 `None`**
    （调用方据此判断"这次启动是不是刚升级完"）。

    `first=True` 表示这是**第一次记录**（刚装上，或者这功能刚上线）——
    这时候**不算升级**，不该推。
    """
    d = load(root)
    last = str(d.get("running") or "")
    if last == str(current):
        return None
    first = not last
    hist = list(d.get("history") or [])
    info = {"from": last, "to": str(current),
            "at": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}
    hist.append(info)
    d["history"] = hist[-KEEP_HISTORY:]
    d["running"] = str(current)
    _save(root, d)
    label = major_label(last, current)
    return {"from": last, "to": str(current), "first": first,
            "major": bool(label), "major_label": label, "at": info["at"]}


def should_push(root, current: str, change: dict) -> "str | None":
    """要不要推、推的理由是什么。返回 `None` 表示不推。

    ⚠ 判据写在一个地方，别散到调用点去 —— 用户问"为什么这台推了那台没推"
    的时候，得有一处能指着说清楚。
    """
    if not change or change.get("first"):
        # 第一次记录：刚装上、或者这功能刚上线。不是升级，不推。
        return None
    d = load(root)
    if str(d.get("pushed") or "") == str(current):
        return None                     # 这一版推过了
    if change.get("major"):
        # 代号（如「2.2.0」）跟出来 —— 时间戳号 `26.0923.x` 对门店没意义
        label = str(change.get("major_label") or "")
        return "大版本升级（%s → %s%s）" % (
            change["from"], change["to"], "，%s" % label if label else "")
    # ⚠ **小版本不推。** 用户 2026-09-17 实测后定的：
    #   「升级提醒不用推送吧」—— 他当初的要求就是**只在大版本推**
    #   （大版本 = 跨过 `MAJOR_ABOVE` 里他声明的边界，2026-09-23 起）。
    #   之前这里还有一条"小版本只要这次带了『要做的事』也推"，
    #   结果 2.0.1 → 2.1.0 这种普通升级也会发一封邮件出来。
    #   要做的事**控制台弹窗照旧会讲**（那是每次更新都弹的），不用再占一次推送。
    return None


def build_message(store: str, change: dict, notes) -> "tuple[str, str]":
    """组推送文案。返回 `(主题, 正文)`。

    ⚠ 正文顺序：**先说要做什么，再说改了什么**。门店扫一眼就该看到"我得干什么"，
    而不是先读六条改动。
    """
    frm, to = change.get("from") or "?", change.get("to")
    subject = "【升级提醒】%s：v%s → v%s" % (store, frm, to)
    lines = [
        "门店 %s 已从 **v%s** 升到 **v%s**。" % (store, frm, to),
        "",
    ]
    notes = notes or {}
    todo = notes.get("todo") or []
    if todo:
        lines += ["⚠ **需要你做的事**（不做会出问题）", ""]
        for i, t in enumerate(todo, 1):
            lines.append("%d. %s" % (i, t.get("text", "").replace("**", "")))
        lines.append("")
    lines += ["这一版改了什么（知道就行）", ""]
    for h in notes.get("highlights") or ():
        lines += ["· " + h.replace("**", "")]
    lines += ["",
              "——",
              "控制台：双击 start.bat 打开，上面那几件事在「设置」页里做。",
              "cbg-reconcile %s" % vmod.describe()]
    return subject, "\n".join(lines)


def notify(root, cfg: dict, change: dict, notes: dict) -> tuple:
    """推邮件 + 企微。返回 `(一句话结果, 真发出去的那几条)`。**不抛。**

    ⚠ 返回值分两截是必要的：调用方要靠"**真发出去过没有**"决定记不记
    `pushed` —— 全跳过（门店没配邮箱）或全失败（网不通）时**不能记**，
    否则"当时没配、后来配了"就永远收不到这条提醒了。
    """
    from . import mailer, wecom
    store = cfg.get("erp_store_name") or cfg.get("store_code") or "本店"
    subject, body = build_message(store, change, notes)
    said, sent = [], []

    try:
        mc = mailer.load_mail_config(cfg, root)
        # ⚠ `has_diff=True` 绕过「只有差异才发」—— 升级提醒跟对账差异没关系
        ok, why = mailer.should_send(mc, has_diff=True)
        if ok:
            mailer.send(mc, subject, body, prefix="")
            said.append("邮件")
            sent.append("邮件")
        else:
            said.append("邮件跳过（%s）" % why)
    except Exception as e:                                    # noqa: BLE001
        said.append("邮件失败（%s）" % e)

    try:
        wc = wecom.load_wecom_config(cfg, root)
        ok, why = wecom.should_send(wc, has_diff=True, ignore_when=True)
        if ok:
            wecom.send_markdown(wc, _markdown(store, change, notes))
            said.append("企微")
            sent.append("企微")
        else:
            said.append("企微跳过（%s）" % why)
    except Exception as e:                                    # noqa: BLE001
        said.append("企微失败（%s）" % e)

    return "、".join(said), sent


def _markdown(store: str, change: dict, notes) -> str:
    notes = notes or {}
    frm, to = change.get("from") or "?", change.get("to")
    out = ["## 升级提醒 · v%s → v%s" % (frm, to), "**%s**" % store, ""]
    notes = notes or {}
    todo = notes.get("todo") or []
    if todo:
        out.append("⚠ **需要你做的事**")
        for i, t in enumerate(todo, 1):
            out.append("> **%d.** %s" % (i, t.get("text", "").replace("**", "")))
        out.append("")
    out.append("这一版改了什么（知道就行）")
    for h in notes.get("highlights") or ():
        out.append("> · %s" % h.replace("**", ""))
    out.append("")
    out.append('<font color="comment">cbg-reconcile 自动发送 · %s</font>'
               % datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(out)


def check(root, cfg, current: str, *, push: bool = True) -> dict:
    """**升级检测 + 记录 + （该推就）推送**。调用方一个 try 都不用写。

    返回 `{"checked","change","pushed","why","result"}`，出问题也在里面说清楚，
    **永远不抛** —— 升级提醒是锦上添花，不能把每天的对账搞挂。
    """
    out = {"checked": False, "change": None, "pushed": False,
           "why": "", "result": ""}
    try:
        change = record(root, current)
        out["checked"] = True
        out["change"] = change
        if change is None:
            return out
        if not change.get("first"):
            _tag = ""
            if change.get("major"):
                _tag = "（大版本 · %s）" % (change.get("major_label") or "?")
            print("[升级] 检测到 v%s → v%s%s"
                  % (change["from"], change["to"], _tag))
            if change.get("major"):
                # 大版本升级：只往上一次记录里补一条"以前是什么版本"就够了，
                # 别的地方不用动 —— 具体迁移都在各自的代码里
                print("       照常跑就行；如果这一版有要做的事，下面会推给你。")
        why = should_push(root, current, change) if push else None
        out["why"] = why or ""
        if not why:
            return out
        # ⚠ `since` 用**升级前的版本**，不用 `seen`：
        #   门店可能已经点过控制台的「知道了」，但**看过 ≠ 做完了** ——
        #   推送的全部意义就是「需要你做的事」那一段，它不能是空的。
        #   （踩过：`seen == current` 时那一段整个消失。）
        notes = whatsnew.digest(root, current, since=change.get("from")) or {}
        if cfg is None:
            out["result"] = "没有配置，跳过推送"
            return out
        said, sent = notify(root, cfg, change, notes)
        out["result"] = said
        out["pushed"] = bool(sent)
        # ⚠ 只有**真发出去过**才记 `pushed`。全跳过 / 全失败时**不记** ——
        #   不然"门店当时没配邮箱、后来配了"就永远收不到这条提醒了。
        if sent:
            d = load(root)
            d["pushed"] = str(current)
            _save(root, d)
        print("[升级] %s" % said)
    except Exception as e:                                    # noqa: BLE001
        out["result"] = "升级检测出错（不影响本次运行）：%s" % e
    return out
