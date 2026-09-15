"""云商 ↔ 华为报量对账。

模块分工：
  session   华为会话（从粘贴的 curl 解析 cookie + csrf）
  cbg       华为取数：订单列表 → 逐单详情 → SN
  erp       云商取数：登录 → 当日销售明细
  reconcile 纯函数：归属过滤 + 差集（业务规则只在这里）
  report    输出 Excel + 控制台摘要
  cli       命令行编排
"""

__version__ = "0.1.0"
