"""华为 CBG 会话管理。

cookie 从哪来：人在门店电脑的浏览器里 F12 → Network → 找任意一条 cbg 请求
→ 右键 Copy as cURL，把那一整坨粘进来。华为用的是 SSO cookie
（`hwssot3` / `WPSESSIONID` / `HWSTORE-SESSION` …）**外加**请求头 `x-csrf-token`，
两者同生共死 —— 必须成对更新，只换一个必然 403。

粘贴的内容可能是 Windows cmd 风格（`^"` 转义）也可能是 bash 风格，这里都吃。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CBG_BASE = "https://cbg.huawei.com"

# 这些 cookie 跟登录态无关，留着只会把请求头撑到几 KB
_NOISE_PREFIX = ("_ga", "_ce", "Hm_", "HMACCOUNT", "cebs", "ce.", "idss_", "cbg_wp_lang")

# 少一个就登不上
_REQUIRED_ANY = ("JSESSIONID", "HWSTORE-SESSION", "hwssot3", "WPSESSIONID")


class CbgAuthError(RuntimeError):
    """会话相关的问题（过期 / 权限不足 / 没抓到 cookie）。"""


def _unescape_cmd(text: str) -> str:
    r"""Windows cmd 的 curl 转义还原。

    实测遇到的形态：`-H ^"accept: ...^"`、`^\^"Chromium^\^"`、`Asia^%^2FShanghai`。
    顺序不能反：先处理 `^\\^"`，再处理 `^"`，最后 `^%`。
    """
    return (
        text.replace('^\\^"', '"')
        .replace('\\^"', '"')
        .replace('^"', '"')
        .replace("^%", "%")
    )


def _clean_cookies(raw: str) -> str:
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", k):
            continue
        if k.startswith(_NOISE_PREFIX) or not v or " " in v:
            continue
        out.append(f"{k}={v}")
    return "; ".join(out)


@dataclass
class CbgSession:
    cookies: str
    csrf: str
    source: str = ""
    extra: dict = field(default_factory=dict)

    # ---- 解析 ----
    @classmethod
    def from_curl(cls, text: str) -> "CbgSession":
        s = _unescape_cmd(text)

        m = (re.search(r"(?:-b|--cookie)\s+\"([^\"]*)\"", s)
             or re.search(r"(?:-b|--cookie)\s+'([^']*)'", s)
             or re.search(r"(?:-b|--cookie)\s+([^\s\\]+)", s))
        if m:
            raw = m.group(1)
        else:  # 没有 -b，就直接找 cookie 串
            m2 = re.search(r"(JSESSIONID=[^\"'\n]+)", s)
            if not m2:
                raise CbgAuthError("这段文本里找不到 cookie（既没有 -b 参数，也没有 JSESSIONID=）")
            raw = m2.group(1)

        cookies = _clean_cookies(raw)
        names = {c.split("=", 1)[0] for c in cookies.split("; ") if c}
        if not names & set(_REQUIRED_ANY):
            raise CbgAuthError(
                f"cookie 里缺少登录凭据（至少要有一个：{' / '.join(_REQUIRED_ANY)}），"
                f"现在只有：{sorted(names)}。八成是复制时漏了 -b 那一行。"
            )

        m = re.search(r"x-csrf-token:\s*([A-Za-z0-9\-_.]+)", s)
        if not m:
            raise CbgAuthError("找不到 x-csrf-token 请求头 —— 复制 curl 时要把 -H 那些行一起带上")
        csrf = m.group(1)

        return cls(cookies=cookies, csrf=csrf, source="curl")

    # ---- 使用 ----
    def headers(self, role: str = "Store_Manager") -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": CBG_BASE,
            "referer": f"{CBG_BASE}/",
            "role-code": role,
            "x-csrf-token": self.csrf,
            "cookie": self.cookies,
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
        }

    def describe(self) -> str:
        """日志用：只露 cookie 名和 csrf 前后 4 位，别把凭据写进日志。"""
        names = [c.split("=", 1)[0] for c in self.cookies.split("; ") if c]
        csrf = f"{self.csrf[:4]}…{self.csrf[-4:]}" if len(self.csrf) > 8 else "****"
        return f"cookie[{len(names)}]: {', '.join(names)} | csrf: {csrf}"

    # ---- 落盘 ----
    def save(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cookies": self.cookies, "csrf": self.csrf},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    @classmethod
    def load(cls, path) -> "CbgSession":
        path = Path(path)
        if not path.exists():
            raise CbgAuthError(
                f"没有会话文件 {path}。先抓一份 curl 导入：\n"
                f"  python -m src.cli auth --from-curl <把 curl 存成的文件>"
            )
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(cookies=d["cookies"], csrf=d["csrf"], source=str(path))
