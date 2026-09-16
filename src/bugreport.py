"""把"出问题时的现场"打成一个包 —— 一键上报。

## ⚠ 顺序是死的：**先落盘，再谈推送**

上报通道**不能只依赖出问题的那条通道**。最常见的 bug 恰恰是"推送失败"——
webhook 的 key 过期、群机器人被删、SMTP 认证失败、门店网络不通。
这时候用推送去上报推送的故障，**必然也失败**。

所以：

    ① 收集 + 打包 → 落到 out/report-bug-<时间>.zip   ← 不依赖网络，一定能成
    ② 试着发邮件（附件）、试着发企微（文件）          ← 两条**各自独立**
    ③ 发不出去就把①那个包的路径摆给用户看              ← 人工发，永远有退路

## ⚠ 绝不能进包的东西

* `.secrets/*.env`（云商账号、邮箱授权码、企微 webhook key）
* `.secrets/cbg-*.json`（华为会话 —— 拿着它就能以门店身份查数据）
* `.secrets/browser-profile/`（浏览器登录态）
* `.secrets/huawei.env`（华为账号密码）
* `out/cbg-*.db`（业务数据，几 MB）

`_FORBIDDEN` 是一张**显式黑名单**，而且 `collect()` 最后会**反查一遍**
（`assert_no_secrets`）—— 跟打包脚本一个套路：排除 + 反查，两道。
只排除不反查的话，哪天有人加了个新文件、又忘了加排除，就静默漏出去了。
"""

from __future__ import annotations

import datetime
import io
import json
import os
import platform
import sys
import zipfile
from pathlib import Path

CST = datetime.timezone(datetime.timedelta(hours=8))

#: 包里**只放这些文件名**。白名单比黑名单安全 —— 黑名单漏一个就漏出去了，
#: 白名单漏一个只是少一份信息。
SECRET_PATTERNS = (
    ".env",                      # 任何 *.env 都不进
    "cbg-",                      # 华为会话 cbg-<门店码>.json
    "browser-profile",
    "huawei.env",
)

#: 日志最多取尾部这么多（**字节**）。见模块注释里的"日志不轮转"那段。
LOG_TAIL_BYTES = 2 * 1024 * 1024

#: 打进去的文本文件里，这些行会被打码（第二道防线）。
_REDACT_HINTS = ("password", "passwd", "授权码", "auth_code", "secret",
                 "token", "webhook", "key=", "apikey", "api_key",
                 "username", "user=", "账号", "密码")


def _now():
    return datetime.datetime.now(CST)


def _redact_line(line: str) -> str:
    """看着像凭据的行打码。

    ⚠ 这是**第二道防线**，不是第一道 —— 第一道是"根本不把 .secrets 放进来"。
    打码只保证"就算哪天混进来一行，也不会原样发出去"。
    """
    low = line.lower()
    if any(h in low for h in _REDACT_HINTS):
        head = line.split(":", 1)[0].split("=", 1)[0][:60]
        return head + "：（已打码）"
    return line


def tail_log(path: Path, limit: int = LOG_TAIL_BYTES):
    """取日志尾部。返回 `(文本, 说明)`。

    ⚠ `out/run.log` **只追加、从不轮转** —— 门店跑几个月能涨到几十 MB，
    整个塞进包里既发不出去（企微 20MB）也没人看。所以只取尾部，
    并在开头写明截了多少。
    """
    if not path.is_file():
        return "", "（没有 %s）" % path.name
    try:
        size = path.stat().st_size
    except OSError as e:
        return "", "（读不到 %s：%s）" % (path.name, e)
    try:
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
                # ⚠ 别从半个汉字中间切 —— 多丢一行也比出现乱码强
                f.readline()
            raw = f.read()
    except OSError as e:
        return "", "（读不到 %s：%s）" % (path.name, e)
    text = raw.decode("utf-8", "replace")
    if size > limit:
        text = ("【这里只有日志的**最后 %d KB】**（全文 %d KB）——"
                "完整的那份在门店电脑的 out\\run.log 里。\n\n"
                % (limit // 1024, size // 1024)) + text
    return text, ""


def _read_text(path: Path, limit: int = 512 * 1024) -> str:
    try:
        raw = path.read_bytes()[:limit]
    except OSError as e:
        return "（读不到 %s：%s）" % (path.name, e)
    return raw.decode("utf-8", "replace")


def environment_text(root: Path) -> str:
    """环境摘要 —— 比 `diagnose.bat` 轻（不跑子进程、不真去登录）。"""
    from . import version
    lines = [
        "版本：%s" % version.describe(),
        "Python：%s" % sys.version.replace("\n", " "),
        "解释器：%s" % sys.executable,
        "系统：%s %s（%s）" % (platform.system(), platform.release(), platform.machine()),
        "工作目录：%s" % root,
        "生成时间：%s" % _now().strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "依赖：",
    ]
    for mod in ("requests", "yaml", "openpyxl"):
        try:
            m = __import__(mod)
            lines.append("  ✅ %-10s %s" % (mod, getattr(m, "__version__", "?")))
        except Exception as e:                                # noqa: BLE001
            lines.append("  ❌ %-10s %s" % (mod, e))
    lines += ["", "关键文件："]
    for name in ("run.bat", "run-now.bat", "run_check.py", "bootstrap.py",
                 "boot.py", "BUILD.txt", "requirements.txt"):
        p = root / name
        lines.append("  %s %s" % ("✅" if p.is_file() else "❌", name))
    lines += ["", "目录："]
    for name in ("config", ".secrets", "out"):
        p = root / name
        if p.is_dir():
            try:
                n = len(list(p.iterdir()))
            except OSError:
                n = -1
            lines.append("  ✅ %-10s（%d 项）" % (name, n))
        else:
            lines.append("  ❌ %s" % name)
    # run 脚本的内容 —— 迁移那类问题全靠它
    for name in ("run.bat", "run.sh", "run-now.bat", "run-now.sh"):
        p = root / name
        if p.is_file():
            lines += ["", "── %s ──" % name, _read_text(p, 64 * 1024)]
    return "\n".join(lines)


def schedule_text(root: Path) -> str:
    """定时任务的现场。

    `.secrets/schedule.json` **可以进包** —— 它只有任务名 / 时间 / 目标日 /
    勾选项，没有任何凭据。而它是"界面读不到 Windows 任务详情"时唯一的线索，
    排查定时任务问题非它不可。
    """
    from . import schedule as sch
    lines = []
    try:
        lines.append("任务脚本：%s" % sch.script_path(root))
        lines.append("脚本存在：%s" % sch.script_path(root).exists())
        lines.append("认出来的勾选：%s" % (sch.existing_steps(root),))
    except Exception as e:                                    # noqa: BLE001
        lines.append("（读脚本失败：%s）" % e)
    try:
        st = sch.status(root)
        lines += ["", "status()：",
                  json.dumps({k: v for k, v in st.items() if k != "tasks"},
                             ensure_ascii=False, indent=1, default=str),
                  "", "任务列表：",
                  json.dumps(st.get("tasks") or [], ensure_ascii=False, indent=1,
                             default=str)]
    except Exception as e:                                    # noqa: BLE001
        lines.append("（status() 失败：%s）" % e)
    rec = sch.record_path(root)
    if rec.is_file():
        lines += ["", "注册记录（.secrets/schedule.json，无凭据）：",
                  _read_text(rec, 64 * 1024)]
    return "\n".join(lines)


def config_text(root: Path, config_path) -> str:
    """门店配置。

    ⚠ 这份 yaml 里**只有指向 .secrets 的路径**，凭据本身在 `.secrets/*.env`
    里（界面填的、不回显）。所以它可以进包 —— 但**仍然逐行过一遍打码**，
    万一哪天有人把密码写进 yaml 了呢。
    """
    p = Path(config_path)
    if not p.is_absolute():
        p = root / p
    if not p.is_file():
        return "（找不到配置：%s）" % p
    head = ("# 门店配置（%s）\n"
            "# ⚠ 逐行过了打码；凭据本身在 .secrets/*.env 里，不在这个文件\n\n" % p.name)
    body = "\n".join(_redact_line(x) for x in _read_text(p, 256 * 1024).splitlines())
    return head + body


def readme_text(root: Path, config_path) -> str:
    return """这个包里是什么
================================================================

给修的人：门店那边点了「上报 bug」，这是自动收集的现场。
解压后按下面的顺序看就行。

  执行日志.txt    ← **先看这个**。就是 out/run.log 的尾部，出错的现场在这
  定时任务.txt    ← 定时任务的注册情况、run.bat 内容、界面读到的参数
  配置.yaml       ← 门店配置（只有路径，没有账号密码）
  环境.txt        ← 版本 / Python / 系统 / 依赖 / 关键文件在不在

什么**没有**在这个包里（故意的）
----------------------------------------------------------------

  .secrets\\ 里的全部东西 —— 云商账号、华为会话、邮箱授权码、
                   企业微信 webhook 地址、浏览器登录态
  out\\cbg-*.db    订单库（业务数据，几 MB）

也就是说：这个包里**没有能拿去登录的凭据**，可以放心发。

⚠ 但**有业务数据**：日志里会出现门店名、串号、金额。
   发之前确认一下收件人是自己人。

生成时间：%s
工作目录：%s
配置：%s
""" % (_now().strftime("%Y-%m-%d %H:%M:%S"), root, config_path)


def collect(root, config_path) -> "list[tuple[str, bytes]]":
    """收集要进包的东西。返回 `[(包内文件名, 内容字节)]`。

    ⚠ **先把内容收齐，再写 zip** —— 收集中途出错的话不会留下半个包。
    """
    root = Path(root)
    out_dir = root / "out"

    log_text, log_note = tail_log(out_dir / "run.log")
    if log_note:
        log_text = log_note
    items = [
        ("说明.txt", readme_text(root, config_path)),
        ("执行日志.txt", log_text),
        ("定时任务.txt", schedule_text(root)),
        ("配置.yaml", config_text(root, config_path)),
        ("环境.txt", environment_text(root)),
    ]
    # 差异报告的摘要（旁车 json）—— 小、能看出对账到底跑没跑、结果如何。
    # ⚠ 不含 xlsx（那里面是完整串号清单，且体积大）。
    for name in ("run.log.1",):
        p = out_dir / name
        if p.is_file():
            items.append(("执行日志.上一份.txt", tail_log(p, 512 * 1024)[0]))
    reports = sorted(out_dir.glob("*.json"))[-6:]
    if reports:
        blob = []
        for p in reports:
            blob.append("── %s ──\n%s" % (p.name, _read_text(p, 32 * 1024)))
        items.append(("报告摘要.txt", "\n\n".join(blob)))

    encoded = []
    for name, text in items:
        # 第二道防线：进包前逐行过一遍打码
        safe = "\n".join(_redact_line(x) for x in str(text).splitlines())
        encoded.append((name, safe.encode("utf-8")))
    assert_no_secrets(encoded)
    return encoded


def assert_no_secrets(items) -> None:
    """⚠ **反查**：包里的每一项都不许是 `.secrets` 下的东西。

    跟打包脚本一个套路（`rsync --exclude` + 反查断言，两道）。
    只排除不反查的话，哪天有人加了个新条目、又忘了加排除，就静默漏出去了 ——
    而漏出去的可能是"拿着就能以门店身份查数据"的华为会话。
    """
    for name, blob in items:
        if any(pat in name for pat in SECRET_PATTERNS):
            raise ValueError("⛔ 包里混进了疑似凭据文件：%s" % name)
        # 内容层面再兜一道：华为会话/凭据文件的特征串
        text = blob[:200000].decode("utf-8", "replace")
        for needle, why in (("cbgSession", "华为会话内容"),
                            ("csrfToken", "华为会话内容"),
                            ("ERP_PASSWORD=", "云商密码"),
                            ("QQ_EMAIL_AUTH_CODE=", "邮箱授权码"),
                            ("qyapi.weixin.qq.com/cgi-bin/webhook/send?key=", "企微 webhook"),
                            ("BEGIN RSA", "私钥")):
            if needle in text:
                raise ValueError("⛔ %s 里出现了 %s —— 不该进包" % (name, why))


def build_zip(root, config_path, out_dir=None) -> Path:
    """收集 → 打包。**返回包的路径，这一步不碰网络。**"""
    root = Path(root)
    out_dir = Path(out_dir) if out_dir else (root / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / ("report-bug-%s.zip" % stamp)
    items = collect(root, config_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in items:
            z.writestr(name, blob)
    # ⚠ 先写临时文件再 rename：中途断电/被杀不会留下半个 zip
    #   （半个 zip 比没有更糟 —— 用户会以为"上报成功了"）
    tmp = path.with_suffix(".zip.part")
    tmp.write_bytes(buf.getvalue())
    os.replace(str(tmp), str(path))
    return path
