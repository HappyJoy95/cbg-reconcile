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
from email.utils import formatdate
from pathlib import Path
from typing import Optional

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
    #: **抄送**：每封都带上（用户 2026-09-19：「接收时都带上这个」= 中台邮箱要能看到全部外发）
    cc: list = field(default_factory=list)

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
        # ⚠ 2026-09-22 设置改版：**发送时机（when）从界面上拿掉了**，
        #   配了路径就发。`when` 字段仍读老配置但**不再校验、不再分支**。
        return miss


def _as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_mail_config(cfg: dict, root=None) -> MailConfig:
    """cfg 是门店配置（`load_config` 的返回值）。配置文件 > 环境变量 > 默认值。

    ⚠ 2026-09-22：有 `.secrets/push-paths.json` 时**以路径列表为准**
      （第一条路径供 `should_send` / 老调用方用；列表空 = 没配）。
    """
    from . import push_paths
    if push_paths.has_file(root):
        rows = push_paths.load_raw(root).get("mail") or []
        if rows:
            return mail_from_row(rows[0], root)
        # 有文件但列表空 = 主动清空 / 没配 —— **不顶中台**（等同以前 enabled:false）
        return MailConfig(enabled=False, host="", recipients=[], when="always")
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

    cfg_obj = MailConfig(
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
        # ⚠ **中台邮箱**（用户 2026-09-19）：
        #   ① 每封邮件都**抄送**它（`cc`）—— 办公室要能看到全部外发；
        #   ② 这台机器**没配自己的发件账号**时，用它当发件账号（见 `central_config`）。
        # ⚠ 中台地址是**公开常量** ⇒ 每封都抄送它（用户：「接收时都带上这个」），
        #   不用每台机器各自填一遍；env 里写了别的就听 env 的。
        cc=split_recipients(sec.get("MAIL_CENTRAL") or pick("central", "") or CENTRAL_ADDR),
    )
    # ⚠ **没有发件邮箱时默认用中台那份**（用户 2026-09-19：「在没有发件邮箱时默认使用这个」）。
    #   判据是"这台机器自己那份能不能发"（`ready`）—— 只有主机/收件人/发件人齐了才算能发。
    #   ⚠ 中台那份**也要配了才顶得上**（`central_config` 读不到就给 `None`）：
    #     什么都没有的时候照旧报"没配邮件"，**不会退化成静默不发**。
    # ⚠ **只在"这台机器压根没配发件账号"时才顶中台**，两条边界都要守：
    #   ① 配置里**明确写了 `enabled: false`** ⇒ 那是"**主动关掉邮件**"，
    #      **绝不顶**（顶了就成了"关了还在发"—— 最不该有的行为）；
    #   ② 自己那份**已经找到口令** ⇒ 说明这台机器配过，**别抢**（顶了会让人
    #      以为自己配的没生效）。缺主机/收件人那是 config 的问题，报出来比悄悄换账号好。
    explicit_off = ("enabled" in m and not _as_bool(m.get("enabled"), False)) or \
        (os.environ.get("MAIL_ENABLED") is not None and not cfg_obj.enabled)
    if not cfg_obj.ready and not explicit_off and not cfg_obj.password:
        central = central_config(root)
        if central is not None:
            return central
    return cfg_obj


#: **中台邮箱**（用户 2026-09-19 给的）—— 地址和 SMTP 主机是**公开信息**，
#: 写进代码、随包走，这样每台门店机器不用各自填一遍。
#: ⚠ **授权码不在代码里**：那等于公开一个能发信的账号（这个仓库是公开的）。
#:   它只放 `.secrets/mail.env` 的 `MAIL_CENTRAL_PASSWORD`。
#:   ⇒ 门店要想"没配发件账号时用中台发"，得在各自机器的 `.secrets/mail.env` 里放那一行
#:     （或者另外告诉我要不要像 ERP 那个内置账号一样做混淆内置 —— 那个是"公开也无所谓"
#:      的查询账号，跟这个能发信的账号性质不同，我没自作主张）。
CENTRAL_ADDR = "439845914@qq.com"
CENTRAL_HOST = "smtp.qq.com"
CENTRAL_PORT = 465
CENTRAL_SECURITY = "ssl"


def central_config(root=None) -> Optional[MailConfig]:
    """**中台邮箱**那份配置 —— 这台机器没配自己的发件账号时用它发。

    用户 2026-09-19：「加一个**中台邮箱**，**在没有发件邮箱时默认使用这个**，
    接收时都带上这个。」

    ⚠ 凭据放 `.secrets/mail.env`（`MAIL_CENTRAL_HOST/USER/PASSWORD/SENDER`）——
      **不进代码、不进包**：这是真能发信的账号，写进公开仓库等于公开它。
      （对比：ERP 那个内置公司账号是"大家共用、只能查数"，性质不同。）

    读不到就返回 `None` ⇒ 调用方照旧报"没配邮件"，**不会**退化成静默不发。
    """
    p = envfile.resolve(DEFAULT_ENV_FILE, root)
    sec = envfile.parse(p)
    pw = str(sec.get("MAIL_CENTRAL_PASSWORD") or os.environ.get("MAIL_CENTRAL_PASSWORD") or "")
    if not pw:
        # ⚠ **没授权码就当没配**（不静默不发）：地址是公开的，但没密码发不出去，
        #   顶上来只会让调用方以为"能发了"，然后卡在 SMTP 认证失败上。
        return None
    host = str(sec.get("MAIL_CENTRAL_HOST") or CENTRAL_HOST).strip()
    user = str(sec.get("MAIL_CENTRAL_USER") or CENTRAL_ADDR).strip()
    try:
        port = int(sec.get("MAIL_CENTRAL_PORT") or CENTRAL_PORT)
    except (TypeError, ValueError):
        port = CENTRAL_PORT
    central = str(sec.get("MAIL_CENTRAL") or CENTRAL_ADDR).strip()
    return MailConfig(
        enabled=True, host=host, port=port,
        security=str(sec.get("MAIL_CENTRAL_SECURITY") or CENTRAL_SECURITY).strip().lower(),
        username=user, password=pw,
        sender=str(sec.get("MAIL_CENTRAL_SENDER") or central).strip(),
        recipients=[central], cc=[central],
        subject_prefix="[报量对账]", when="always", env_file=str(p),
    )


def save_mail_secrets(env_file, *, username=None, password=None) -> Path:
    """只写敏感字段。传 None 表示不动。"""
    updates = {}
    if username is not None:
        updates["MAIL_USERNAME"] = username
    if password is not None:
        updates["MAIL_PASSWORD"] = password
    return envfile.update(envfile.resolve(env_file or DEFAULT_ENV_FILE), updates)


# ------------------------------------------------------------- 路径列表（2026-09-22）
def mail_from_row(row: dict, root=None) -> MailConfig:
    """`.secrets/push-paths.json` 里的一行 → 可直接发的 `MailConfig`。

    路径行里**自带密码**（整份 JSON 已在 `.secrets/`），不再拆 env。
    有行就算配了（`enabled=True`）—— 界面上没有「启用」勾选。
    """
    if not isinstance(row, dict):
        row = {}
    try:
        port = int(row.get("port") or 465)
    except (TypeError, ValueError):
        port = 465
    security = str(row.get("security") or "ssl").strip().lower()
    if security not in SECURITY:
        security = "ssl"
    mc = MailConfig(
        enabled=True,
        host=str(row.get("host") or "").strip(),
        port=port,
        security=security,
        username=str(row.get("username") or "").strip(),
        password=str(row.get("password") or ""),
        sender=str(row.get("sender") or "").strip(),
        recipients=split_recipients(row.get("recipients") or ""),
        subject_prefix=str(row.get("subject_prefix") or "[报量对账]").strip(),
        when="always",   # 字段留着兼容；路径模式不再看 when
        env_file=str(row.get("env_file") or DEFAULT_ENV_FILE),
        cc=split_recipients(row.get("cc") or ""),
    )
    # 中台每封都抄送（老行为）：路径里没写 cc 时补上
    if not mc.cc:
        mc.cc = split_recipients(CENTRAL_ADDR)
    return mc


def _port_or_465(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 465


def load_mail_paths(cfg: dict, root=None) -> list:
    """全部邮件路径；列表空 = 这条渠道没配。

    * 有 `.secrets/push-paths.json` → 按文件里的行；
    * 没文件 → 从老 `mail:` + env **只读回落**成一条（含中台回落，门店不用重填）。

    返回 `(path_id, MailConfig)` 列表 —— id 给界面删改用；老回落给临时 id。
    """
    from . import push_paths
    if push_paths.has_file(root):
        rows = push_paths.load_raw(root).get("mail") or []
        out = []
        for r in rows:
            mc = mail_from_row(r, root)
            out.append((str(r.get("id") or ""), mc))
        return out
    # 老配置回落：load_mail_config 自带中台回落
    mc = load_mail_config(cfg, root)
    if mc is None or not (mc.enabled or mc.ready):
        return []
    if not (mc.host and mc.recipients and mc.from_addr):
        return []
    return [("legacy", mc)]


def mail_configs_only(cfg: dict, root=None) -> list:
    """只要 `MailConfig` 列表（发送扇出用）。"""
    return [mc for _id, mc in load_mail_paths(cfg, root)]


def save_mail_paths(rows, root=None) -> Path:
    """整表写回 mail 侧；wecom 键原样保留。`rows` 是 UI 交下来的 dict 列表。"""
    from . import push_paths
    clean = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            port = int(r.get("port") or 465)
        except (TypeError, ValueError):
            port = 465
        clean.append({
            "id": str(r.get("id") or "") or "",
            "host": str(r.get("host") or "").strip(),
            "port": port,
            "security": str(r.get("security") or "ssl").strip().lower() or "ssl",
            "username": str(r.get("username") or "").strip(),
            "password": str(r.get("password") or ""),
            "sender": str(r.get("sender") or "").strip(),
            "recipients": str(r.get("recipients") or "").strip(),
            "subject_prefix": str(r.get("subject_prefix") or "[报量对账]").strip(),
            "env_file": str(r.get("env_file") or DEFAULT_ENV_FILE),
            "cc": str(r.get("cc") or "").strip(),
        })
    push_paths.ensure_ids(clean)
    data = push_paths.load_raw(root)
    data["mail"] = clean
    if not isinstance(data.get("wecom"), list):
        data["wecom"] = []
    return push_paths.save(data, root)


def describe_mail_paths(cfg: dict, root=None) -> dict:
    """给设置页：路径列表 + 预设。**绝不回传密码**（`has_password` 标有无）。"""
    paths = []
    for pid, mc in load_mail_paths(cfg, root):
        paths.append({
            "id": pid or "legacy",
            "host": mc.host,
            "port": mc.port,
            "security": mc.security,
            "username": mc.username,
            "has_password": bool(mc.password),
            "sender": mc.sender,
            "recipients": ", ".join(mc.recipients),
            "subject_prefix": mc.subject_prefix,
            "cc": ", ".join(mc.cc or []),
            "ready": mc.ready,
            "problems": mc.problems(),
        })
    return {
        "paths": paths,
        "count": len(paths),
        "presets": PRESETS,
        "ready_count": sum(1 for p in paths if p["ready"]),
    }


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
        # when 仍回给老前端万一还读；新设置页不再展示
        "when": mc.when,
        "env_file": mc.env_file,
        "username": mc.username,
        "has_password": bool(mc.password),
        "ready": mc.ready,
        "problems": mc.problems(),
        "presets": PRESETS,
    }


# --------------------------------------------------------------------- 发送
def should_send(mc: MailConfig, has_diff: bool = True,
                ignore_when: bool = False) -> tuple[bool, str]:
    """该不该发。返回 (发不发, 原因)。

    ⚠ 2026-09-22：**不再看 `when` / `has_diff` / `ignore_when`**（设置页已去掉
      发送时机）—— 配了能发的路径就发。参数**签名保留**，业务侧调用点别改。
    """
    if not getattr(mc, "enabled", False):
        return False, "没有邮件推送路径"
    bad = mc.problems()
    if bad:
        return False, "配置不全：" + "、".join(bad)
    return True, ""


def build_message(mc: MailConfig, subject: str, body: str,
                  attachments=(), prefix: Optional[str] = None,
                  to=None) -> EmailMessage:
    msg = EmailMessage()
    # ⚠ **`Date` 头要自己加**：`smtplib.send_message()` **不会**替你补（它只做编码转换）。
    #   少了它的直接后果是**我们自己收信时 `date` 是空的** —— 2026-09-20 实测：
    #   发去中台的那封盘点清单，`fetch.mail` 解析出来 `date` 空、附件也看不见
    #   （附件那半是另一个 bug，已修）。邮件客户端一般显示"服务器收到的时间"，
    #   所以人看不出来 —— 这种"人看不出、程序看得出"的缺字段最容易一直留着。
    msg["Date"] = formatdate(localtime=True)
    # ⚠ `prefix` 给"第二条推送"用：POS 合规跟报量排查是**两条独立的消息**，
    #   都顶着 `[报量对账]` 会让人以为发重了。默认不动（还是配置里那个）。
    msg["Subject"] = f"{mc.subject_prefix if prefix is None else prefix} {subject}".strip()
    msg["From"] = mc.from_addr
    # ⚠ `to`：这一封**只发给这些人**（区长名单）。不给就用配置里那份。
    msg["To"] = ", ".join(to or mc.recipients)
    # ⚠ **中台邮箱每封都抄送**（用户 2026-09-19：「接收时都带上这个」）——
    #   放在 Cc 里（不是 To），收件人一眼看得出"这封还抄送给了谁"。
    #   `smtplib` 的 `send_message` 会自己把 Cc 加进投递名单，不用我们重复塞。
    cc = [x for x in (mc.cc or []) if x and x not in (to or mc.recipients)]
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(body, charset="utf-8")
    for a in attachments:
        # ⚠⚠ **附件有两种形态**（2026-09-21 加加密时定的）：
        #   ① **路径**（老样子）—— 直接读盘；
        #   ② **`(文件名, 字节)` 元组** —— 推送模块（`modules/notify._send_mail`）
        #      加密好之后传下来的。
        # ⚠ 加密那一步**不在这个文件里**：`mailer` 是底层，而"往外发的东西怎么保护"
        #   归推送模块管；底层反过来 import 能力层是**红线**（`test_module_layout.py`
        #   拦着）。所以底下这层只管把字节塞进邮件，它**不认识加密**。
        if isinstance(a, tuple) and len(a) == 2:
            name, data = str(a[0] or ""), bytes(a[1] or b"")
            if not name:
                continue
            msg.add_attachment(data, maintype="application",
                               subtype="octet-stream", filename=name)
            continue
        p = Path(a)
        if not p.is_file():
            continue
        maintype, subtype = _XLSX_TYPE if p.suffix.lower() == ".xlsx" else ("application", "octet-stream")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send(mc: MailConfig, subject: str, body: str, attachments=(),
         prefix: Optional[str] = None, to=None) -> None:
    """同步发送。失败抛 MailError（调用方决定要不要因此失败整个流程）。

    ⚠⚠ `to=` **必须收下并转给 `build_message`**（2026-09-21 真发那一下才发现的）：
      `notify._send_mail` 一直是按 `to=[区长邮箱]` 调的，而这里**签名里没有 `to`**
      ⇒ `TypeError: send() got an unexpected keyword argument 'to'` ——
      **所有"指定收件人"的邮件一封都发不出去**（M18 上报、M21 拆分都是），
      而它们各自的测试都把 `notify.send` 打了桩，所以一路绿灯到真发才露出来。
      （`build_message` 那边本来就有 `to`，只是没人把它递下去。）
    """
    msg = build_message(mc, subject, body, attachments, prefix=prefix, to=to)
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


#: 第二条推送（POS 合规）自己的主题前缀 —— 见 `build_message` 的 prefix 说明
POS_SUBJECT_PREFIX = "[POS 合规]"


POOLS_SUBJECT_PREFIX = "[双平台数据对比]"
#: 销售达成（M5）—— ⚠ **必须传**，不传的话主题会顶着配置里那个 `[报量对账]`，
#: 门店会以为发重了（`build_message` 那段注释写的）。
ATTAIN_SUBJECT_PREFIX = "[销售达成]"
#: 周度目标拆分（发给区长）—— 2026-09-19 加。
SPLIT_SUBJECT_PREFIX = "[目标拆分]"


def build_pools_mail(ctx: dict, lines, headline: str, has_attach: bool = False) -> tuple:
    """双平台数据对比（AD/BC）那封邮件 —— **单独一封**，和报量排查 / POS 分开。

    正文用 `pools.notify_lines` 的输出 —— 和企微**共用同一份格式化**，
    不在这里另排一遍（排两遍迟早有一处忘了改）。
    """
    store = ctx.get("门店", "?")
    tail = [
        "",
        "——",
        "AD = 玲珑报了、云商没报（云商该出库没出）",
        "BC = 云商报了、玲珑没报（门店该报量没报）",
    ]
    if has_attach:
        tail.append("完整清单见附件（串号 / 机型 / 门店 / 单号 / 时间 / 金额）。")
    tail += ["本邮件由 cbg-reconcile 自动发送。",
             f"配置 {ctx.get('配置文件', '')}"]
    body = "\n".join([f"门店 {store}", headline, ""] + list(lines) + tail)
    return f"{store} · {headline}", body


def build_attain_mail(ctx: dict, lines, headline: str) -> tuple:
    """销售达成那封邮件。正文和企微**共用同一份 `lines`**（不另排一遍）。"""
    store = ctx.get("门店", "?")
    subject = f"{store} · {headline}"
    body = "\n".join([f"门店 {store}", headline, ""] + list(lines) + [
        "",
        "——",
        "口径：零售/分销计入，退货冲减（源数据里已是负数）；",
        "「演示机 / 体验机」和串号标识为「外调」的**不计入**。",
        "单项达成率封顶 120%；总达成率 = 各产品列按占比加权平均。",
        "⚠ 周中看的时候数字会偏低 —— 那一周还没过完（正文里写了数据截至哪天）。",
        "本邮件由 cbg-reconcile 自动发送。",
        f"配置 {ctx.get('配置文件', '')}",
    ])
    return subject, body


def build_pos_mail(ctx: dict, lines, headline: str) -> tuple:
    """POS 合规那封邮件。**和报量排查分开两封**（用户 2026-09-16 定的）。

    正文用 `pos_report.notify_lines` 的输出 —— 和企微**共用同一份格式化**，
    不在这里另排一遍。
    """
    store = ctx.get("门店", "?")
    subject = f"{store} · {headline}"
    body = "\n".join([f"门店 {store}", headline, ""] + list(lines) + [
        "",
        "——",
        "口径（官方）：POS 使用率 = 1 −（现金 + 记账）/ 总金额，**先按天算再取日均值**；",
        "现金 = 开钱箱的收款方式，记账 = 渠道名为「记账」的（如公对公打款）。",
        "官方的「不含退货数据」= **退货要扣掉**（算净额，退货当月扣）；",
        "本店数据里另**排除国补 / 即时零售 / Care+**",
        "（那几类在我们库里全走现金，不排除会被补贴砸掉几十分）。",
        "「旧口径」= 非现金/全部（只认「现金」两个字）、整月汇总 —— 留着跟历史报表对账。",
        "⚠ 官方公式里的「异常金额」（成交价低于零售价 >30% 的部分）**暂无数据源**，",
        "按 0 计 —— 这一项会让官方分数比这里算的略低。",
        "本邮件由 cbg-reconcile 自动发送。",
        f"配置 {ctx.get('配置文件', '')}",
    ])
    return subject, body


def build_report_mail(ctx: dict, summary: list[str], missing: int,
                      unshipped: int = 0) -> tuple[str, str]:
    """拼主题和正文。主题要让人不看正文就知道结果。"""
    store = ctx.get("门店", "?")
    day = ctx.get("目标日", "?")
    if missing:
        head = f"❌ 玲珑无但云商有 {missing} 台"
    elif unshipped:
        head = f"⚠️ 玲珑有但云商无 {unshipped} 台"
    else:
        head = "✅ 无差异"
    subject = f"{store} {day} · {head}"
    body = "\n".join(summary)
    body += ("\n\n——\n"
             "本邮件由 cbg-reconcile 自动发送。完整差异清单见附件。\n"
             f"门店 {store} | 配置 {ctx.get('配置文件', '')}\n")
    return subject, body


# ------------------------------------------------------------------ IMAP（收信）
#: 常见邮箱的 IMAP 主机 —— 猜得到就用，猜不到让用户配 `MAIL_IMAP_HOST`
IMAP_HOSTS = {"qq.com": "imap.qq.com", "163.com": "imap.163.com",
              "126.com": "imap.126.com", "sina.com": "imap.sina.com",
              "outlook.com": "outlook.office365.com", "gmail.com": "imap.gmail.com"}


def imap_host_of(addr: str) -> str:
    """邮箱地址 → IMAP 主机（猜不出来就空着，由调用方报错说清）。"""
    dom = str(addr or "").split("@")[-1].strip().lower()
    return IMAP_HOSTS.get(dom, ("imap." + dom) if dom else "")


def imap_config(cfg: dict, root=None, *, platform=None) -> dict:
    """收信该用哪个账号 —— 用户 2026-09-20 定的规矩。

    ⚠⚠ **先把两个方向分清楚**（用户随后纠正过我的说法：
      「中台邮箱只给平台岗用**不准确**啊，**门店的还是可以用它默认发邮件**的，
        当然如果门店配置了邮件**且发送成功了**，那就不用中台发邮件了」）：

    | 方向 | 谁用中台邮箱 |
    |---|---|
    | **发信**（SMTP，`load_mail_config` 那条路） | **谁都可以**：门店没配自己的 ⇒ 默认用中台发；配了自己的**且能发** ⇒ 就不用中台 |
    | **收信**（IMAP，就是本函数） | **只有平台岗**：门店 / 区长没配自己的 ⇒ **不读邮箱** |

    ⇒ 也就是："中台邮箱只给平台岗用"这句话**只对收信成立**。

    ⇒ 判定顺序：
      ① 这台机器**自己配了发件账号** ⇒ 用它自己那个（**任何角色都成立**）；
      ② 没配自己的 **且是平台岗** ⇒ 用**中台邮箱**（授权码 SMTP/IMAP 通用）；
      ③ 没配自己的 **且不是平台岗**（门店 / 区长）⇒ **返回 `{}`，不读邮箱**。

    ⚠ `platform` 不给就自己判断（`store_profile().type == "platform"`）——
      调用方也可以直接传（比如自检里已经算过一次画像）。
    """
    mc = load_mail_config(cfg, root)
    # ⚠ `load_mail_config` **自己就会回落到中台**（那是发信的规矩）——
    #   所以不能靠"它 ready 了"判断用的是谁，得**比账号地址**（实测踩过）。
    own = (mc.username or mc.from_addr) != CENTRAL_ADDR and bool(mc.password)
    if not own:
        if platform is None:
            try:
                from . import config_io
                platform = (config_io.store_profile(
                    config_io.pick(cfg or {}), root).get("type") == "platform")
            except Exception:                                  # noqa: BLE001
                platform = False
        if not platform:
            # ⚠⚠ 2026-09-21 **改过**（真发那一下发现的）：原来这里对门店/区长
            #   `return {}`（"不读邮箱"）。可 M18/M19 的设计是
            #   「门店上报 → 邮件 → **区长**收信落库」，店长的邮件**抄送中台** ——
            #   区长机器要是没配自己的收件账号，就**一封都收不到**，
            #   而"区长今晚看到"直接变成"永远看不到"，界面上只有一句"没配收信"。
            #   ⇒ 现在的规矩跟**发信**一致：没配自己的 ⇒ 用中台那份（IMAP 跟 SMTP
            #     共用同一个授权码）。⚠ 中台邮箱里有**全区**的邮件 ——
            #     "只能看自己辖区的"靠 `role_scope()` 筛（跟库里全有那份一个道理）。
            pass
        central = central_config(root)
        if central is None:
            return {}
        mc = central
    source = "central" if (mc.username or mc.from_addr) == CENTRAL_ADDR else "store"
    user = mc.username or mc.from_addr
    host = str((cfg or {}).get("mail", {}).get("imap_host") or "").strip() \
        or os.environ.get("MAIL_IMAP_HOST") or imap_host_of(user)
    if not (host and user and mc.password):
        return {}
    return {"host": host, "port": 993, "user": user, "password": mc.password,
            "source": source, "from_addr": mc.from_addr or user}


def imap_problems(info: dict) -> list:
    """收信配置缺什么（给界面/日志说人话）。"""
    miss = []
    if not info:
        return ["这台机器没有自己的发信账号，所以**不读邮箱**"
                "（收信只有平台岗会去读中台邮箱；门店 / 区长要读得先配自己的邮箱）"
                "—— ⚠ 但**发信**不受影响：没配自己的照旧默认用中台发"]
    if not info.get("host"):
        miss.append("IMAP 服务器（这个域名猜不出来，请在设置里填）")
    if not info.get("password"):
        miss.append("密码/授权码")
    return miss
