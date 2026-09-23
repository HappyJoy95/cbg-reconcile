"""**唤醒时刻**（`When`）—— 「什么时候叫醒我」这件事的**唯一口径**。

用户 2026-09-20：

> 「要给个接口，**让各个模块设置什么时间点唤醒，以及唤醒做什么**。
>   然后定时器看到到点了就做这个。设置要可以设置**日期、时间，每周几，每个月几号**这种」

## 为什么是结构化的，不是 cron 字符串

界面上要**直接编辑**（下拉选频率、时间框、周几多选、几号数字框）——
cron 那种 `30 8 * * 1` 门店看不懂，而"存一个字符串、界面再解析回控件"
等于把同一件事写两遍（这个项目为"两份定义"栽过好几次）。

| kind | 什么时候醒 |
|---|---|
| `daily` | 每天 `time` |
| `weekly` | `weekdays` 里的每天 `time`；**空 = 工作日（一~五）** |
| `monthly` | 每月 `day` 号 `time`；`day=0` = **当月最后一天**；该月没有那一天就顺延到月末 |
| `once` | `date` 那天 `time`，跑过一次就再也不跑 |

## 这个文件是**纯函数**（零 IO、零依赖）

`slot()` / `next_after()` 只吃一个 `datetime`，所以整张时刻表可以**表驱动地测**
（跨月、跨年、月末、周末、夏令时不存在于国内，但**跨年**和**31 号的月份**必须测）。
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

#: 认识的频率。加一个就要同时想清楚：`slot` / `next_after` / `text` / 界面控件。
#:
#: ⚠ `hourly`（**每小时**）2026-09-20 加的，为的是健康模块那个「自动更新」——
#:   用户：「健康模块默认注册一个自动更新，**固定一个小时执行一次**」。
#:   它的"几点"不是时刻而是**每小时的第几分钟**（`minute`，见 `When.minute`）。
KINDS = ("hourly", "daily", "weekly", "monthly", "once")

#: 默认唤醒时间 —— 就是今天那条 Windows 计划任务的时间点（21:00）。
#: ⚠ 不是"随便挑的"：定时器上线当天的行为要跟之前**逐字一致**，门店零感知。
DEFAULT_TIME = "21:00"

#: 工作日（ISO：1=周一 … 7=周日）。`weekly` 的 `weekdays` 留空就是这个意思。
WORKDAYS: Tuple[int, ...] = (1, 2, 3, 4, 5)

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: `weekly` 选满七天就等于每天 —— 界面上说"每天"比列七个"周一、周二…"清楚。
_ALL_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)


def parse_time(text: str) -> Tuple[int, int]:
    """`"21:00"` → `(21, 0)`。认不出来**抛 ValueError**（不回落）。"""
    s = str(text or "").strip()
    if ":" not in s:
        raise ValueError("时间要写成 HH:MM，收到的是 %r" % (text,))
    hh_s, _, mm_s = s.partition(":")
    try:
        hh, mm = int(hh_s), int(mm_s)
    except ValueError:
        raise ValueError("时间要写成 HH:MM，收到的是 %r" % (text,))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("时间超出范围：%r（小时 0-23、分钟 0-59）" % (text,))
    return hh, mm


def _fmt_time(hh: int, mm: int) -> str:
    return "%02d:%02d" % (hh, mm)


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


#: 报错时要说清"该长什么样" —— 只说"不认识"等于让人去猜。
KIND_HINT = ("hourly（每小时）/ daily（每天）/ weekly（每周几）"
             " / monthly（每月几号）/ once（指定日期）")


@dataclass(frozen=True)
class When:
    """什么时候唤醒。**所有字段都有默认值** ⇒ `When()` = 每天 21:00。"""

    kind: str = "daily"
    time: str = DEFAULT_TIME
    #: 1=周一 … 7=周日。**只对 `weekly` 有意义**；留空 = 工作日。
    weekdays: Tuple[int, ...] = ()
    #: 每月几号（1..31）。**0 = 当月最后一天**。只对 `monthly` 有意义。
    day: int = 1
    #: `YYYY-MM-DD`。只对 `once` 有意义。
    date: str = ""
    #: **每小时的第几分钟**（0-59）。只对 `hourly` 有意义。
    #: ⚠ 独立字段、**不借用 `time`** —— 用 `time` 表达"每小时的第 17 分"要靠
    #:   "小时那半截不许看"，那种约定读代码的人一定会看错。
    minute: int = 0

    # ------------------------------------------------------------------ 校验
    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("不认识的频率 %r —— 只认 %s" % (self.kind, KIND_HINT))
        parse_time(self.time)                       # 不合法就抛
        for w in self.weekdays or ():
            if int(w) not in _ALL_WEEKDAYS:
                raise ValueError("周几只能是 1-7（1=周一），收到 %r" % (w,))
        if self.kind == "hourly" and not (0 <= int(self.minute) <= 59):
            raise ValueError("每小时的第几分钟只能是 0-59，收到 %r" % (self.minute,))
        if self.kind == "monthly" and not (0 <= int(self.day) <= 31):
            raise ValueError("每月几号只能是 0-31（0 = 最后一天），收到 %r" % (self.day,))
        if self.kind == "once":
            if not self.date:
                raise ValueError("「指定日期」必须给日期（YYYY-MM-DD）")
            try:
                datetime.date.fromisoformat(str(self.date))
            except ValueError:
                raise ValueError("日期要写成 YYYY-MM-DD，收到 %r" % (self.date,))

    # ------------------------------------------------------------------ 出口
    def normalized(self) -> "When":
        """把 `weekdays` 排好、去重（`weekly` 之外的一律清空，免得存进去一堆没用的字段）。"""
        if self.kind != "weekly":
            return replace(self, weekdays=())
        ws = tuple(sorted({int(w) for w in (self.weekdays or WORKDAYS)}))
        return replace(self, weekdays=ws)

    @property
    def hhmm(self) -> Tuple[int, int]:
        return parse_time(self.time)

    def _at(self, d: datetime.date) -> datetime.datetime:
        hh, mm = self.hhmm
        return datetime.datetime(d.year, d.month, d.day, hh, mm)

    def _monthly_day(self, year: int, month: int) -> int:
        """这个月该在哪天醒（`day=0` 或超出当月天数 ⇒ 月末）。"""
        last = _month_end(year, month)
        d = int(self.day)
        if d == 0:
            return last
        return min(d, last)

    # ------------------------------------------------------- 最近一次「该醒」
    def slot(self, now: datetime.datetime) -> Optional[datetime.datetime]:
        """**最近一次该醒的时刻**（`<= now`）；从来没到过就 `None`。

        ⚠ 只回答"上一次该醒是什么时候"，**不判断"还该不该跑"** ——
          那要连"这个 slot 跑过没 / 是不是过了容忍窗口"一起看，
          写在 `timer.due()` 里（这里保持纯函数，好测）。
        """
        if self.kind == "once":
            when = self._at(datetime.date.fromisoformat(self.date))
            return when if when <= now else None
        if self.kind == "hourly":
            # 本小时的那一次；还没到就退回上一个小时（跨天自动落到昨天 23:xx）
            cand = now.replace(minute=int(self.minute), second=0, microsecond=0)
            return cand if cand <= now else cand - datetime.timedelta(hours=1)
        if self.kind == "daily":
            today = self._at(now.date())
            return today if today <= now else today - datetime.timedelta(days=1)
        if self.kind == "weekly":
            best = None
            for back in range(0, 8):                       # 往前找 7 天够覆盖一周
                d = now.date() - datetime.timedelta(days=back)
                if d.isoweekday() not in self.normalized().weekdays:
                    continue
                cand = self._at(d)
                if cand <= now:
                    best = cand
                    break
            return best
        # monthly：本月那次；没到就上个月
        y, m = now.year, now.month
        cand = self._at(datetime.date(y, m, self._monthly_day(y, m)))
        if cand <= now:
            return cand
        py, pm = _shift_month(y, m, -1)
        return self._at(datetime.date(py, pm, self._monthly_day(py, pm)))

    # ---------------------------------------------------- 下一次「该醒」
    def next_after(self, now: datetime.datetime) -> Optional[datetime.datetime]:
        """下一次该醒的时刻（**严格晚于 `now`**）。`once` 跑过了就 `None`。"""
        if self.kind == "once":
            when = self._at(datetime.date.fromisoformat(self.date))
            return when if when > now else None
        if self.kind == "hourly":
            cand = now.replace(minute=int(self.minute), second=0, microsecond=0)
            return cand if cand > now else cand + datetime.timedelta(hours=1)
        if self.kind == "daily":
            today = self._at(now.date())
            return today if today > now else today + datetime.timedelta(days=1)
        if self.kind == "weekly":
            ws = self.normalized().weekdays
            for fwd in range(0, 8):
                d = now.date() + datetime.timedelta(days=fwd)
                if d.isoweekday() not in ws:
                    continue
                cand = self._at(d)
                if cand > now:
                    return cand
            return None                                    # 理论上到不了（一周总有）
        y, m = now.year, now.month
        cand = self._at(datetime.date(y, m, self._monthly_day(y, m)))
        if cand > now:
            return cand
        ny, nm = _shift_month(y, m, 1)
        return self._at(datetime.date(ny, nm, self._monthly_day(ny, nm)))

    # ------------------------------------------------------------------ 文字
    def text(self) -> str:
        """给人看的说法（界面和推送里都用这一份，别各写各的）。"""
        t = _fmt_time(*self.hhmm)
        if self.kind == "hourly":
            m = int(self.minute)
            return "每小时" if m == 0 else "每小时的第 %d 分钟" % m
        if self.kind == "daily":
            return "每天 %s" % t
        if self.kind == "weekly":
            ws = self.normalized().weekdays
            if not ws:
                return "每周 %s" % t
            if ws == _ALL_WEEKDAYS:
                return "每天 %s" % t
            if ws == WORKDAYS:
                return "工作日 %s" % t
            return "每%s %s" % ("、".join(WEEKDAY_NAMES[w - 1] for w in ws), t)
        if self.kind == "monthly":
            if int(self.day) == 0:
                return "每月最后一天 %s" % t
            return "每月 %d 号 %s" % (int(self.day), t)
        return "%s %s" % (self.date, t)

    def as_dict(self) -> dict:
        """存盘 / 走 HTTP 的形状。**只留这个 kind 用得上的字段**（免得界面看着一团）。"""
        n = self.normalized()
        # ⚠ `hourly` **不给 `time`** —— 它的"几点"是 `minute`（每小时的第几分钟），
        #   带上一个用不着的 `21:00` 只会让存盘/接口看着像"每天 21 点"。
        if n.kind == "hourly":
            return {"kind": "hourly", "minute": int(n.minute)}
        out = {"kind": n.kind, "time": _fmt_time(*n.hhmm)}
        if n.kind == "weekly":
            out["weekdays"] = list(n.weekdays)
        if n.kind == "monthly":
            out["day"] = int(n.day)
        if n.kind == "once":
            out["date"] = n.date
        return out

    @classmethod
    def from_dict(cls, data) -> "When":
        """从存盘 / HTTP 的形状还原。**认不出来就抛** —— 别悄悄回落到每天 21:00。"""
        if isinstance(data, When):
            return data
        if not isinstance(data, dict):
            raise ValueError("唤醒时刻要是一个对象，收到 %r" % (data,))
        ws = data.get("weekdays") or ()
        if isinstance(ws, str):
            ws = [x for x in ws.replace("，", ",").split(",") if x.strip()]
        return cls(kind=str(data.get("kind") or "daily"),
                   time=str(data.get("time") or DEFAULT_TIME),
                   weekdays=tuple(int(w) for w in ws),
                   day=int(data.get("day", 1) or 0),
                   date=str(data.get("date") or ""),
                   minute=int(data.get("minute", 0) or 0))

    @classmethod
    def parse(cls, text: str) -> "When":
        """一句话 → `When`（给命令行/测试用）：`每天 21:00` / `weekly 08:30 1,4` 这种。

        ⚠ 只认**英文 kind + 时间 + 可选参数**，中文那种（`每周一 08:30`）
          是 `text()` 的输出，不往回解析（两套写法互相解析迟早对不上）。
        """
        parts = str(text or "").split()
        if not parts:
            raise ValueError("空字符串不是唤醒时刻")
        kind = parts[0]
        rest = parts[1:]
        time = DEFAULT_TIME
        if rest and ":" in rest[0]:
            time = rest.pop(0)
        if kind == "weekly":
            ws = tuple(int(x) for x in rest[0].replace("，", ",").split(",")) if rest else ()
            return cls(kind="weekly", time=time, weekdays=ws)
        if kind == "hourly":
            # `hourly 17` = 每小时的第 17 分钟（不带就是整点）
            return cls(kind="hourly", minute=int(rest[0]) if rest else 0)
        if kind == "monthly":
            return cls(kind="monthly", time=time, day=int(rest[0]) if rest else 1)
        if kind == "once":
            if not rest:
                raise ValueError("once 要带日期：once YYYY-MM-DD [HH:MM]")
            date = rest[0]
            if len(rest) > 1 and ":" in rest[1]:
                time = rest[1]
            return cls(kind="once", time=time, date=date)
        return cls(kind=kind, time=time)


def describe(whens: Sequence[When]) -> str:
    """一组时刻 → 一句话（界面上"什么时候跑"那一格）。空 = 只手动。"""
    got: List[When] = [w if isinstance(w, When) else When.from_dict(w) for w in (whens or ())]
    if not got:
        return "只手动跑"
    return " / ".join(w.text() for w in got)
