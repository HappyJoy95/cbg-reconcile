"""本店人员名单（组织架构 + 谁在职）—— **执行模块**，见开发目标 §4.5.4。

原来是 `web.staff_state(app)`：业务逻辑长在 HTTP 层里，`startup.py`（启动刷新）
为了复用它只好 `from . import web` —— 一条**反向依赖**（阶段 2 的 2.3）。
现在双方都调这里：`/api/staff` 和启动刷新都只是薄薄的调用方。

⚠ 参数是**显式的**（`root` / `config_path` / `env_file`），不收 `app` 对象 ——
   收 `app` 就等于把 HTTP 层的形状漏进业务里，"以后换个入口"又要改这一层。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ... import config_io
from ... import edition as _edition
from ...paths import ROOT
if not _edition.is_lifehall():
    # 生活馆包里 `erp.py` 不存在（edition.PRUNE）—— 人员名单整个来自云商，
    # 所以下面会碰 ErpClient 的函数在生活馆直接走"没有人员设置"分支。
    from ... import erp
    from ...erp import DEFAULT_ENV_FILE, ErpClient, load_credentials

#: 门店手动打过的勾（**存"被剔除的"**，不是存"在职的"）
STAFF_REL = ".secrets/staff.json"


def staff_file(root=None) -> Path:
    return Path(root or ROOT) / STAFF_REL


def load_excluded(root=None) -> set:
    """门店剔掉的那批登录名（大小写不敏感）。文件坏了就当没剔过。"""
    try:
        ex = json.loads(staff_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x).strip() for x in (ex.get("excluded") or []) if str(x).strip()}


def save_excluded(root, accounts) -> None:
    """存的是**被剔除的**（不在职）那一批。

    ⚠ 不能存"在职" —— 以后新入职的人会默认变成"不在职"，那是最坏的一种默认。
    """
    f = staff_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"excluded": sorted({str(x).strip() for x in accounts})},
                            ensure_ascii=False, indent=1), encoding="utf-8")


def staff_state(root=None, config_path=None, env_file=None) -> dict:
    """本店员工 + 门店手动打过的勾。

    用户 2026-09-18：「通用设置里面加一个人员设置，登录到门店后自动加载挂在门店下
    所有在职员工，当然也加上在职的复选框，门店可以选择剔除掉**已离职但是状态没更新**
    的员工」。

    ⚠ **`Status` 区分不出离职**（实测 239 个账号全是 3）—— 所以那个复选框不是
      "锦上添花"，它是**唯一**的手段。默认全勾上（在职），门店自己去掉走掉的人。

    ⚠ 机构 Id 优先用配置里的 `erp_branch_id`（门店登录云商时写进去的）；
      没有就按门店名去组织架构树里查一次 —— 老机器没登过门店账号，靠这条兜底。
    """
    root = Path(root or ROOT)
    v = config_io.pick(config_io.load_raw(config_path)) if config_path else {}
    name = (v.get("erp_store_name") or "").strip()
    branch_id = v.get("erp_branch_id")

    excluded = load_excluded(root)

    if _edition.is_lifehall():
        # 生活馆版没有云商（人是从云商组织架构读的）—— 形状照 ok=False 分支抄，
        # 外加 count/active_count（有消费方直接读这两个数）。
        return {"ok": False, "error": "生活馆版没有人员设置（人员来自云商）",
                "people": [], "branch_id": None, "excluded": sorted(excluded),
                "count": 0, "active_count": 0}

    if not name and branch_id is None:
        return {"ok": False, "error": "还没认出这家店 —— 先在「门店」里登录一次云商",
                "people": [], "branch_id": None, "excluded": sorted(excluded)}

    env = env_file or (v.get("erp") or {}).get("env_file") or DEFAULT_ENV_FILE
    try:
        client = ErpClient(load_credentials(env), env_file=env, timeout=60)
        # 机构 Id 没有就查一次（顺带写回配置，下次不用再查）
        if branch_id is None:
            tree = client.call(erp.BRANCH_TREE_URL,
                               {"token": client.creds.get("token", "")}).get("Data")
            for node in erp._flatten_tree(tree):
                if (node.get("IsBranch") and str(node.get("Name") or "").strip() == name):
                    branch_id = node.get("Id")
                    break
            if branch_id is not None and config_path:
                config_io.update(config_path, {"erp_branch_id": branch_id})
        people = client.users(branch_id)
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": "读云商用户名单失败：%s: %s" % (type(e).__name__, e),
                "people": [], "branch_id": branch_id, "excluded": sorted(excluded)}

    low = {x.upper() for x in excluded}
    rows = []
    for u in people:
        acct = str(u.get("UserName") or "").strip()
        rows.append({
            # ⚠ 登录名大小写**不统一**（`SL…`/`sL…`/`sl…` 混着）——
            #   当 key 一律转大写，不然同一个人会被算成两个。
            "account": acct,
            "real": str(u.get("Real") or "").strip(),
            "phone": str(u.get("Phone") or "").strip(),
            # 默认**在职**（勾上）；门店去掉的就是"已离职但状态没更新"的
            "active": acct.upper() not in low,
        })
    rows.sort(key=lambda r: (not r["active"], r["real"]))
    return {"ok": True, "branch_id": branch_id, "erp_name": name,
            "people": rows, "excluded": sorted(excluded),
            "count": len(rows), "active_count": sum(1 for r in rows if r["active"])}


#: 门店 → 在职人员名单的缓存（展开门店时用，别每次都走网络）
ROSTER_REL = ".secrets/staff-roster.json"
#: 缓存多久算旧（秒）。**当天有效**够用 —— 人员调动不是分钟级的事。
ROSTER_TTL = 12 * 3600


def rosters_by_store(root=None, config_path=None, env_file=None, *,
                     max_age: int = ROSTER_TTL, force: bool = False) -> dict:
    """**每家门店的在职人员名单** → `{"门店名": ["王俊燕", ...]}`。

    ⚠ 干什么用：达成页点开门店名，下面要列**这家店在职的全体员工** ——
      用户 2026-09-21：「点开门店名称时下面的人员名单应该是**门店在职全部的**，
      不是谁有数据才显示谁」。
      原来那份名单是"这周卖过东西的人"推出来的 ⇒ 没开单的人**根本分不出来目标**。

    ⚠ 两次调用就能拿到全区：① 组织架构树（门店名 → `BranchId`）
      ② 用户名单（240 个，每条带 `BranchId`）—— 按 `BranchId` 归堆即可，
      **不用一家一家店去查**（那会是 40 次网络来回）。
    ⚠ 缓存写 `.secrets/staff-roster.json`（12 小时）：展开门店是随手动作，
      每次都走网络的话，云商一限流整页就没名单了。读不到网络就**用旧缓存**。
    ⚠ 剔除表（`load_excluded`）是**本店**的（「人员设置」里去掉的离职账号）——
      只对本店生效，别的店没这份信息。
    """
    if _edition.is_lifehall():
        # 同 staff_state：云商不在包里。这个函数只喂达成页（生活馆裁掉了），
        # 但守一道 —— 免得哪天多出个调用方，`from ... import erp` 当场炸。
        return {}
    from ... import config_io, erp
    from ...paths import ROOT
    root = Path(root or ROOT)
    cache_path = root / ROSTER_REL
    cached = {}
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        cached = {}
    age = time.time() - float(cached.get("at") or 0)
    if not force and cached.get("stores") and age < max_age:
        return cached["stores"]

    v = config_io.pick(config_io.load_raw(config_path)) if config_path else {}
    env = env_file or (v.get("erp") or {}).get("env_file") or DEFAULT_ENV_FILE
    try:
        client = ErpClient(load_credentials(env), env_file=env, timeout=60)
        tree = client.call(erp.BRANCH_TREE_URL,
                           {"token": client.creds.get("token", "")}).get("Data")
        by_id = {}
        for node in erp.branch_nodes(tree):
            bid, name = node.get("Id"), str(node.get("Name") or "").strip()
            if bid is not None and name:
                by_id[bid] = name
        out = {name: [] for name in by_id.values()}
        for u in client.users():
            store = by_id.get(u.get("BranchId"))
            real = str(u.get("Real") or "").strip()
            if store and real:
                out[store].append(real)
        for name in out:
            # 同一个人可能在名单里出现两次（账号不统一），去重 + 按拼音/字面排
            out[name] = sorted(set(out[name]))
    except Exception as e:                                     # noqa: BLE001
        if cached.get("stores"):
            return cached["stores"]                              # 读不到就用旧的
        raise RuntimeError("读云商人员名单失败：%s: %s" % (type(e).__name__, e))

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"at": time.time(), "stores": out},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass                                                     # 缓存写不上不影响这次
    return out


def active_accounts(root=None, config_path=None, env_file=None) -> list:
    """在职那批的登录名 —— 门禁/别的模块要的是这个（不是整份字典）。"""
    st = staff_state(root, config_path, env_file)
    return [r["account"] for r in (st.get("people") or []) if r["active"]] if st.get("ok") else []


# --------------------------------------------------------------- 保存后自动上报
#
# 用户 2026-09-21（连着几句定的）：
#   「门店勾选完各店人员状态（门店发来的）后点击保存，然后就调用发送邮件，这个设置了吗」
#   → 没设置 → 问收件人/时机 → 「**两者**。**保存即发**。连点几次保存**合并成一封**。
#     **30s** 的窗口够了。**完整上报包还是带着人员名单**」
#   → 窗口改成「**那还是 5 分钟吧**」
#   → 又问「关浏览器不影响吧…这个 300s 是怎么实现的？通过 timer 做个五分钟之后的任务？」
#   → 最后：「把 timer 的注册加上这种**单次注册**机制吧，**记录日志**，**执行完删除注册**」
#
# ⇒ 现在的实现：**登记一条一次性任务**（`timer.register_once`），
#   由定时器心跳到点派发 `daily --steps report` —— 那条链发的是**完整上报包**，
#   人员表照旧在里面。
#
# ⚠⚠ **别再回到 `threading.Timer`**：第一版就是那么写的（进程内一枚计时器），
#   它有三个毛病，正好都是这次换掉的原因：
#     ① **活在内存里** —— 服务一重启，那 5 分钟就白等了；
#     ② 跟定时器是**两套"什么时候做什么"**（`due()` 一套、这枚计时器一套）；
#     ③ 派发不走 `daily --steps` ⇒ 运行日志/抽屉里看不到那一趟。
#   换成 timer 的登记之后，这三条都没了：落盘、同一条 `tick()`、同一条执行路径。
#
# ⚠ 「连点几次只发一封」现在是**登记的 key** 保证的（同一个 key 再登记 = 改时间），
#   不是靠撤计时器（见 `timer.once.register` 的 docstring）。

#: 合并窗口（秒）。⚠ 用户先定 30 秒、当天又改成 **5 分钟**（「那还是 5 分钟吧」）——
#: 别自己改回去：他勾的是一排人，窗口短了会发好几封。
REPORT_MERGE_SECONDS = 5 * 60

#: 登记在哪一步上（`report` = 上报数据那一步，注册表里的 cmd）。
REPORT_STEP = "report"

#: 这条登记的 key —— **同一个 key 再登记就是改时间**（连点合并靠它）。
#: ⚠ 用固定 key 而不是默认的 `cmd`：万一以后 `report` 上还有别的一次性登记，别互相顶掉。
REPORT_ONCE_KEY = "staff-report"


def report_in(root=None) -> int:
    """还有几秒要把上报发出去（0 = 没登记 / 已到点）—— 界面拿它写"5 分钟后自动上报"。"""
    from ...modules import timer
    return timer.once_seconds_left(root, REPORT_ONCE_KEY)


def cancel_report(root=None) -> bool:
    """撤掉在等的那条登记（"我不想发了" / 测试用）。返回撤没撤。"""
    from ...modules import timer
    return timer.cancel_once(root, REPORT_ONCE_KEY)


def schedule_report(root=None, *, delay=None, note="", who="") -> dict:
    """保存完人员设置 → **过 `delay` 秒**（默认 5 分钟）跑一趟上报。

    返回 `{scheduled, in, at, why}`；⚠ **不抛**（登记不上不该让"保存"变红 ——
    保存本身早就落盘了，见 `save_excluded`）。

    ⚠ **为什么是"等一下再跑"而不是"立刻跑、窗口内不重复"**：门店改的是一串人，
      第一次点保存时他往往**还没改完** —— 立刻发出去的那封是**旧状态**，
      后面再点几次也追不回来。等最后一跳过去**一个窗口**（默认 5 分钟），
      那一趟 `report.build()` 现读人员表 ⇒ 一封里就是**最终状态**。
    ⚠ 窗口内再点保存 = **把那条登记的时间往后挪**（同一个 key），不是排第二条。
    ⚠ ⚠ **粒度**：定时器心跳是 30 秒一跳 ⇒ 实际是"5 分钟 ~ 5 分 30 秒"之间跑。
      对"保存后上报"这种事无所谓（用户认了），但**别拿它当精确闹钟**。
    ⚠ 旁边有任务在跑时，`tick()` 会等下一跳再取这条登记 —— 登记**还在**（落盘），
      不会因为"撞上别的活"就丢。
    """
    from ...modules import timer
    secs = REPORT_MERGE_SECONDS if delay is None else float(delay)
    res = timer.register_once(root, REPORT_STEP, after=secs, key=REPORT_ONCE_KEY,
                              note=note or "保存人员设置", who=who)
    return {"scheduled": bool(res.get("ok")), "in": int(res.get("in") or 0),
            "at": res.get("at") or "", "why": res.get("why") or ""}
