"""企业微信群机器人推送。

群机器人 webhook：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx`

两条设计取舍：

1. **webhook 地址当凭据存** —— 拿到它就能往群里发消息，性质等同密码，
   所以进 `.secrets/wecom.env`，不进仓库里的配置文件。

2. **markdown 发详情，text 负责 @人** —— 企微群机器人的 markdown **不支持 @**，
   只有 text 类型能带 `mentioned_list`。所以有差异时先发一条短 text @所有人，
   再发 markdown 详情。只发一条做不到"既能 @ 又能看清单"。

⚠ 频控：每个机器人 **每分钟最多 20 条**。所以正常情况下一次只发 1~2 条。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from . import envfile

DEFAULT_ENV_FILE = ".secrets/wecom.env"
WHEN = ("always", "only_diff")
API = "https://qyapi.weixin.qq.com/cgi-bin/webhook"
MAX_LIST = 10                 # markdown 里最多列几条，多了看附件
MARKDOWN_LIMIT = 3800         # 企微上限 4096 字节，留点余量（中文一个字 3 字节）

# 常见错误码 → 人话
ERRCODE_HINT = {
    93000: "webhook 地址不对（key 无效或机器人被删了）",
    45009: "发得太频繁，企微限流是每分钟 20 条，等一会儿再试",
    40001: "webhook 地址无效",
    40008: "消息内容有问题（多半是太长或格式不对）",
}


class WecomError(RuntimeError):
    pass


@dataclass
class WecomConfig:
    enabled: bool = False
    webhook: str = ""
    when: str = "always"
    mention_all: bool = True
    send_file: bool = True
    env_file: str = DEFAULT_ENV_FILE

    @property
    def key(self) -> str:
        """允许直接粘整条 URL，也允许只填 key。"""
        return extract_key(self.webhook)

    @property
    def ready(self) -> bool:
        return bool(self.key)

    def problems(self) -> list[str]:
        out = []
        if not self.key:
            out.append("webhook 地址")
        if self.when not in WHEN:
            out.append(f"发送时机（只能是 {'/'.join(WHEN)}）")
        return out


def extract_key(raw: str) -> str:
    m = re.search(r"[?&]key=([A-Za-z0-9\-_]+)", raw or "")
    if m:
        return m.group(1)
    s = (raw or "").strip()
    # 只填了 key 的情况：企微的 key 是标准 UUID
    return s if re.fullmatch(r"[A-Za-z0-9\-_]{16,}", s) else ""


def mask_key(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("****" if key else "")


def _as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------- 配置
def load_wecom_config(cfg: dict, root=None) -> WecomConfig:
    w = (cfg or {}).get("wecom") or {}
    env_path = envfile.resolve(w.get("env_file") or DEFAULT_ENV_FILE, root)
    sec = envfile.parse(env_path)

    def pick(key, default=""):
        if w.get(key) not in (None, ""):
            return w[key]
        return os.environ.get(f"WECOM_{key.upper()}", default)

    return WecomConfig(
        enabled=_as_bool(pick("enabled", False)),
        webhook=str(sec.get("WECOM_WEBHOOK") or pick("webhook", "")).strip(),
        when=str(pick("when", "always")).strip().lower(),
        mention_all=_as_bool(pick("mention_all", True), True),
        send_file=_as_bool(pick("send_file", True), True),
        env_file=str(env_path),
    )


def save_wecom_secrets(env_file, webhook: str | None = None) -> Path:
    updates = {}
    if webhook is not None:
        updates["WECOM_WEBHOOK"] = webhook
    return envfile.update(envfile.resolve(env_file or DEFAULT_ENV_FILE), updates)


def describe_wecom(wc: WecomConfig) -> dict:
    """给界面看 —— **webhook 只回显打码后的 key**。"""
    return {
        "enabled": wc.enabled,
        "webhook_key": mask_key(wc.key),
        "has_webhook": bool(wc.key),
        "when": wc.when,
        "mention_all": wc.mention_all,
        "send_file": wc.send_file,
        "env_file": wc.env_file,
        "ready": wc.ready,
        "problems": wc.problems(),
    }


def should_send(wc: WecomConfig, has_diff: bool) -> tuple[bool, str]:
    if not wc.enabled:
        return False, "企微推送没开"
    if wc.when == "only_diff" and not has_diff:
        return False, "配置的是「仅有差异时发」，本次无差异"
    bad = wc.problems()
    if bad:
        return False, "配置不全：" + "、".join(bad)
    return True, ""


# --------------------------------------------------------------------- 组装
def build_markdown(ctx: dict, missing: list, unshipped: list,
                   matched: int = 0, total: int = 0) -> str:
    """拼 markdown 正文。markdown 里 **不能 @人**（企微限制），@ 走 text 消息。"""
    store = ctx.get("门店", "?")
    day = ctx.get("目标日", "?")
    n_missing = len(missing)

    lines = [f"## 报量对账 · {store}", f"**{day}**"]
    if n_missing:
        lines.append(f"云商卖 <font color=\"info\">{total}</font> 台 → "
                     f"已报 <font color=\"info\">{matched}</font>，"
                     f"**未报 <font color=\"warning\">{n_missing}</font>**")
    else:
        lines.append(f"云商卖 <font color=\"info\">{total}</font> 台，"
                     f"**<font color=\"info\">全部已报量 ✅</font>**")

    if missing:
        lines.append(f"\n**❌ 未报量（{n_missing}）**")
        for s in missing[:MAX_LIST]:
            lines.append(f"> `{s.sn}` {s.item[:24]}")
            lines.append(f"> {s.seller} · {s.pay_time} · ¥{s.amount}")
        if n_missing > MAX_LIST:
            lines.append(f"> …还有 **{n_missing - MAX_LIST}** 台，见报告附件")

    if unshipped:
        lines.append(f"\n**⚠️ 调拨货查无出库（{len(unshipped)}）**")
        for it in unshipped[:MAX_LIST]:
            lines.append(f"> `{it.sn}` {str(it.info.get('item', ''))[:24]}")
        if len(unshipped) > MAX_LIST:
            lines.append(f"> …还有 **{len(unshipped) - MAX_LIST}** 台")

    lines.append(f"\n<font color=\"comment\">cbg-reconcile 自动发送 · "
                 f"{ctx.get('生成时间', '')}</font>")
    return _fit("\n".join(lines))


_TRUNC_SUFFIX = "\n\n<font color=\"comment\">（内容过长已截断）</font>"


def _fit(text: str, limit: int = MARKDOWN_LIMIT) -> str:
    """按**字节数**截断 —— 企微的上限是字节不是字符，中文一个字 3 字节。

    ⚠ 截断后缀**自己的字节数也要算进去** —— 第一版只留了 40 字节的余量，
    而后缀有 70 字节，结果"截断后"仍然超限。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    room = max(limit - len(_TRUNC_SUFFIX.encode("utf-8")), 0)
    cut = raw[:room].decode("utf-8", "ignore")     # ignore：别把汉字截半个
    return cut.rstrip() + _TRUNC_SUFFIX


def build_mention_text(ctx: dict, missing: int, unshipped: int = 0) -> str:
    store = ctx.get("门店", "?")
    day = ctx.get("目标日", "?")
    if missing:
        return f"【报量对账】{store} {day}：有 {missing} 台已卖未报量，请尽快上报"
    if unshipped:
        return f"【报量对账】{store} {day}：有 {unshipped} 台调拨货查无出库，请核对"
    return ""


# --------------------------------------------------------------------- 发送
def _check(j: dict, what: str) -> None:
    code = j.get("errcode")
    if code == 0:
        return
    hint = ERRCODE_HINT.get(code, j.get("errmsg") or "")
    raise WecomError(f"{what}失败：errcode={code} {hint}".strip())


def _post(url: str, payload: dict, timeout: int = 20) -> dict:
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise WecomError(f"连不上企微：{e}") from e
    except ValueError as e:
        raise WecomError(f"企微返回的不是 JSON：{e}") from e


def send_text(wc: WecomConfig, content: str, mention_all: bool = False) -> None:
    payload = {"msgtype": "text", "text": {"content": content}}
    if mention_all:
        payload["text"]["mentioned_list"] = ["@all"]
    _check(_post(f"{API}/send?key={wc.key}", payload), "发文本消息")


def send_markdown(wc: WecomConfig, content: str) -> None:
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    _check(_post(f"{API}/send?key={wc.key}", payload), "发 markdown 消息")


def upload_file(wc: WecomConfig, path) -> str:
    """传文件换 media_id（有效期 3 天）。超过 20MB 企微会拒。"""
    p = Path(path)
    if not p.is_file():
        raise WecomError(f"文件不存在：{p}")
    if p.stat().st_size > 20 * 1024 * 1024:
        raise WecomError(f"文件超过 20MB（{p.stat().st_size / 1048576:.1f}MB），企微不收")
    try:
        with p.open("rb") as f:
            r = requests.post(f"{API}/upload_media?key={wc.key}&type=file",
                              files={"media": (p.name, f,
                                               "application/octet-stream")},
                              timeout=60)
        r.raise_for_status()
        j = r.json()
    except requests.RequestException as e:
        raise WecomError(f"上传文件失败：{e}") from e
    except ValueError as e:
        raise WecomError(f"企微返回的不是 JSON：{e}") from e
    _check(j, "上传文件")
    mid = j.get("media_id")
    if not mid:
        raise WecomError(f"上传文件没拿到 media_id：{j}")
    return mid


def send_file(wc: WecomConfig, path) -> None:
    media_id = upload_file(wc, path)
    _check(_post(f"{API}/send?key={wc.key}", {"msgtype": "file", "file": {"media_id": media_id}}),
           "发文件")


def push(wc: WecomConfig, ctx: dict, missing: list, unshipped: list,
         matched: int = 0, total: int = 0, report_path=None) -> str:
    """推一次。返回一句人能看懂的结果。失败抛 WecomError。"""
    sent = []

    # ① 有差异时先 @人（markdown 不能 @，只能靠 text）
    text = build_mention_text(ctx, len(missing), len(unshipped))
    if wc.mention_all and text:
        send_text(wc, text, mention_all=True)
        sent.append("已 @所有人")

    # ② markdown 详情
    send_markdown(wc, build_markdown(ctx, missing, unshipped, matched, total))
    sent.append("已发摘要")

    # ③ 报告文件
    if wc.send_file and report_path:
        send_file(wc, report_path)
        sent.append("已发报告附件")

    return "、".join(sent)


def test_push(wc: WecomConfig) -> str:
    send_markdown(wc, "## cbg-reconcile 测试消息\n"
                      "收到就说明 webhook 配对了。\n"
                      "<font color=\"comment\">之后每次对账跑完都会推到这个群</font>")
    return "已推送测试消息"
