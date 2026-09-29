"""pytest 全局配置 —— **别删这个文件**。

⚠ `CBG_NO_DB_REBUILD` 是**保命用的**：`src/dbmigrate.py` 会在 `daily` 开头
把 `out/cbg-<年>.db` **改名**（2.1.0 的一次性库重建）。门槛靠 `BUILD.txt`，
而开发机上项目根**可能正好存在**一个同名文件（测试/脚本写的）——
2026-09-17 就这么把开发机的 77MB 真库改过名。

设上这个环境变量之后，**任何测试都不可能触发那一步**。
（那次能救回来，是因为它是"改名"不是"删"——见 `dbmigrate` 顶部。）
"""

import os

os.environ.setdefault("CBG_NO_DB_REBUILD", "1")


# ─────────────────── 测试起的 HTTP 服务：shutdown 别等满 0.5s ───────────────────
#
# ⚠ 这条是**测试提速的头号来源**（2026-09-29 实测）：
#   `ThreadingHTTPServer.serve_forever()` 的默认 `poll_interval=0.5`，
#   而 `shutdown()` 要等**下一次轮询**才看得到停止标志 ⇒ 每个 `close()`
#   固定卡 0.5 秒。9 个测试文件都在 setUp 里真起服务、tearDown 里 shutdown
#   ⇒ 光这一项就吃掉 **约 86 秒**（全量 170s → 84s，见 git log）。
#
#   现象很隐蔽：`--durations` 看不出哪条慢（那 0.5s 记在**拆 fixture 的
#   teardown** 上，而 teardown 不进 durations 的 call 段），所以它一直被
#   当成"测试条数太多"。**测试条数一条都没少**（2859 条，24 秒跑完）。
#
#   改成 0.01s 只影响"多久发现该停了"，不改任何语义 —— 真正的断言都在
#   请求/响应上，跟轮询周期无关。
from http.server import HTTPServer as _HTTPServer

_serve_forever = _HTTPServer.serve_forever


def _fast_serve_forever(self, poll_interval=0.01):
    return _serve_forever(self, poll_interval)


_HTTPServer.serve_forever = _fast_serve_forever


# ─────────────────────── 测试**不许打真外网** ───────────────────────
#
# ⚠ 第二大耗时来源（2026-09-29 实测）：**只有 8 条测试**在真发 HTTP，
#   被本机代理（7897 拒绝连接）挡住后走 `_with_net_retry` 的 1.5s + 3.0s 退避
#   ⇒ 一条测试吃掉 13 秒，8 条合计约 **60 秒**，而且**结果取决于当天网络**
#   （`test_staff_report` 那条就因此红过 —— 接口把 `ok` 回成了 false）。
#
# 现在在这儿兜一道：**回环地址放行**（测试真起的 127.0.0.1 服务不算），
# 其余一律**立刻**抛错，且抛的**不是** `SSLError / ConnectionError / Timeout`
# —— 这样 `_is_transient_net_error()` 判它为"非瞬时错" ⇒ **当场 raise、不重试**。
# 于是"忘了打桩"从「等 60 秒 + 结果看天」变成「立刻红 + 栈里指名道姓」。
#
# ⚠ 已有的重试测试（`test_erp_net_retry` / `test_tdoc`）都把 session mock 成
#   `MagicMock`，压根不进这里，不受影响。

# ───────────── 回环地址不许走代理（2026-09-29 第三次提速，150s → 26s）─────────────
#
# ⚠ 这条**只管测试进程自己**，改不了系统设置：macOS 的**系统代理**
#   （`scutil --proxy` → 127.0.0.1:7897）在管事，而 `urllib` 读的正是它 ——
#   于是测试里探**本机** CDP 端口 `http://127.0.0.1:12345/json/version`
#   也被送进代理，代理连不上那个端口就回 **502**，每次 1~2 秒。
#
#   现象同样是"测试条数太多"的假象：`test_browser.py` 105 条全量要 **140 秒**
#   （一条 27 秒），而 `_nav_watch()` 明明把 `time.sleep` 打桩成 no-op 了 ——
#   因为卡的是 **urllib 的 socket 读**，不归假时钟管。
#   ⚠ 更坑的是它**随代理开合漂移**：代理没开时 Connection refused 是 0 秒，
#   一开就慢。同一批代码，上午 26 秒、下午 150 秒，很容易误判成"谁改坏了"。
#
#   ⇒ `no_proxy` 指回环地址 ⇒ urllib 直连 ⇒ 立刻 refused。实测同一条
#   `test_headless_never_navigates_to_the_login_page`：**26s → 0.02s**。
#   ⚠ 这也**不会**削弱上一道外网总闸：总闸拦的是"真外网"，而这里放行的是
#   "本机服务"——两道闸的口径本来就是一致的（回环=本机，其余=外网）。
import os as _os

for _k in ("no_proxy", "NO_PROXY"):
    _os.environ[_k] = "127.0.0.1,localhost,::1"

from urllib.parse import urlparse as _urlparse

import requests as _requests


class _TestNetworkBlocked(RuntimeError):
    """测试里发起了真外网请求 —— 打桩掉它，别连出去。"""


_orig_session_request = _requests.Session.request


def _guarded_session_request(self, method, url, *args, **kw):
    host = (_urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return _orig_session_request(self, method, url, *args, **kw)
    raise _TestNetworkBlocked(
        "测试不许打真外网：%s %s —— 这条要联网就用 mock.patch 打桩"
        % (method, url))


_requests.Session.request = _guarded_session_request


# ─────────────────────── 测试**不许往项目根写东西** ───────────────────────
#
# ⚠ 这条是**用真金白银换来的**（2026-09-19 一天里踩了三次）：
#   `runlog.record(root=None)` / `attain.run(root=None)` 这些的 `root=None`
#   = **项目根**（那是给生产用的默认值）。测试里忘了传临时 root，
#   就会往开发机的 `out/` 里写：跑过 28 行假 `notify:*` 记录（健康面板跟着误报
#   "sms 连着失败 7 次"），也覆盖过真的 `out/attain-2026.json`。
#
# 光靠"我记得传 root"拦不住 —— 每次加一个会写盘的新步骤，都要把所有相关测试
# 过一遍补桩，而漏掉的那个测试**只在开发机上才看得出问题**。
# 所以在这儿兜一道：**跑之前拍一张快照，跑完比对**，多出来的文件直接报错。
#: ⚠ `in/`（2026-09-21 晚）也要盯：它是**收进来的东西**（各店发来的上报包 + 收信库），
#:   跟 `out/` 一样属于"这台电脑自己的"，忘了传临时 root 照样会写进开发机。
_WATCH_DIRS = ("out", "in")


_OUT_DIRS = _WATCH_DIRS          # 老名字，别再引用


def _snapshot():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    got = set()
    for d in _WATCH_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            for name in os.listdir(p):
                got.add(d + "/" + name)
    return got


def pytest_sessionstart(session):
    session._cbg_files_before = _snapshot()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session, "_cbg_files_before", None)
    if before is None:
        return
    new = sorted(_snapshot() - before)
    if new:
        # ⚠ 只**报**不失败（`pytest_sessionfinish` 改不了退出码）——
        #   但这条红字足够定位：说明某个测试在往项目根写东西。
        print("\n" + "!" * 70)
        print("⚠ 测试往项目根写了新文件（大概率是忘了传临时 root）：")
        for n in new:
            print("    " + n)
        print("  查法：`ls -l` 看 mtime 落在哪个测试；那个测试的 root= 要传 tmp。")
        print("!" * 70)
