"""**收信能力** —— 数据抓取模块里的一个小口子：从邮箱里取指定邮件。

用户 2026-09-20：「数据抓取模块要加一个**获取指定邮件**的能力，
**默认读取的是 439845914@qq.com** 这个邮箱，**有配置 smtp 的话那就读取自己配置的邮箱**
的指定邮件」。

| 用哪个账号 | 什么时候 |
|---|---|
| **自己配的** | 这台机器配过发件账号（`mailer.load_mail_config().ready`） |
| **中台邮箱** | 没配自己的 —— 默认就是它（授权码 SMTP/IMAP 通用） |

⚠ 为什么放在**抓取模块**：它是"**外面的数据进来**"这一类，跟 dump/erp/tdoc 一个性质；
  收信是**读取**，跟推送（`modules/notify`，只发）分得很清楚 —— 别看名字像就混在一起。

⚠ 只用标准库（`imaplib` + `email`）：这一层不许引第三方（跟 `tdoc` 一样，
  门店机器上少一个依赖就少一个坏点）。

## ⚠⚠ 「按主题/发件人搜」是**本地筛**，不是服务端搜（2026-09-20 实测钉的）

中台那个 QQ 邮箱上，服务端 `SEARCH SUBJECT/FROM` **是假筛** —— A/B 双向实测：

| 搜索条件 | 返回 |
|---|---|
| `ALL` | 7 封（收件箱全部） |
| `SUBJECT "库存盘点"`（真有这封） | **7 封** |
| `SUBJECT "绝无此主题XYZZY"`（瞎编） | **7 封** |
| `FROM "000000000"`（瞎编） | **7 封** |
| `UNSEEN` | 0 封 ✓ 真筛 |
| `SINCE 01-Jan-2030` | 0 封 ✓ 真筛 |

⇒ QQQ 把 `SUBJECT`/`FROM` **整个忽略、还不报错**（跟云商那些假筛参数一个套路）；
  而且服务端搜**中文**时 imaplib 会直接 `UnicodeEncodeError`（关键词按 ASCII 编码）。
⇒ 所以现在：**只有 `unseen_only` 交给服务端**（那条是真筛），
  关键词一律**拉回本地自己比**（`matches()`，纯函数、好测）。
  代价是"多拉几封"，用 `FILTER_SCAN` 封顶。
"""

from __future__ import annotations

from email import message_from_bytes
from email.header import decode_header, make_header
from typing import List, Optional

#: 默认取多少封（够用就行；IMAP 一次拉太多很慢）
DEFAULT_LIMIT = 10

#: **带关键词筛选时最多拉几封**回来本地比（服务端筛是假的，见模块头那段）。
#: 200 是"够用又不至于把一次调用拖成分钟级"的折中 —— 真要翻更早的邮件，
#: 那是"搜历史"的需求，得换一条路（按日期分段），不是把这里调大。
FILTER_SCAN = 200


def matches(item: dict, *, subject_contains: str = "", sender_contains: str = "") -> bool:
    """这封邮件符不符合关键词（**纯函数**）—— 大小写不敏感的子串匹配。

    ⚠ 为什么要有这么个"简单"的函数：**服务端那条路不可信**（QQ 忽略 SUBJECT/FROM，
      见模块头）。所以"筛没筛"这件事必须有一处能单测的判据 —— 就是这里。
    """
    sub = str(subject_contains or "").strip().lower()
    frm = str(sender_contains or "").strip().lower()
    if sub and sub not in str(item.get("subject") or "").lower():
        return False
    if frm and frm not in str(item.get("from") or "").lower():
        return False
    return True


def config(cfg: dict = None, root=None, *, platform=None) -> dict:
    """该用哪个邮箱收信（转发 `mailer.imap_config`，口径只在那一处）。

    ⚠ **中台邮箱只有平台岗会用**（用户 2026-09-20）：门店 / 区长没配自己的发件账号
      就**不读邮箱**，返回 `{}`。`platform` 不传就自己去判断（看门店画像）。
    """
    from ... import mailer
    return mailer.imap_config(cfg or {}, root, platform=platform)


def _text(raw) -> str:
    """邮件头里的编码字（`=?utf-8?B?...?=`）→ 人能看的字符串。"""
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:                                          # noqa: BLE001
        return str(raw)


def parse(raw: bytes) -> dict:
    """一封原始邮件 → `{subject, from, to, date, body, html, attachments}`（纯函数，好测）。

    ⚠ 正文优先取 `text/plain`；只有 HTML 的话**不解析 HTML**（这一层不做富文本），
      直接把 HTML 原样带回来，让调用方自己决定 —— 宁可给原文，也别给一份
      "解析到一半"的假文本。

    ⚠ `attachments` 里**带 `data`（原始字节）**：这个能力的用途就是"从邮件里拿
      数据文件"（用户 2026-09-20：「获取指定邮件」），只给文件名等于没给。
      2026-09-20 实测补的：那次真发了一封盘点清单到中台，正文和主题都解析得出来，
      而**附件整个看不见**（`parse()` 里只 walk 正文）—— 收件人拿到文件了、
      程序却以为"这封没有附件"，正是最坏的那种"看着很合理的空"。
    """
    msg = message_from_bytes(raw)
    body, html = "", ""
    files: list = []
    for part in msg.walk():
        ctype = part.get_content_type()
        is_attach = part.get("Content-Disposition", "").startswith("attachment") \
            or bool(part.get_filename())
        if is_attach:
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:                                  # noqa: BLE001
                payload = b""
            files.append({"filename": _text(part.get_filename()) or "",
                          "content_type": ctype, "size": len(payload), "data": payload})
            continue
        if not msg.is_multipart() or ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:                                      # noqa: BLE001
            continue
        if ctype == "text/plain" and not body:
            body = text
        elif ctype == "text/html" and not html:
            html = text
    if not msg.is_multipart():
        try:
            text = (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:                                      # noqa: BLE001
            text = ""
        # ⚠ **非 multipart 也要看 content-type**：只有 HTML 的那种（不少系统通知邮件
        #   就是这样）如果直接塞进 `body`，上层就分不出"这是 HTML"了。
        if msg.get_content_type() == "text/html":
            html = text
        else:
            body = text
    return {"subject": _text(msg.get("Subject")), "from": _text(msg.get("From")),
            "to": _text(msg.get("To")), "date": _text(msg.get("Date")),
            "body": body or html, "html": bool(html and not body),
            "attachments": files}


def recent(cfg: dict = None, root=None, *, limit: int = DEFAULT_LIMIT,
           unseen_only: bool = False, subject_contains: str = "",
           sender_contains: str = "") -> List[dict]:
    """取最近几封邮件（**最新的在前**）；`subject_contains` / `sender_contains` **本地筛**。

    ⚠⚠ **关键词为什么不交给服务端**（2026-09-20 A/B 实测，见模块头那张表）：
      QQ 的 `SEARCH SUBJECT/FROM` **整个被忽略、还不报错**（真值和瞎编值都返回收件箱全部），
      而且中文关键词会让 imaplib 直接 `UnicodeEncodeError`。
      所以：`unseen_only` 走服务端（`UNSEEN` 是真筛），关键词**拉回本地比**（`matches()`）。
      代价：带关键词时会多拉一些（`FILTER_SCAN` 封顶），够用。

    ⚠ 连不上 / 认证失败**抛异常**（带人话原因）：抓取失败和"这段时间没有新邮件"
      是**两件事**，合成"返回空列表"会让上层以为"没数据"（这个项目栽过好几次）。
    """
    import imaplib
    info = config(cfg, root)
    from ... import mailer
    bad = mailer.imap_problems(info)
    if bad:
        raise RuntimeError("收信没配好：" + "、".join(bad))
    want = int(limit)
    kw = bool(str(subject_contains or "").strip() or str(sender_contains or "").strip())
    scan = max(want, FILTER_SCAN) if kw else want
    try:
        box = imaplib.IMAP4_SSL(info["host"], info["port"], timeout=30)
    except Exception as e:                                     # noqa: BLE001
        raise RuntimeError("连不上 %s（网络 / 端口被挡？）：%s: %s"
                           % (info["host"], type(e).__name__, e))
    try:
        box.login(info["user"], info["password"])
        box.select("INBOX")
        # ⚠ 这里**只发真的管用的条件**（`UNSEEN`）—— 关键词一个都不往服务端送：
        #   送了也是白送（被忽略），而且中文会抛 UnicodeEncodeError。
        crit = ["UNSEEN"] if unseen_only else []
        typ, data = box.search(None, *(crit or ["ALL"]))
        if typ != "OK":
            raise RuntimeError("邮箱搜索失败：%s" % (data,))
        ids = (data[0] or b"").split()
        out: List[dict] = []
        for one in ids[-scan:][::-1]:                          # 最新的在前
            typ, got = box.fetch(one, "(RFC822)")
            if typ != "OK" or not got or not isinstance(got[0], tuple):
                continue
            item = parse(got[0][1])
            if kw and not matches(item, subject_contains=subject_contains,
                                  sender_contains=sender_contains):
                continue
            item["uid"] = one.decode("ascii", "replace")
            out.append(item)
            if len(out) >= want:
                break
        return out
    finally:
        try:
            box.logout()
        except Exception:                                      # noqa: BLE001
            pass
