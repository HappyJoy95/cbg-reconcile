"""构建指纹 —— 一眼看出这台电脑上跑的是哪一版。

**为什么需要这个**：门店电脑上跑的常常是**拷过去的旧版本**。
旧版本的报错文案、界面、行为都跟新版本不一样，但没有任何地方能看出来 ——
于是变成"我明明修好了 / 你那边怎么还报这个"，白白来回一整轮。

有了它：
* `selftest.bat` 第 0 节会打印构建时间；
* 控制台右上角会显示；
* `/api/health` 里也有。

三个来源，按优先级：

1. `BUILD.txt` —— 打包时 `tools/build_package.sh` 写时间戳；从 GitHub 自更新
   之后由 `src/selfupdate.py` 写成 `GitHub main · v1.2.0`。
2. `.git/HEAD` —— **clone 下来的仓库**没有 `BUILD.txt`（那文件是未跟踪的），
   但 `.git` 里就有分支和 commit。直接读文件，**不调 git 命令**：
   门店电脑上不装 git，装到一半的机器上也未必有。
3. 都没有 —— 写"源码运行（未打包）"，说明这棵树上什么线索都没有。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

APP_NAME = "CBG报量对账"
VERSION = "2.1.1"           # 功能有增删就动这个；打包时间看 BUILD.txt
                            # 2.1.1：**beta 包能升回同号的正式版**
                            #        ① 现象：装过 `2.1.0-beta3` 的机器 VERSION
                            #           也是 2.1.0，正式版推上去之后
                            #           `2.1.0 > 2.1.0` 不成立 ⇒ **永远看不到
                            #           「有新版本」，卡在 beta 上**。
                            #           （2.0.1 那次是"顺手升一位"绕过去的，
                            #           见下面 2.0.1 的第 ④ 条 —— 权宜；
                            #           正式版一旦跟 beta 同号就没救了。）
                            #        ② 规则：`远端 > 本地`，**或**
                            #           `远端 == 本地 且 本地是 beta 包`。
                            #           判据只能是 `BUILD.txt` 里的 beta 标记
                            #           （版本号同号，分不出来）。
                            #        ③ 判据抽成 `version.is_beta()`，
                            #           `dbmigrate` 那道安全门槛也用它。
                            # 2.1.0：**四池对账（玲珑 ↔ 云商）**
                            #        ① 四个数据池同库（还是 `out/cbg-<年>.db`）：
                            #           A 玲珑销售单（已有）/ B 玲珑在库 / C 云商销售单 /
                            #           D 云商在库；列存全、快照留 30 天
                            #        ② **归一到一个判据**：玲珑的在库清单 = "这台机器
                            #           现在归本店报量"。原先想从云商「串号标识」推归属，
                            #           实测**证伪**（那是首次采购入库店，不跟调拨变）
                            #        ③ 四象限出两个清单：
                            #           **AD = 玲珑报了、云商没报**（云商该出库没出）
                            #           **BC = 云商报了、玲珑没报**（门店该报量没报）
                            #           明细到串号 / 机型 / 门店 / 单号 / 时间 / 金额，
                            #           出 Excel，邮件和企微都带附件
                            #        ④ **推送记忆**：同一个串号连着几天推（批发单云商
                            #           先报、玲珑过后才报）不再每天重复强调 ——
                            #           第一次标 `★ 新`，之后写「已推 N 次，首次 X」；
                            #           设置页可手动清除
                            #        ⑤ 第 1 步从「抓华为数据」扩成**「抓四池数据」**
                            #           （四池**不给单独按钮**，在「整个项目」里跑）
                            #        ⑥ 口径改名：原来的「未报量 / 调拨货查无出库」
                            #           统一成上面的 AD / BC
                            # 2.0.1：**更新日志弹窗 + 升级记录 + 大版本升级推送**
                            #        ① 每次更新后第一次打开控制台，弹一次
                            #           「这一版改了什么 + **你要做什么**」
                            #           （内容在 src/whatsnew.py，跟着代码走 ——
                            #           发布说明.md 不在 git 里，自更新拿不到新的）
                            #        ② 检测到**大版本升级**（1.x → 2.x）就把这份提醒
                            #           **推**到邮箱/企微 —— 弹窗只有开了控制台才看得到，
                            #           而门店的日常是"它自己跑，我不看"
                            #        ③ 「设置 → 检查更新」下面显示**升级记录**
                            #        ④ ⚠ 也是给 **beta 包**收尾：beta 的版本号也是
                            #           2.0.0，跟正式版**同号 ⇒ 自更新不会换**，
                            #           装过 beta 的机器要靠这次升版才能回到正式版
                            # 2.0.0：**POS 使用率合规 + 本地订单库**
                            # 2.0.0：**POS 使用率合规 + 本地订单库**
                            #        ① 华为订单落 SQLite（`out/cbg-<年>.db`，一年一个库，
                            #           自包含；全字段保留，供以后扩展）
                            #        ② 报量对账的华为侧**改从库里读** —— 顺带修掉两个真误报：
                            #           已退货原单不再被 `returnStatus=0` 整张滤掉；
                            #           一个 SN 挂多张单时取"非服务产品"那张
                            #           （原来是后写覆盖先写，实测会取到 Care+ 服务单）
                            #        ③ 一条 `daily` 跑完日常流程：抓华为当月 → 报量对账 → 算 POS
                            #           ⚠ 第 1 步失败就**跳过 2、3 直接报错**：库不新鲜时
                            #           差集会把当天所有销售算成「未报量」，一份看着很合理的假清单
                            #        ④ POS 使用率看板（新标签页）：分母=非国补&非即时零售&非 Care+，
                            #           分子=非现金支付额；退货在**退货当月**按原单口径扣减；
                            #           国补没有机器可读标记，所以给「按标签/按备注 × 现状/申诉后」
                            #           四个数；最近两个月标「暂定」（上个月的分数还会被
                            #           这个月的退货改）
                            #        ⑤ 会话失效时在第 1 步**静默续期**（无头、不弹窗；
                            #           自检不过就不覆盖好会话）
                            #        ⑥ 「运行」页四个按钮（整个项目 / 抓数据 / 报量排查 /
                            #           POS 合规），都走 `daily` 一个入口加不同跳过开关；
                            #           设置里勾「自动化跑什么」，改勾选**不重新注册任务**；
                            #           两个都不勾是**错的**（不许静默当成"都跑"）
                            #        ⑦ 推送分两条（报量排查一条、POS 一条）；POS **不 @人**
                            #           （每天 @会被屏蔽，连带把报量排查也屏蔽掉）
                            #        ⑧ 口径改名（门店定的）：未报量 → **玲珑无但云商有**，
                            #           调拨货查无出库 → **玲珑有但云商无**。
                            #           「玲珑」= 华为那个销售系统的代号
                            #        ⑨ ⚠ **升级路径**：老门店的 run.bat 里写的是 `check`，
                            #           而 2.0.0 的 check 只从库读 —— 不做迁移就等于每天失败。
                            #           两道保障：run_check.py 把老参数改写成 `daily`；
                            #           界面概览页顺带按 RUNNER_MARK v5 重建 run.bat
                            # 1.6.1：抓会话撞图形验证码时**当场醒目提示**并收尾 ——
                            #        自动登录撞的：中止 + 关窗 + 重置 profile；
                            #        手动登录撞的：只提示、不打扰用户操作。
                            #        并记住"上次撞过"，下次不自动填账号密码
                            #        （否则重试会撞同一个验证码，死循环）。
                            # 1.6.0（主线：**兼容 Windows 7**）
                            #        ① 支持 Windows 7 / Python 3.8.10（代码降到 3.8 能跑，
                            #           三头 3.8.20 / 3.9.25 / 3.14 各自跑全套测试）
                            #        ② 安装时记住解释器（多 Python 机器不装错）
                            #        ③ 全程不再需要管理员权限（装机那次 UAC 也取消了）
                            #        ④ 定时任务：提权注册 + 自己记一份注册参数
                            #           （`.secrets/schedule.json`），提权建的任务
                            #           照样看得到时间和命令

ROOT = Path(__file__).resolve().parent.parent
BUILD_FILE = ROOT / "BUILD.txt"
UNPACKAGED = "源码运行（未打包）"


def _git_dir(root: Optional[Path] = None) -> Path:
    """`.git` 的位置。可能是目录（普通 clone），也可能是个文件（worktree）。"""
    base = ROOT if root is None else Path(root)
    p = base / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            line = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return p
        if line.lower().startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            q = Path(target)
            return q if q.is_absolute() else (base / q)
    return p


def _read_sha(git: Path, ref: str) -> str:
    """按 ref 名找 commit sha。refs 缺失时退到 packed-refs。"""
    try:
        sha = (git / ref).read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    if sha:
        return sha
    try:                                   # gc 过之后 ref 会挪进 packed-refs
        for line in (git / "packed-refs").read_text(
                encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    except OSError:
        pass
    return ""


def git_revision(root: Optional[Path] = None) -> Optional[Tuple[str, str]]:
    """从 `.git` 读出 `(分支, 短 sha)`。不是 git 仓库就返回 None。

    detached HEAD（sha 直接写在 HEAD 里）也能认，分支名留空。
    """
    git = _git_dir(root)
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head:
        return None

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        if not ref:                        # `ref: ` 后面是空的 —— 不成形，别当分支
            return None
        sha = _read_sha(git, ref)
        branch = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ""
    else:
        sha, branch = head, ""             # detached：HEAD 里就是 sha

    if not sha:
        return None
    return branch, sha[:7]


def git_stamp(root: Optional[Path] = None) -> str:
    """`git main@b0250d2`；读不出来就返回空串。

    `root` 给 `bootstrap.py` 用 —— 它得问"**这个**目录是不是 clone 来的"。
    """
    got = git_revision(root)
    if not got:
        return ""
    branch, short = got
    return f"git {branch}@{short}" if branch else f"git {short}"


def build_id() -> str:
    """构建指纹。优先级：BUILD.txt > .git > "源码运行（未打包）"。"""
    try:
        txt = BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        txt = ""
    return txt or git_stamp() or UNPACKAGED


def packaged() -> bool:
    """是不是"装出来的"。

    clone 下来的算装好了 —— 它有自己的版本线索（见 `build_id`），
    而 `BUILD.txt` 存在的意义就是区分"有线索"和"什么都没有"。
    """
    return build_id() != UNPACKAGED


#: `BUILD.txt` 里那个 beta 标记（打包脚本写的是 `beta1 · 2026-09-18 10:20`）。
BETA_MARK = "beta"


def _read_build() -> str:
    try:
        return BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_beta(build: Optional[str] = None) -> bool:
    """这台机器上跑的是不是 **beta 测试包**。

    ⚠ **只能靠 `BUILD.txt` 判，不能靠 `VERSION`** —— beta 包和正式包的
    **版本号是同号的**（beta 只是"这一版正在测"的标记，见 AGENTS.md 发版那节），
    从版本号上根本分不出来。

    `build=None` 时读本机的 `BUILD.txt`；传字符串就判那串。

    ⚠ **这条判据有两处在用，所以抽成一个函数**：
    * `dbmigrate.only_in_release` —— "beta 包不许动门店的库"（**安全门槛**）；
    * `selfupdate.has_update` —— "beta 包能升回同号的正式版"（2.1.1 加的）。
    各写一份的话，哪天有人改了其中一处的大小写处理，另一处会**静默失效**。
    """
    txt = _read_build() if build is None else build
    return BETA_MARK in str(txt or "").lower()


def describe() -> str:
    """一行说清楚：`CBG报量对账 v1.2.0 · 2026-09-15 13:42`"""
    return f"{APP_NAME} v{VERSION} · {build_id()}"
