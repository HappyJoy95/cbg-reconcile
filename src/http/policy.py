"""公共页面与操作权限策略的派生规则。

功能页面和操作范围只在 ``features.registry`` 声明；本模块将声明整理成
HTTP 层读取的表。它不导入 App、Handler 或 ``src.web``，因此策略可以单独审阅。
"""


#: **页面 key → 谁能看见它**。业务功能的一级/二级页面来自注册表；
#: 左下角这几项是“这台电脑”的系统设置，不是业务功能，故在这里显式声明。
#:
#: 页面 key 与 HTML 的 `data-tab` / `data-subtab` / `data-foot` 值一致，
#: 前端只使用后端下发的 `pages`，不维护第二份可见性表。
FOOT_PAGES = {
    "account": "", "general": "", "update": "", "scheduler": "",
    # 主题是本机外观设置，全员可见。
    "theme": "",
    # 玲珑授权只对走玲珑的身份有意义。
    "linglong": "experience platform",
    # 数据交换展示多店数据，仅区长/平台身份可见，门店接口仍由 route guard 拒绝。
    "stores": "multi",
}


def build_page_rules() -> dict:
    """从业务注册表及本机设置派生页面可见性；不维护第二份功能清单。

    增加业务页只需在 `Feature` / `Sub` 声明 `types`；子页面未声明时继承父项。
    生活馆仅返回白名单页面，避免前端再按版本分支。
    """
    from ..features import registry
    from .. import edition

    out = {}
    for feature in registry.all_features():
        out[feature.key] = feature.types or ""
        for sub in feature.children:
            out[sub.key] = sub.types or feature.types or ""
    out.update(FOOT_PAGES)
    if edition.is_lifehall():
        # 生活馆只保留允许页面，页面权限继续由注册表与运行时 guard 判断。
        return {key: "" for key in out if key in edition.LIFEHALL_PAGES}
    return out


def build_perm_rules() -> dict:
    """从注册表派生操作权限和默认数据范围。

    `Sub.ops` / `Sub.data` 是操作身份与数据范围的唯一声明来源。
    """
    from ..features import registry

    return registry.page_perms()


#: 可见性词表中的 `multi` 表示“管多店的身份”（区长 / 平台）。
#: 区长同时带玲珑店型档位；平台同时带 `platform`，可见性判据按集合相交。
MULTI = "multi"
