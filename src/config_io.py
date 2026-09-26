"""配置文件读写。

写回时**逐行做定点替换，保留原注释** —— 配置文件里的注释就是操作手册
（比如「顺和汇在数据里是小写 s」），被 yaml.dump 冲掉就亏大了。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# 前端允许改的字段（白名单）。其余一律不碰。
EDITABLE = (
    # `erp_branch_id` = 这家店在**云商组织架构**里的机构 Id（跟华为编码不是一回事）——
    # 「人员设置」要靠它筛出本店员工（`UserList?BranchId=`）。
    # 门店登录云商时顺手写进来；老机器没有就按门店名去组织架构里查一次。
    "erp_branch_id",
    # `platform: true` = 这台机器登录的是**云商平台岗账号**（看得到全部门店）。
    # ⚠ 它跟"本店"是**互斥**的两种身份：平台岗不绑某一家店，
    #   菜单全开、也不要求玲珑会话（它本来就不属于任何一家店）。
    "platform",
    "store_code", "marker", "erp_store_name", "timezone",
    # ⚠ 只留 `cmd_check` **真的还在读**的两个（`cli.py:383/385`）。
    #   `page_size` / `pay_status` / `return_status` 2026-09-17 拿掉了 ——
    #   它们是**死配置**：`cli.py`/`run_check.py`/`bootstrap.py` 一处都没把它们
    #   传进接口，`dump.py` 用的是自己的 `--page-size`（默认 200），
    #   而且明确**不许**传 `payStatus`/`returnStatus`（传了会把已退原单整张滤掉）。
    "check.lookback_days", "check.report_lookahead_days",
    # 邮件推送（路径列表在 .secrets/push-paths.json；下面是**老单配置**回落键）
    # ⚠ 2026-09-22：when / enabled / mention_all / send_file 界面不再改，
    #   但字段留在 EDITABLE —— 老配置迁移前还能读；别急着删以免白名单测试抖。
    "mail.enabled", "mail.host", "mail.port", "mail.security", "mail.sender",
    "mail.recipients", "mail.subject_prefix", "mail.env_file",
    # 企微推送（webhook / 路径列表在 .secrets/）
    "wecom.enabled", "wecom.mention_all", "wecom.send_file",
    "wecom.env_file",
)


def load_raw(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


#: 门店映射表 —— **云商门店名 → 串号标识 → 华为门店编码**。
#: 加新店要改的就是它（`selfupdate.ALLOW_EVEN_IF_NEVER` 里唯一放行的那份配置）。
STORES_REL = "config/stores.yaml"


def stores_table(root) -> list:
    """读 `config/stores.yaml` 的 `stores:` 列表。

    读不到/格式不对就给**空表** —— 调用方要能区分"表里没有这家店"和"表根本没读到"，
    所以这里不抛异常，由调用方按空表处理并说清。
    """
    doc = load_raw(Path(root) / STORES_REL) or {}
    return [s for s in (doc.get("stores") or []) if isinstance(s, dict)]


def managers_table(root) -> list:
    """读 `config/managers.yaml` 的 `managers:` 列表（**区长名单**）。

    读不到/格式不对给**空表** —— 调用方要能区分"这家店没配区长"和"名单没读到"，
    所以这里不抛异常（跟 `stores_table` 一个套路）。
    """
    doc = load_raw(Path(root) / "config" / "managers.yaml") or {}
    return [m for m in (doc.get("managers") or []) if isinstance(m, dict)]


#: `stores.yaml` 里标区域的字段名
REGION_KEY = "region"


def _name_list(v) -> list:
    """`"西北区"` / `["西北区", "南区"]` / `None` → `["西北区", …]`（去掉空串）。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x or "").strip()]


def regions_of(rows) -> list:
    """名单里出现过的区域名（**按名单顺序**去重）。"""
    out = []
    for r in rows or ():
        reg = str(r.get(REGION_KEY) or "").strip()
        if reg and reg not in out:
            out.append(reg)
    return out


def _is_real_store(row) -> bool:
    """这一条**是不是一家真门店**（体检的时候用）。

    名单里有两类**不是门店**的条目，它们不该被"没标区域"点名：

    * **平台岗** —— 虚拟门店（`kind: 平台岗`），平台账号登录时写的就是它；
    * **没有 `erp_name` 的**（比如颐高合作店）—— 没云商门店名就进不了任何区长的范围，
      标了区域也没用。
    """
    if not str(row.get("erp_name") or "").strip():
        return False
    return str(row.get("kind") or "").strip() != "平台岗"


def stores_of_regions(rows, regions) -> list:
    """这些区域底下有哪些店（返回**门店行**，两种写法调用方自己取）。

    ⚠ 用户 2026-09-21 定的 **B 方案**：「平台看全部、区长是分开的」—— 分开靠的是
      **区域**，而区域是**门店自己的属性**（`stores.yaml` 的 `region` 列），
      不是区长手里那份手抄的门店清单。这样**新店只要标了区域就自动进对应区长**，
      不会再出现"加了店、忘了加进区长的名单 ⇒ 那家店对谁都不可见"。
    """
    want = set(_name_list(regions))
    if not want:
        return []
    return [r for r in (rows or ())
            if str(r.get(REGION_KEY) or "").strip() in want]


def stores_of_manager(m, rows) -> list:
    """**这个区长管哪些店 —— 全项目唯一的口径**（`role_scope` 和 `managers_of` 都走它）。

    两种写法都认，**取并集**：

    | 写法 | 长什么样 | 说明 |
    |---|---|---|
    | 老（显式列店） | `stores: [青岛城阳万象汇店, …]` | 一个一个字写的，**照旧能用** |
    | 新（按区域） | `regions: [西北区]` | 照 `stores.yaml` 的 `region` 列自动圈 |

    ⚠ `region:`（**单数、字符串**）是**界面上那个标签**（"区长 杨英梅（西北区）"）。
      它**只在 `stores:` 和 `regions:` 都没写时**才当选择器用 —— 老文件里
      `region:` 和 `stores:` 是并存的，把它当选择器会让老文件的范围**悄悄变**。

    ⚠ 区域名打错 ⇒ 圈到 0 家店 ⇒ 那个区长**什么都看不到**。这不是"少看几页"，
      是整片空白，所以 `region_audit()` 专门把它挑出来（自检里会打印）。
    """
    explicit = _name_list(m.get("stores"))
    regions = _name_list(m.get("regions"))
    if not explicit and not regions:
        regions = _name_list(m.get("region"))          # 只写了一个区名 —— 就按它圈
    out = list(explicit)
    for r in stores_of_regions(rows, regions):
        for k in ("erp_name", "tdoc_name"):
            nm = str(r.get(k) or "").strip()
            if nm and nm not in out:
                out.append(nm)
    return out


def region_audit(root) -> list:
    """**区长分区体检** —— 返回一串给人看的问题（没问题就是空表）。

    盯三件会让数据**静默看不见**的事：

    1. 区长写了**名单里没有的区域名**（打错一个字 ⇒ 他一家店都看不到）；
    2. 名单里有店**没标区域**（那家店对**所有**区长都不可见，只有平台看得见）；
    3. 一家店被**两个区长**圈到（两个人都看得到它 —— 越权，而且没人会报）。

    ⚠ 只读、不抛：`selftest` 和看板都能直接调（跟 `stores_table` 一个规矩）。
    """
    out = []
    try:
        rows = stores_table(root)
        managers = managers_table(root)
    except Exception as e:                                     # noqa: BLE001
        return ["门店名单 / 区长名单读不出来：%s" % e]
    if not rows or not managers:
        return out                       # 没配名单就没什么可体检的（门店机器上很正常）
    all_regions = set(regions_of(rows))
    for m in managers:
        name = str(m.get("name") or "?")
        regions = _name_list(m.get("regions"))
        if not regions and not _name_list(m.get("stores")):
            regions = _name_list(m.get("region"))
        for reg in regions:
            if reg not in all_regions:
                out.append("区长「%s」写了区域「%s」，但门店名单里没有这个区"
                           "（⇒ 他一家店都看不到）" % (name, reg))
    unmarked = [str(r.get("erp_name") or r.get("name") or "?")
                for r in rows if _is_real_store(r)
                and not str(r.get(REGION_KEY) or "").strip()]
    if unmarked:
        out.append("门店名单里有 %d 家**没标区域**（对区长不可见，只有平台看得见）：%s"
                   % (len(unmarked), "、".join(unmarked)))
    seen = {}
    for m in managers:
        for nm in stores_of_manager(m, rows):
            key = store_key(rows, nm)
            if key:
                seen.setdefault(key, []).append(str(m.get("name") or "?"))
    for store, names in sorted(seen.items()):
        if len(set(names)) > 1:
            out.append("「%s」被 %d 位区长同时圈到（%s）—— 他们会互相看到对方的店"
                       % (store, len(set(names)), "、".join(sorted(set(names)))))
    return out


def store_key(rows, name) -> str:
    """门店名 → **主键**（`erp_name`）—— **两种写法都认**（`tdoc_name` 也算同一家）。

    ⚠ 为什么必须有它：一家店在本项目里有**两种写法** —— 云商里是「青岛鲁疆广场店」、
      腾讯文档里是「鲁疆广场」（`stores.yaml` 的 `tdoc_name`）。纯按 `erp_name` 比，
      同一条会被当成**两家店** ⇒ "28 家不重不漏"变成 29、"被两位区长同时圈到"
      还会为同一家店报两条（假警报）。
    ⚠ 认不出来就**原样返回**（宁可多留一个名字，也别悄悄吞掉一家店）。
    """
    n = str(name or "").strip()
    for r in rows or ():
        pair = {str(r.get("erp_name") or "").strip(), str(r.get("tdoc_name") or "").strip()}
        if n and n in pair:
            return str(r.get("erp_name") or n).strip()
    return n


def find_store_in(rows, erp_name):
    """在**已经读出来的**名单里找那一家（`find_store()` 的内核）。
    ⚠ 单独拆出来是为了**一次请求只读一遍 `stores.yaml`**：
      `role_scope()` 原来按门店数一次次读（一家店 3~4 遍，实测 25ms/请求），
      而这份表 30 家、每读一遍 ~5ms。规矩不变 —— 见 `find_store()`。
    """
    want = (erp_name or "").strip()
    if not want:
        return None
    for s in rows or ():
        if str(s.get("erp_name") or "").strip() == want:
            return s
    return None


def find_store(erp_name, root):
    """按**云商门店名**在映射表里找那一家，找不到返回 None。

    ⚠ 比对只去掉首尾空白（门店名是手填进 yaml 的，前后多个空格很常见），
    **绝不做模糊/包含匹配** —— 「青岛城阳万达店」和「联想城阳万达店」是两家店，
    模糊一下就会把配置填成隔壁那家，而界面上**看不出来**（门店名看着都对）。
    找不到就老老实实返回 None，让界面提示手填。

    ⚠ 手里**已经有名单**（比如 `role_scope`）时用 `find_store_in()`，
      别为了找一家店把整张表再读一遍。
    """
    return find_store_in(stores_table(root), erp_name)


#: **三种门店身份** = `store_profile()` 里 `type` 的三个取值 ——
#: 账号 / 门店 / 内容 的划分就靠它。
#:
#: 用户 2026-09-19：「体验店也不是全开，体验店是开**体验店对应的**，
#: 合作店是开**合作店对应的**……你需要把这个**整理到一起**，
#: 每个一级标签和二级标签内容上都加上这个标记。为了以后的开发方便」。
#:
#: ⚠ 2026-09-21（M17）改过：**"谁能看见哪一页"的表搬到了 `src/web.py` 的
#:   `PAGE_RULES`**（随 `/api/overview.role.pages` 下发），HTML 上不再标
#:   `data-types`，这里的 `type_sees()` 也删了（它俩是同一份东西的两半，
#:   而"页面藏了、接口照样给"就是两半走散的结果）。
#:   ⇒ 加新身份/新页面：改 `web.py::PAGE_RULES`，**改这一处就够**。
#:   `type` 这个字段本身**还在**（画像里还在发、还有别处读），只是不再管可见性。
STORE_TYPES = ("experience", "partner", "platform")

#: **虚拟平台岗门店**在名单里的样子。
#:
#: 用户 2026-09-19：「平台岗就做个虚拟的平台岗门店就是了」——
#: 所以平台岗不再是一个独立的配置标志（`platform: true`），
#: 而是名单里**一行普通的门店**（`kind: 平台岗`、`huawei_code` 空）。
#: ⇒ 它跟别的店走同一套"登录 → 写身份"的路。
PLATFORM_KIND = "平台岗"

#: 那家虚拟门店在配置里写的**店名** —— 必须和 `config/stores.yaml` 里那条一致
#: （有一条测试钉着，别改一处漏一处）。
PLATFORM_STORE = "平台岗"


def store_profile(values: dict, root) -> dict:
    """**这台机器是哪家店、要不要玲珑** —— 全项目问这一处。

    用户 2026-09-18：「云商登录成功之后看是哪个店，**如果是我们串号标识里有的
    那十四家店需要登录玲珑，其余店不需要登录玲珑就可以用**。但是他们的控制台
    也不会显示玲珑相关的内容。这就是做的账号、门店权限与内容的划分」。

    ⚠ **判据是"名单里这家店有没有串号标识"**，不是 `kind`：
    名单里 15 家 `kind=体验店`，但只有 **14 家**有标识（「青岛海信广场店」没有）——
    用户说的正是"标识里有的那十四家"。等它拿到标识，这个判据会自动跟上。

    ⚠ 为什么这份判断必须集中在一处：门禁（要不要拦着登录玲珑）、
    菜单（显不显示「玲珑授权」/「报量查询」/「五项合规」）、每天跑几步
    （`dump` 抓的就是玲珑数据）三处都要问同一个问题 ——
    各写一份的话，迟早出现"菜单里藏了但任务还在跑"这种自相矛盾。
    """
    # 生活馆（2026-09-26）：名单里可能根本没这家店（认店只拿到店码）—— 但它
    # **一定走玲珑**（待领清单的数据源就是玲珑销售单）。不置 True 的话 run_daily
    # 会把 dump 按掉，待领清单从此没数，且日志只说"这家店不走玲珑"，极难查。
    # ⇒ 下面三个出口的 `needs_linglong` 统一走这一档（full 版行为逐字不变）。
    # 函数内 import：本文件是纯函数库，避免在文件头引入对 edition 的模块级依赖。
    from . import edition as _edition
    # ⚠ 平台岗：**不绑某一家店**。菜单全开、但不要求玲珑会话 ——
    #   玲珑会话是按门店编码存的，平台岗本来就不属于任何一家店。
    if str(values.get("platform") or "").strip().lower() in ("1", "true", "yes"):
        # ⚠ **老标志**（2026-09-19 之前的机器上就是它）。留着读是为了兼容：
        #   那些机器的配置里没有店名，只写了 `platform: true`。
        #   店名照样填**虚拟门店**那个名字 —— 不然界面上（左下角）会显示成
        #   「（未配置门店）」，看着像没配好。重新登录一次就会换成新的写法。
        return {"erp_name": PLATFORM_STORE, "huawei_code": "", "marker": "",
                "kind": PLATFORM_KIND,
                "huawei_name": "", "in_roster": False, "platform": True,
                "needs_linglong": _edition.is_lifehall(),
                "show_all": True, "type": "platform"}

    name = (values.get("erp_store_name") or "").strip()
    hit = find_store(name, root) if name else None

    # ⚠ **虚拟平台岗门店**（名单里 `kind: 平台岗`）。
    #   它跟别的店**同一套机制**：登录 → 把店名写进配置 → 画像照名单认。
    #   跟上面那条老标志的区别是：老标志不写店名，于是"平台岗"和"某家店"
    #   两种身份能同时留在配置里（见 `PLATFORM_KIND` 的注释）。
    if (hit or {}).get("kind") == PLATFORM_KIND:
        return {"erp_name": name,
                "huawei_code": (values.get("store_code") or "").strip(),
                "marker": "", "kind": PLATFORM_KIND,
                "huawei_name": (hit or {}).get("huawei_name") or "",
                "in_roster": True, "platform": True,
                "needs_linglong": _edition.is_lifehall(),
                "show_all": True, "type": "platform"}
    # ⚠ **名单优先**，配置里的只是"名单里没这家店"时的兜底。
    #
    #   反过来写（配置优先）会让**旧值一直赢**：2026-09-18 实测踩到 ——
    #   从新业广场店（标识 Y）换登成麦凯乐店（合作店、名单里没标识），
    #   配置里那个 Y 没清掉，于是"一家合作店要玲珑"，永远进不去控制台。
    #   名单是**随程序更新的唯一真源**，它说了算。
    roster_marker = ((hit or {}).get("marker") or "").strip()
    marker = roster_marker if hit else (values.get("marker") or "").strip()
    return {
        "erp_name": name,
        "huawei_code": (values.get("store_code") or "").strip(),
        "marker": marker,
        "kind": (hit or {}).get("kind") or "",
        "huawei_name": (hit or {}).get("huawei_name") or "",
        "in_roster": bool(hit),
        #: 有没有串号标识 —— **有就要走玲珑**（那 14 家体验店）
        "needs_linglong": (True if _edition.is_lifehall() else bool(marker)),
        "platform": False,
        #: 菜单要不要**全开**（平台岗全开）
        "show_all": False,
        #: 三类身份之一：`experience` / `partner` / `platform`
        "type": "experience" if marker else "partner",
    }


def pick(raw: dict) -> dict:
    """挑出前端要展示/编辑的字段（点号路径）。"""
    out = {}
    for key in EDITABLE:
        node = raw
        for part in key.split("."):
            node = (node or {}).get(part) if isinstance(node, dict) else None
        out[key] = node
    return out


_NEEDS_QUOTE = re.compile(r"""[\s\[\]{}#&*!|>'"%@`,]|^[-?:]|:\s""")


def _fmt(value) -> str:
    """序列化成 YAML 标量。

    ⚠ 该加引号的必须加：`subject_prefix: [报量对账]` 里开头的 `[` 在 YAML 里是**流式列表**，
    不加引号会被解析成 `["报量对账"]` —— 值悄悄变成了列表，很难查。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "":
        return '""'
    if s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return f'"{s}"'
    # 长得像数字的字符串也要加引号，否则 "123" 存进去会变成 int 123
    if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s):
        return f'"{s}"'
    if _NEEDS_QUOTE.search(s) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def update(path, updates: dict) -> dict:
    """定点改值，保留注释。返回改完之后的原始配置 dict。

    updates 的 key 用点号路径（如 `check.lookback_days`）。
    """
    p = Path(path)
    unknown = [k for k in updates if k not in EDITABLE]
    if unknown:
        raise ValueError(f"这些字段不允许从界面改：{unknown}")

    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines()
    section = None
    pending = dict(updates)

    for i, line in enumerate(lines):
        m = re.match(r"^(\S[^:]*):", line)          # 顶格键 = 新 section
        if m:
            section = m.group(1).strip()
        m = re.match(r"^(\s*)([A-Za-z_][\w]*):(\s*)(.*)$", line)
        if not m:
            continue
        indent, key, _, rest = m.groups()
        full = key if not indent else f"{section}.{key}"
        if full not in pending:
            continue
        comment = ""
        cm = re.search(r"\s+#", rest)
        if cm:
            comment = rest[cm.start():]
        lines[i] = f"{indent}{key}: {_fmt(pending.pop(full))}{comment}"

    for key, value in pending.items():              # 文件里没有的键 → 追加
        *parents, leaf = key.split(".")
        if parents:
            lines.append("")
            lines.append(f"{parents[0]}:")
            lines.append(f"  {leaf}: {_fmt(value)}")
        else:
            lines.append(f"{leaf}: {_fmt(value)}")

    p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return load_raw(p)
