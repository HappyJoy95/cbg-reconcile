# 运行支撑模块目录迁移设计

> 状态：用户已在 2026-10-04 确认“三阶段兼容迁移”方案。本文定义实施边界；逐文件步骤和验收命令另列实施计划。保留为本地未提交文档。

## 背景

本项目已将业务页面与业务 HTTP 处理放进 `web/features/` 和 `src/features/`，并通过显式注册声明权限和运行入口。剩下的基础设施实现仍散在 `src/` 根目录：外部系统客户端、Windows 桌面/任务调度实现，以及超过 6000 行的本机 HTTP 控制台。根层同时承载兼容导入，测试、CLI、bootstrap、自动更新和 Lifehall 裁剪规则都依赖这些路径。

本次只调整代码归属和模块装载关系。用户可见行为、API 请求/响应、任务名与顺序、业务权限、数据库路径和外部服务调用均保持不变。

## 目标结构

```text
src/
  integrations/       # ERP、CBG、浏览器、腾讯文档、PMall 与消息通道适配器
  desktop/            # Windows 自启动、计划任务、提权、服务与子进程支持
  http/               # 本机 HTTP 传输、路由门禁、公共 App 与服务启动
  web.py              # 兼容入口，继续向 CLI/旧调用方公开 App、Handler、serve 等名称
  erp.py ...          # 兼容入口；保留旧 import 路径并转到新实现
  features/           # 业务模块，现有注册协议不变
  modules/            # 六个系统能力门面，外部调用方式不变
```

目录和文件保持显式导入，不扫描目录、不引入插件发现或新运行时依赖。新包只收纳实现；现有 `src.web`、`src.erp`、`src.schedule` 等兼容路径在本次迁移中继续可用。

兼容入口必须保持**同一 Python 模块对象**，而非只复制公开名字：旧路径上的测试替身、模块级补丁与调用方访问私有兼容符号都应作用于新实现。新旧路径的对象身份和代表性 monkeypatch 行为列入回归测试。兼容层仅转发，不复制业务逻辑。

## 分阶段迁移

### 阶段 A：外部系统与服务适配器

将现有客户端/适配实现移入 `src/integrations/`：

* 云商与 CBG：`erp.py`、`erp_stub.py`、`cbg.py`；
* 浏览器会话与 CDP：`browser.py`、`cdp.py`；
* 腾讯文档与 PMall：`tdoc.py`、`pmall.py`；
* 消息通道适配：`mailer.py`、`wecom.py`。

玲珑数据入库的 `dump.py` 是抓取执行流程，不是纯客户端；`reconcile.py`、`report.py`、`bugreport.py`、`mailcrypto.py` 及更新/版本模块也不在本阶段移动。功能通过现有 `src.modules.auth`、`src.modules.fetch`、`src.modules.notify` 门面或已有调用服务访问实现；不允许业务页面直接绕过门面新增系统能力依赖。

云商相关文件继续遵守 Lifehall 不包含 ERP 能力的规则。更新 `src/edition.py::PRUNE` 和相应安装包/自更新测试，使完整版包含新实现与兼容入口、生活馆版同时裁掉 ERP 实现及会导入该实现的兼容入口。`PRUNE_ENABLED` 仍保持关闭。

### 阶段 B：桌面运行与 Windows 任务支持

将 `autostart.py`、`elevate.py`、`runtime.py`、`schedule.py`、`service.py`、`winutil.py`、`runner.py` 移入 `src/desktop/`，旧 `src.<module>` 路径保持模块身份兼容。

Windows 7 / Python 3.8.10 行为、普通用户权限、UAC 的按需提权、任务和 Run 项归属验证、计划任务名称与命令行保持不变。`bootstrap.py` 仍留在仓库根，且保持安装依赖前可运行的纯标准库入口；根入口和门店批处理文件的路径不变。

### 阶段 C：本机 HTTP 控制台

把 `src/web.py` 的 HTTP 实现拆进 `src/http/`，按清晰责任拆分通用路由与门禁、App 公共服务、请求/响应处理、静态文件与服务启动。业务 HTTP handler 继续归 `src/features/<业务>/http.py`，不搬回公共 HTTP 包。

`src/web.py` 保留兼容导入入口。外部仍可使用既有 `App`、`Handler`、`serve`、`ROOT`、路由策略函数及已公开辅助函数。请求处理仍使用标准库，不增加 Web 框架，不改变 loopback 默认绑定、Host/Origin 防护、setup/runtime 门禁、业务 route guard 或错误状态码。

实施计划会按现有函数之间的依赖和测试补丁点，把模块边界写成逐文件任务；若某组函数无法抽离且保持无循环依赖，则保留在 `src/http/app.py`，不为追求均匀文件大小再拆出抽象层。

## 更新、打包与回退边界

* `edition.PRUNE` 继续作为 Lifehall 文件排除的唯一声明源，并同时约束正式包和自更新目标；每个阶段都验证 zip 文件集。
* 增加路径迁移回归：完整版包包含实现和兼容入口；Lifehall 包没有 ERP 客户端及不可解析的兼容入口；安装更新后运行路径可用。
* 更新/回退测试使用临时安装根。验证新文件覆盖、旧路径兼容，以及配置、`.secrets/`、`out/`、`in/` 不被覆盖；不访问真实凭据、门店数据库或网络服务。
* 保持 `PRUNE_ENABLED=False`。本轮不启动真实项目服务、不执行真实更新或回退，不打包、不改版本、不同步发行仓。

## 验收标准

1. 三个阶段的新旧 import 路径指向同一模块对象；CLI、bootstrap、业务执行、timer、notify 和 feature HTTP dispatch 的原调用路径均有回归覆盖。
2. 现有行为测试保持通过；涉及 Windows 任务、浏览器、ERP/CBG、通知和 PMall 的测试使用隔离桩，不触发真实副作用。
3. Lifehall 裁剪后的临时包可导入并走允许路径；不能导入 ERP 客户端，也不能因 `src/erp.py` 等兼容入口引用被裁掉的文件而启动失败。
4. HTTP API 的路径、方法、请求体/响应体、状态码、Origin/Host 检查、权限门禁和生命周期与迁移前一致；业务 handlers 不反向导入 `src.web` 形成环。
5. 所有源码冻结后运行 Python 3.8、3.9、3.14 三套全量测试，并检查所有 `web/**/*.js`、注册表验证和 `git diff --check`。
6. 不覆盖当前未提交工作，不提交、不推送、不打包、不升版本；完成后报告本地工作区状态和无法进行的真实现场验收边界。

## 本次不做

* 不改变任何业务口径、权限或数据范围，不重做登录页、页面布局或菜单生成。
* 不迁移 `src/cli.py`、`bootstrap.py`、`src/run_daily.py`、`src/features/`、`src/modules/`、`src/app/` 或 `src/storage/`。
* 不把 `dump.py`、`reconcile.py`、`report.py`、`bugreport.py`、`selfupdate.py`、`whatsnew.py`、`upgrade.py` 或 `mailcrypto.py` 顺手并入新包。
* 不删除旧导入路径，不启用自动清理，不改变门店包/发行仓内容和升级渠道。
