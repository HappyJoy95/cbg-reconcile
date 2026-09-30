# -*- coding: utf-8 -*-
"""业务接口的 HTTP 出口 —— **不吃代理**。

## 为什么（2026-09-29 实测）

云商 / 华为 / 腾讯文档 / 企微都是**国内业务站**，不该看代理的脸色。那天
`127.0.0.1:7897` 的代理软件关了，可**代理环境变量还留在进程里**（进程是开着
代理时启动的，环境不会自愈），于是抓数全被送去一个没人听的端口：

```
ProxyError: HTTPSConnection(host='127.0.0.1', port=7897): Connection refused
❌ 云商销售拉取失败：连试 3 次网络都被掐断 → 退出码 2
```

`fetch_attempt` 里连挂三条、定时器那一趟记成失败 —— 界面上就是
「最近拉数据一直报错」。**去掉代理变量重跑当场成功**（15833 行落库），
所以这不是网络问题，是"死代理"。

## 两种用法

* **带登录态 / 会复用连接的**：`httputil.session()` —— `trust_env = False`，
  环境里的代理、`REQUESTS_CA_BUNDLE`、`.netrc` 一律不看。
* **一次性裸调**（那几处测试打桩打在 `requests.post` 上，别换调用形状）：
  `requests.post(url, ..., proxies=httputil.NO_PROXY)` ——
  `Session.merge_environment_settings` 只给**缺的键**补环境变量，
  显式给 `None` 就不会再被盖回去（`all` 是 `all_proxy` 那个 socks 键）。

⚠ **`selfupdate`（GitHub）不走这儿** —— 它**要**能吃代理（有的网络出不去）；
  代理死了它会失败，那是另一条路的事，别顺手改。
"""

from __future__ import annotations

import requests

#: 直连：三个键都显式置 `None`（见上面「一次性裸调」那段）。
NO_PROXY = {"http": None, "https": None, "all": None}


def session() -> "requests.Session":
    """一个**不吃代理**的 Session —— 带登录态的客户端用它。"""
    s = requests.Session()
    s.trust_env = False
    return s


__all__ = ["NO_PROXY", "session"]
