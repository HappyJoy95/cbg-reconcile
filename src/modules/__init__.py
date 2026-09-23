"""**系统级模块**（能力层）—— 一个模块一个文件夹，对外只暴露一个入口。

用户 2026-09-19：「系统级的模块包括**登录验证、数据抓取、计时、主题和壁纸、推送**……
（加一个）**系统健康状态**」+「把代码文件**按模块分好**」。

```
modules/
  auth/     登录验证      → 账号信息 + 三套登录态的自检（云商 / 华为会话 / 华为 SSO）
  fetch/    数据抓取      → 数据源登记表（用什么抓、落在哪）+ 新鲜度（M14 的五态）
  notify/   推送          → **只管发**（收到渠道编码 + 内容）+ 记录各个渠道
  timer/    计时          → 几点跑、跑哪几步（唤醒计划来自功能注册表）+ 开机自启
  health/   系统健康状态  → 汇总证据 + 启动自检 + **自动更新的策略**
  theme/    主题和壁纸    → 有哪些主题（从 `web/theme.css` 扫）+ 前端文件自检 + 壁纸口径
```

⚠⚠ **模块是"对外入口"，不搬家实现**（设计基线 §一·八）：
`notify` 内部调的还是 `integrations` 那一层的 `mailer.py` / `wecom.py`；
`timer` 调的还是 `schedule.py` / `autostart.py`；真正干活的代码**大部分没动**。
这样"只做接入"能尽快兑现，而大搬家可以慢慢来。

已搬进来的只有那些**本来就是"口径"的东西**：`auth.session_path`
（会话文件路径，以前住在 `cli.py` —— 入口层拼路径，`web.py` 和自检各拼一次迟早拼歪）。

⚠ 两条规矩（有测试钉着）：

1. **模块之间可以互相调用，但不许成环**（用户 2026-09-19：「互相也有调用」）——
   成环的代价是"改一个牵动另一个" + 初始化顺序变玄学；
2. **健康模块的"写"在 `storage/runlog.py`**（最底层，谁都能调），
   "读/汇总"在 `modules/health/`（它对各来源**懒加载**）—— 这样谁都不会反过来依赖它。

## 业务侧该怎么接（用户要的"只做接入"）

```python
from ..modules import auth, fetch, notify, timer      # 四个出口，一眼看全

acc = auth.accounts(cfg)                  # 我是谁、哪家店（**不回密码**）
st = fetch.state(root, need=...)          # 数据能不能算（五态）
if 该推: notify.send("wecom", {...}, cfg=cfg)
timer.steps()                             # 有哪些步骤可以被唤醒
```
"""

from __future__ import annotations

#: 六个系统模块。**显式清单**（跟 `features/__init__.py` 的 `ALL` 一个规矩：
#: 不扫目录、不反射 import —— 出问题时要一眼看到谁接了进来）。
#: ⚠ `tests/test_modules.py::test_目录里不许有没登记的模块` 两头钉着。
MODULES = ('auth', 'fetch', 'notify', 'timer', 'health', 'theme')
