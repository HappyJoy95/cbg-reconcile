"""业务功能的**安装注册机制** —— 一份声明，喂三处。

用户 2026-09-19：「还需要一个**业务功能的安装注册机制**。」

## 现在散成三处（这就是"只做接入"没达标的根源）

| 散在哪 | 位置 |
|---|---|
| 定时步骤表 | `run_daily.py`：`STEPS` / `STEP_LABELS` / `STEP_FLAGS` / `MANUAL_STEPS`（⚠ 界面预设那三张 `BUTTON_*` / `AUTOMATION_DEFAULT_STEPS` 2026-09-21 晚随"手动整批"一起删了）|
| 控制台菜单表 | `web/index.html` 的 `data-tab` / `data-subtab` + `web/app.js` 的 `SUBTABS` / `TAB_HOME` |
| ~~自动化勾选项~~ | ⚠ **2026-09-20 取消了**：声明了 `whens` 的步骤一律都跑 |

⇒ **本模块是那个唯一来源**：`run_daily` 的三张表从这儿**派生**（见它文件头），
菜单那边等前端接上（`tests/test_registry.py` 里先钉"注册表与 HTML 不漂移"）。
⚠ 派生口按版收窄（生活馆版 2026-09-26）：功能走 `build_all()`、
能力层步骤走 **`builtin_steps()`** —— 生活馆只留抓玲珑 + 自动更新，
且 `dump` 的 `whens` 剥成空（用户定：不建计划任务，手动刷）。

## 形状：父带"目录"，子带"内容"

```
Feature(key=…)        ← 一级：功能模块（销售数据 / 五项合规 / 门店）
  └ Sub(key=…)        ← 二级：子模块（POS 合规 / 报量查询 / 设置）
      └ Step(cmd=…)   ← 定时步骤（被计时模块唤醒时跑什么）
```

⚠ **父带目录、子带内容**（用户问过"注册需要带子模块一起吗"，见设计基线 §一·九·二）：
父声明"我有哪些子"（树是父的知识：顺序、可见性继承、孤儿检测），
子的**内容**（step / needs / notify / run）写在子自己的文件夹里。

## ⚠ 红线：这不是插件框架

| ✅ 可以 | ❌ 不可以 |
|---|---|
| 一处**显式**清单（`features/__init__.py` 的 `ALL`） | 扫 `features/*/` 目录自动发现 |
| 每个功能在自己 `__init__.py` 里声明 | `manifest.yaml` / `plugin.json` |
| 启动时**校验**（key / 菜单 / 步骤撞车 ⇒ 报错） | 运行时反射 `importlib.import_module` |

⇒ 加一个功能 = **新建文件夹 + `ALL` 加一行**，而且**可以被 `grep` 出来**。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, List, Optional, Tuple

#: 「什么时候唤醒」的值对象住在**计时模块**里（那是这件事的归属），
#: 功能模块只负责**声明**。⚠ 它是纯的（零 IO、不 import 本模块）⇒ 不成环。
from ..modules.timer.when import When, describe as _describe_whens


@dataclass
class Step:
    """定时步骤 —— 被**计时模块**唤醒时跑什么。

    `cmd` 同时是：`run_daily.STEPS` 里的名字、控制台「自动化跑什么」的勾选值、
    以及 `--skip-<cmd>` 的开关名来源。
    """

    cmd: str
    label: str
    order: int = 50
    default: bool = True          # 算不算"每天那趟"（`run_daily.MANUAL_STEPS`：
                                  #   到点会跑的那一套，也是门店双击手动跑的那一套）
    #: 执行入口（`app.<功能>.run`）。**不给就按 `cmd` 走 CLI 子命令**（今天的路子）——
    #: 各功能把编排搬进 `app/` 之后再把 `run=` 填上，那是逐步的事。
    run: Optional[Callable] = None
    flag: str = ""                # `--skip-x` 的开关名；不给就按 cmd 推
    #: ⭐ **什么时候叫醒这一步**（用户 2026-09-20：「让各个模块设置什么时间点唤醒，
    #:   以及唤醒做什么」）—— `cmd` 回答"做什么"，`whens` 回答"什么时候"。
    #:
    #: ⚠ **空元组 = 只能手动跑**（定时器不管它）。别写成"空 = 默认 21:00"：
    #:   "没声明"和"声明了每天 21:00"是两件事，用默认值去猜意图迟早出事。
    #: ⚠ 一个步骤可以有**多条**（比如每周一和每月 1 号各一次），
    #:   每条各自算自己的 slot。
    whens: Tuple[When, ...] = ()
    #: ⭐ **不许关掉**（定时器任务表里那个开关滑块锁死）——
    #:   用户 2026-09-20：「**自动更新和数据抓取模块不允许关闭**。数据抓取能改时间」。
    #:
    #: ⚠ 这几步关掉的后果都是"看不出来"的那种：
    #:   * 不抓数 ⇒ 本地库永远是旧的，后面的分析和推送都是拿旧数据在算，
    #:     而界面上只会说"跑完了"；
    #:   * 不自动更新 ⇒ 门店永远停在装机那一版（安全修复也进不去）。
    #: ⚠ "不许关"**只锁那个开关**：时间照样能改（用户明说了"数据抓取能改时间"）。
    #: ⚠ 命令行上的 `--skip-dump` / `--steps` 是**调试后门**，不受这里约束
    #:   （人在旁边看着才用，界面上没有入口）。
    required: bool = False


@dataclass
class Sub:
    """二级：子模块（菜单里的第二级）。"""

    key: str
    label: str
    order: int = 50
    types: str = ""               # 比父更窄的可见性（空 = 继承父）
    step: Optional[Step] = None   # 有步骤 ⇒ 会被 daily 唤醒；没有就是纯页面（如「设置」）


@dataclass
class Feature:
    """一级：功能模块（菜单里的一级）。"""

    key: str
    label: str
    order: int = 50
    types: str = ""               # "" = 三类门店都看；否则是 `experience platform` 这种
    children: List[Sub] = field(default_factory=list)
    home: str = ""                # 点一级标签落在哪个子面板（默认第一个子）

    def steps(self) -> List[Step]:
        return [s.step for s in self.children if s.step]


#: 默认唤醒时刻 —— **所有步骤默认都是它**（= 今天那条 Windows 计划任务的时间点）。
#: ⚠ 定时器上线当天，行为要跟之前**逐字一致**：还是那几步、还是 21:00，
#:   门店零感知；要改就到界面上改（`通用设置 › 定时执行`）。
DEFAULT_WHENS: Tuple[When, ...] = (When(kind="daily", time="21:00"),)

#: **能力层自带的步骤** —— 它们不属于任何"功能"（抓数不是某功能的一部分）。
#:
#: ⚠ 2026-09-20（用户：「数据抓取再加个**云商数据定时抓取**吧」）**从一步拆成两步**：
#:   * `dump`    = 华为/玲珑那边（池A 订单 + 池B 玲珑在库）；
#:   * `erp-dump` = **云商**那边（池D 云商在库 + 池C 云商销售）。
#:   拆的理由：它们**是两件独立的事**，各自要能单独设时间、单独开关 ——
#:   原来云商那三个池子是"顺手"在 `cmd_dump` 里拉的（注释里写着"顺带"），
#:   界面上根本看不出来，也就没法"只重抓云商"。
#:   ⚠ 顺序仍是**先玲珑后云商**（10 / 20）：同一个时间点跑的时候，
#:     后面的分析（pos 30 / pools 40 / attain 45）读的就是这两步刚写进去的数。
BUILTIN_STEPS: Tuple[Step, ...] = (
    Step(cmd="dump", label="抓取玲珑数据", order=10, default=True, flag="--skip-dump",
         required=True, whens=DEFAULT_WHENS),
    Step(cmd="erp-dump", label="抓取云商数据", order=20, default=True,
         required=True, flag="--skip-erp-dump", whens=DEFAULT_WHENS),
    # ⚠ **系统健康模块**的自动更新（用户 2026-09-20：「健康模块默认注册一个自动更新，
    #   **固定一个小时执行一次**」）。
    #   * `hourly` 频率：查一次"有没有新版本"很便宜（一个 GitHub API 请求），
    #     真**动手更新**还要过 `health.update_plan()` 那几道闸（见 `cli.cmd_autoupdate`）；
    #   * `minute=17` 而不是整点：错开一点，也免得跟 21:00 那趟对账撞在同一分钟；
    #   * `default=False` —— 它**不属于"每天那趟整批"**（`daily` 不跑它，见
    #     `run_daily` 里那条规矩），由定时器按小时单独叫醒。
    #     半夜对账跑到一半被换代码，是最不该发生的事。
    Step(cmd="autoupdate", label="自动更新", order=60, default=False, required=True,
         flag="--skip-autoupdate",
         whens=(When(kind="hourly", minute=17),)),
    # ⭐ **数据上报**（M18，2026-09-21）：门店把当天新增/变化的行打成 SQLite 附件，
    #   邮件发给本店区长（抄送中台）。
    #   ⚠ `whens=()` = **定时器不管它**，只跟着 `daily` 那趟末尾跑 ——
    #     正是用户定的「不注册定时器」（一个 `Step` 的既有语义，不是新机制）。
    #   ⚠ 它有 pages 吗？没有 —— 门店侧**不需要人去点**（四·八验收 7）。
    #   ⚠ 2026-09-21 晚上改的（用户：「**上报数据还是有自己的吧**」）：
    #     原来是 `whens=()`（跟着每天那趟跑），界面上那个开关永远开不了 ——
    #     用户看到的是"这功能坏了"。现在给它**自己的时间 21:15**。
    #   ⚠ 时间卡在两个约束中间，别随手改：
    #     ① **必须晚于 21:00 那趟**（它要发的是那趟刚抓/刚算完的数据）；
    #     ② **必须早于 21:30**（区长/平台那台机器 21:30 收信，晚了就变"明天才看到"）。
    #   ⚠ `default=False`：**不进整批**（否则 21:00 整批跑一遍、21:15 又叫醒一次 = 一天两遍）。
    Step(cmd="report", label="上报数据", order=90, default=False, flag="--skip-report",
         whens=(When(kind="daily", time="21:15"),)),
    # ⭐ **收取门店上报**（M19）：区长 / 平台那台机器收信落库。
    #   ⚠ 时间**21:30 而不是 21:00**：门店那趟 21:00 跑完才发信（21:00~21:10 到），
    #     同一时刻收信只会收个空 —— 那"区长今晚就看到"就变成"明天才看到"。
    #   ⚠ `default=False`（不跟着整批跑）：它已经在 21:30 单独叫醒，
    #     放整批里就是**一天跑两遍**，而第二遍什么都收不到（用户看到的是"跑了两趟"）。
    #     跟 `autoupdate` 同一个道理，界面上那条"不在整批里"的提示也认这个。
    #   ⚠ 门店机器上 IMAP 没配 ⇒ 这一步**安静跳过**（不是失败），所以放哪儿都安全。
    Step(cmd="report-inbox", label="收取门店上报", order=95, default=False,
         flag="--skip-report-inbox",
         whens=(When(kind="daily", time="21:30"),)),
)


def all_features() -> List[Feature]:
    """注册表的唯一入口 —— ⚠ 每次现算（`features.ALL` 是定格常量，
    生活馆测试要能切 env 看到不同结果，所以问 `build_all()`）。"""
    from . import build_all
    return build_all()


def step_by_cmd(cmd: str) -> Optional[Step]:
    """按 `cmd` 找那一步 —— 定时器和界面都按 cmd 认任务。"""
    for s in all_steps():
        if s.cmd == cmd:
            return s
    return None


#: 能力层自带那一步的"归属" —— 它不属于任何**功能**模块（抓数不是某个业务的一部分），
#: 但界面上照样要有个"哪个模块的"栏位（用户 2026-09-20：
#: 「每个模块的设置页面加上定时执行相关设置」⇒ 得知道每一步归谁）。
#: ⚠ 能力层每一步各归哪个**系统模块**（用户 2026-09-20：「健康模块默认注册一个
#: 自动更新…」）—— 加一条能力层步骤就要在这儿说清它归谁，否则界面上会指错模块。
BUILTIN_OWNER = {
    "dump": {"key": "fetch", "label": "数据抓取"},
    "erp-dump": {"key": "fetch", "label": "数据抓取"},
    "autoupdate": {"key": "health", "label": "系统健康"},
    # ⚠ 上报这两步**不属于任何功能模块**（它没有页面 —— 门店侧不需要人去点，
    #   四·八验收 7）⇒ 归它自己那个"数据上报"名下，界面上才知道该指谁。
    #   不登记的话 `step_owner` 会兜成"数据抓取"，那是**指错模块**。
    # ⚠ 名字跟界面上的叫法对齐（用户 2026-09-21：「一级标签改叫**数据交换**」）——
    #   定时器页面上"这一步归哪个模块"那一列显示的就是它。
    "report": {"key": "report", "label": "数据交换"},
    "report-inbox": {"key": "report", "label": "数据交换"},
}


def step_owner(cmd: str) -> dict:
    """这一步**归哪个模块**（一级功能模块）→ `{"key", "label"}`。

    ⚠ 从**注册表推**，不在界面上再写一份：前端写死一份的话，
      哪天某一步换了模块（或者加了一步），界面上就会指错地方，
      而那种错**只有肉眼能发现**。
    """
    for f in all_features():
        for s in f.children:
            if s.step and s.step.cmd == cmd:
                return {"key": f.key, "label": f.label}
    return dict(BUILTIN_OWNER.get(cmd, {"key": "fetch", "label": "数据抓取"}))


def wakes() -> List[Step]:
    """**会被定时器唤醒的步骤**（声明了 `whens` 的那些，按 `order` 排好）。

    ⚠ 没声明的**不在里面** —— "没声明"就是"只手动跑"，不是"默认 21:00"。
    """
    return [s for s in all_steps() if s.whens]


def builtin_steps() -> List[Step]:
    """能力层步骤（**按版收窄的口子**）—— 生活馆只留抓玲珑 + 自动更新，
    且 `dump` 的 whens 剥成 `()`（用户 2026-09-26 定：不建计划任务，手动刷）。

    ⚠ full 版原样返回 `BUILTIN_STEPS`（既有 2810 条测试钉的是原对象）；
      `BUILTIN_OWNER` 那张展示表保持全表 —— 键多出来无害，`step_owner` 查不到会兜底。
    """
    from .. import edition
    out = list(BUILTIN_STEPS)
    if edition.is_lifehall():
        drop = {"erp-dump", "report", "report-inbox"}
        out = [s for s in out if s.cmd in edition.LIFEHALL_BUILTIN_STEPS
               and s.cmd not in drop]
        replaced = []
        for s in out:
            if s.cmd == "dump":
                s = replace(s, whens=())     # 生活馆不建计划任务：手动刷
            replaced.append(s)
        out = replaced
    return out


def all_steps() -> List[Step]:
    """定时步骤（含能力层自带的），按 `order` 排好。"""
    out = builtin_steps()
    for f in all_features():
        out += f.steps()
    return sorted(out, key=lambda s: (s.order, s.cmd))


def steps() -> Tuple[str, ...]:
    """`run_daily.STEPS` 的来源。"""
    return tuple(s.cmd for s in all_steps())


def step_labels() -> dict:
    return {s.cmd: s.label for s in all_steps()}


def step_flags() -> dict:
    return {s.cmd: (s.flag or ("--skip-" + s.cmd)) for s in all_steps()}


def default_steps() -> Tuple[str, ...]:
    """`run_daily.MANUAL_STEPS` 的来源（"每天那趟"的那几步）。"""
    return tuple(s.cmd for s in all_steps() if s.default)


def menus() -> list:
    """菜单树（一级 + 二级），给控制台用 —— **照 `order` 排好**。"""
    out = []
    for f in sorted(all_features(), key=lambda x: (x.order, x.key)):
        subs = sorted(f.children, key=lambda s: (s.order, s.key))
        out.append({"key": f.key, "label": f.label, "order": f.order, "types": f.types,
                    "home": f.home or (subs[0].key if subs else ""),
                    "children": [{"key": s.key, "label": s.label, "order": s.order,
                                  "types": s.types or f.types, "is_step": bool(s.step)}
                                 for s in subs]})
    return out


def validate() -> list:
    """注册表自检 —— 返回**问题清单**（空 = 没问题）。

    ⚠ 放在**启动自检**里当硬门槛（设计基线 §一·十一）：撞车意味着
    "两个功能抢同一个菜单/步骤名"，那种事必须在启动时当场报出来，
    而不是等用户点到某个页面才发现。
    """
    bad = []
    seen_feature, seen_sub, seen_cmd = {}, {}, {}
    for f in all_features():
        if f.key in seen_feature:
            bad.append("功能 key 重复：%s" % f.key)
        seen_feature[f.key] = f
        if not f.label:
            bad.append("功能 %s 没有中文名" % f.key)
        for s in f.children:
            if s.key in seen_sub:
                bad.append("子模块 key 重复：%s（%s 与 %s）"
                           % (s.key, seen_sub[s.key], f.key))
            seen_sub[s.key] = f.key
            if not s.label:
                bad.append("子模块 %s 没有中文名" % s.key)
    for s in all_steps():
        if s.cmd in seen_cmd:
            bad.append("定时步骤重复：%s —— 两个功能抢同一个步骤名" % s.cmd)
        seen_cmd[s.cmd] = True
        if not s.label:
            bad.append("步骤 %s 没有中文名" % s.cmd)
        if s.run is not None and not callable(s.run):
            bad.append("步骤 %s 的 run= 不是可调用的" % s.cmd)
        # ⭐ 唤醒时刻（用户 2026-09-20 的接口）—— 声明错了要**在启动自检里当场报**，
        #   而不是等某一跳定时器悄悄不跑（那种"没反应"最难查）。
        for w in s.whens or ():
            if not isinstance(w, When):
                bad.append("步骤 %s 的 whens 里有个不是 When：%r" % (s.cmd, w))
        if s.whens and not s.cmd:
            bad.append("步骤 %s 声明了 whens（什么时候唤醒），但没有 cmd —— "
                       "定时器到点不知道怎么跑它" % (s.label or "?"))
    # ⚠ `run` 允许为空：空 = "按 cmd 走 CLI 子命令"（今天的路子）。
    #   等各功能的编排搬进 `app/` 之后再逐个填上 —— 那是"只做接入"的下一步，不是现在。
    return bad
