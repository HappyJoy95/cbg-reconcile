"""邮件推送：跑完对账把差异清单发到指定邮箱。

用标准库 `smtplib` + `email` —— **不引新依赖**，门店电脑上少装一个包是一个。

配置分两处（跟云商账号一个路子）：
    config/store-*.yaml   mail: 段 —— 主机、端口、收件人这些**不敏感**的
    .secrets/mail.env     MAIL_USERNAME / MAIL_PASSWORD —— **敏感的**

一条铁律：**发邮件失败绝不能让对账失败**。对账结果才是主产物，
邮件只是投递方式 —— 邮件挂了要大声说，但退出码不能因此变。
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from . import envfile

DEFAULT_ENV_FILE = ".secrets/mail.env"
SECURITY = ("ssl", "starttls", "none")
WHEN = ("always", "only_diff")

# 常见邮箱的填法，界面上给人做参考
PRESETS = [
    {"name": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "security": "ssl",
     "hint": "密码填「授权码」，不是 QQ 密码"},
    {"name": "腾讯企业邮", "host": "smtp.exmail.qq.com", "port": 465, "security": "ssl",
     "hint": "密码填邮箱登录密码或客户端专用密码"},
    {"name": "163 邮箱", "host": "smtp.163.com", "port": 465, "security": "ssl",
     "hint": "密码填「授权码」"},
    {"name": "阿里企业邮", "host": "smtp.mxhichina.com", "port": 465, "security": "ssl",
     "hint": ""},
    {"name": "Outlook", "host": "smtp.office365.com", "port": 587, "security": "starttls",
     "hint": ""},
]

_XLSX_TYPE = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _connect_ipv4(host: str, port: int, timeout, source_address=None):
    """只走 IPv4 建连。

    **实测坑**：`smtp.qq.com` 有 IPv6 地址，但那条路经常卡死 ——
    `socket.create_connection` 会按 getaddrinfo 的顺序挨个试，
    卡在 IPv6 上就是几十秒白等。没有 IPv4 地址时返回 None，让调用方回退。
    """
    import socket
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    err = None
    for family, stype, proto, _canon, sa in infos:
        sock = None
        try:
            sock = socket.socket(family, stype, proto)
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock:
                sock.close()
    raise err or OSError(f"连不上 {host}:{port}")


class _SMTP4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        sock = _connect_ipv4(host, port, timeout, self.source_address)
        if sock is not None:
            return sock
        return super()._get_socket(host, port, timeout)      # 没有 A 记录就回退


class _SMTP4SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _connect_ipv4(host, port, timeout, self.source_address)
        if sock is None:
            return super()._get_socket(host, port, timeout)
        # server_hostname 必须传**域名**（传 IP 会证书校验失败）
        return self.context.wrap_socket(sock, server_hostname=self._host)


class MailError(RuntimeError):
    pass


# --------------------------------------------------------------------- 配置
def split_recipients(raw) -> list[str]:
    """收件人写成一行，逗号/分号/空格/换行都认。"""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw or "").replace(";", ",").replace("\n", ",").split(",")
    out, seen = [], set()
    for p in parts:
        a = p.strip()
        if a and "@" in a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


@dataclass
class MailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 465
    security: str = "ssl"
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: list = field(default_factory=list)
    subject_prefix: str = "[报量对账]"
    when: str = "always"
    env_file: str = DEFAULT_ENV_FILE

    @property
    def from_addr(self) -> str:
        return self.sender or self.username

    @property
    def ready(self) -> bool:
        return bool(self.host and self.port and self.recipients and self.from_addr)

    def problems(self) -> list[str]:
        miss = []
        if not self.host:
            miss.append("SMTP 服务器")
        if not self.recipients:
            miss.append("收件人")
        if not self.from_addr:
            miss.append("发件人（或账号）")
        if self.security not in SECURITY:
            miss.append(f"加密方式（只能是 {'/'.join(SECURITY)}）")
        if self.when not in WHEN:
            miss.append(f"发送时机（只能是 {'/'.join(WHEN)}）")
        return miss


def _as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_mail_config(cfg: dict, root=None) -> MailConfig:
    """cfg 是门店配置（`load_config` 的返回值）。配置文件 > 环境变量 > 默认值。"""
    m = (cfg or {}).get("mail") or {}
    env_path = envfile.resolve(m.get("env_file") or DEFAULT_ENV_FILE, root)
    sec = envfile.parse(env_path)

    def pick(key, default=""):
        if m.get(key) not in (None, ""):
            return m[key]
        return os.environ.get(f"MAIL_{key.upper()}", default)

    try:
        port = int(pick("port", 465))
    except (TypeError, ValueError):
        port = 465

    return MailConfig(
        enabled=_as_bool(pick("enabled", False)),
        host=str(pick("host", "")).strip(),
        port=port,
        security=str(pick("security", "ssl")).strip().lower(),
        username=str(sec.get("MAIL_USERNAME") or pick("username", "")).strip(),
        password=str(sec.get("MAIL_PASSWORD") or ""),
        sender=str(pick("sender", "")).strip(),
        recipients=split_recipients(pick("recipients", "")),
        subject_prefix=str(pick("subject_prefix", "[报量对账]")).strip(),
        when=str(pick("when", "always")).strip().lower(),
        env_file=str(env_path),
    )


def save_mail_secrets(env_file, *, username=None, password=None) -> Path:
    """只写敏感字段。传 None 表示不动。"""
    updates = {}
    if username is not None:
        updates["MAIL_USERNAME"] = username
    if password is not None:
        updates["MAIL_PASSWORD"] = password
    return envfile.update(envfile.resolve(env_file or DEFAULT_ENV_FILE), updates)


def describe_mail(mc: MailConfig) -> dict:
    """给界面看 —— **绝不回传密码**。"""
    return {
        "enabled": mc.enabled,
        "host": mc.host,
        "port": mc.port,
        "security": mc.security,
        "sender": mc.sender,
        "recipients": ", ".join(mc.recipients),
        "subject_prefix": mc.subject_prefix,
        "when": mc.when,
        "env_file": mc.env_file,
        "username": mc.username,
        "has_password": bool(mc.password),
        "ready": mc.ready,
        "problems": mc.problems(),
        "presets": PRESETS,
    }


# --------------------------------------------------------------------- 发送
def should_send(mc: MailConfig, has_diff: bool) -> tuple[bool, str]:
    """该不该发。返回 (发不发, 原因)。"""
    if not mc.enabled:
        return False, "邮件推送没开"
    if mc.when == "only_diff" and not has_diff:
        return False, "配置的是「仅有差异时发」，本次无差异"
    bad = mc.problems()
    if bad:
        return False, "配置不全：" + "、".join(bad)
    return True, ""


def build_message(mc: MailConfig, subject: str, body: str,
                  attachments=()) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{mc.subject_prefix} {subject}".strip()
    msg["From"] = mc.from_addr
    msg["To"] = ", ".join(mc.recipients)
    msg.set_content(body, charset="utf-8")
    for a in attachments:
        p = Path(a)
        if not p.is_file():
            continue
        maintype, subtype = _XLSX_TYPE if p.suffix.lower() == ".xlsx" else ("application", "octet-stream")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send(mc: MailConfig, subject: str, body: str, attachments=()) -> None:
    """同步发送。失败抛 MailError（调用方决定要不要因此失败整个流程）。"""
    msg = build_message(mc, subject, body, attachments)
    try:
        if mc.security == "ssl":
            server = _SMTP4SSL(mc.host, mc.port, timeout=30)
        else:
            server = _SMTP4(mc.host, mc.port, timeout=30)
        with server:
            server.ehlo()
            if mc.security == "starttls":
                server.starttls()
                server.ehlo()
            if mc.username:
                server.login(mc.username, mc.password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"SMTP 认证失败（多数邮箱要填「授权码」而不是登录密码）：{e}") from e
    except smtplib.SMTPException as e:
        raise MailError(f"SMTP 出错：{type(e).__name__}: {e}") from e
    except OSError as e:
        raise MailError(f"连不上 {mc.host}:{mc.port} —— {e}") from e


def build_report_mail(ctx: dict, summary: list[str], missing: int,
                      unshipped: int = 0) -> tuple[str, str]:
    """拼主题和正文。主题要让人不看正文就知道结果。"""
    store = ctx.get("门店", "?")
    day = ctx.get("目标日", "?")
    if missing:
        head = f"❌ 未报量 {missing} 台"
    elif unshipped:
        head = f"⚠️ 调拨货 {unshipped} 台查无出库"
    else:
        head = "✅ 无差异"
    subject = f"{store} {day} · {head}"
    body = "\n".join(summary)
    body += ("\n\n——\n"
             "本邮件由 cbg-reconcile 自动发送。完整差异清单见附件。\n"
             f"门店 {store} | 配置 {ctx.get('配置文件', '')}\n")
    return subject, body
