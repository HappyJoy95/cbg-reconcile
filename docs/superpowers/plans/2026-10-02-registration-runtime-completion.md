# 功能注册制与统一进入流程：剩余执行计划

> **For agentic workers:** 按本计划逐项执行并在检查点记录证据；执行方式沿用本轮已获授权的团队安排。步骤以 `- [ ]` 跟踪，未取得对应证据不能勾选完成。

**Goal:** 完成既定注册协议、运行身份隔离、生活馆进入流程及业务归属，保持业务口径与现有执行语义。

**Architecture:** 安装 Edition 与运行入口是两个独立维度，共享运行上下文统一决定身份、页面、数据源与调度能力。业务显式声明 audience/ops/data/run，公共层执行权限和生命周期；业务接口与页面回归业务目录，`src/web.py` 只承担传输、公共门禁与显式派发。

**Tech Stack:** Python 3.8.10 / 3.9 / 3.14，现有标准库 HTTP 服务、pytest、原生 JavaScript，无框架、构建器或 CDN。

---

## 执行前提、边界与证据状态

本计划是原先仅保存在会话里的剩余执行清单的持久记录，承接
`.dsh/docs/2026-10-02-功能注册制与统一进入流程-开发目标.md`。
开工先读 `AGENTS.md` 与 `.dsh/docs/2026-09-19-开发指南-模块与功能接入.md`。
原开发目标的历史进展保留；其中早先 T6/T8 的“完成”只代表当时那批实现，不能据此认定本计划已完成。

**本轮不做：** 新业务或新指标口径、Edition 或更新渠道改造、安装包、版本号、提交或推送；
不清理真实配置、凭据、会话或历史数据，不触碰 `.secrets/`、`config/store-*.yaml`、`in/`、`out/` 的现场内容。
测试只能使用临时根目录、桩数据和隔离浏览器状态，不调用真实 ERP/玲珑取数，不发送邮件或企微。
**服务重启例外与现场写入边界：** 用户现场调试已正常重启本机服务，该启动会执行现有 boot/startup
状态写入，因此不能声称本轮真实 `.secrets/` 全部零写入。源码/测试执行没有自行读取凭据或操作
业务历史；不主动清理现场内容的边界继续有效。后端批次完成审查后，须同步服务重启再做 live 验证，
避免磁盘新静态 JS 与内存旧 Python 后端混版；重启前确认 capture 空闲且无子任务，沿用现有启动入口。
`src/features/tools/sn_trace/` 与 `tests/test_sn_trace.py` 的删除是另行批准的功能退役，必须保留，不恢复。
库存上游锁定的 `web/inventory/core.js`、`store.js`、`xlsx.js` 不在此次迁移改动范围。
**当前库存接入事实（已读代码）：** `web/index.html` 的 `#subpanel-inventory .inv-root`
是同文档内容，`web/app.js` 的 `mountInventory` 首次切页按 core → api → store → xlsx → ui
顺序懒加载，`focusInvScan` 直接聚焦当前面板的扫码框，不走库存 iframe 首键 postMessage。
用户本轮提供的旧 `AGENTS.md` 库存 iframe 段与当前代码不同；本轮不改该固定指南，
结构迁移保持当前同文档行为，不恢复旧 iframe。价签仍使用独立 iframe。

截至本计划写入时，已收到如下**实现者报告，且有独立只读规格/质量审查 PASS**：

| 批次 | 报告的完成内容 | 报告的验证 |
|---|---|---|
| Slice 1 | full 安装选 lifehall 时，`role_scope` 强制本地范围，清除返回身份里的旧 who/account/kind | Python 3.14：entry/roles 91 passed，108 subtests；独立规格与质量审查 PASS |
| Slice 2 | 两种 lifehall 的 code-only 门禁、entry API/UI、重新授权路径；不以 ERP logout 清除旧现场 | 三头相关测试各 595 passed，3.14 另 497 subtests；独立规格与质量审查 PASS |
| Slice 2 补修 | 可选玲珑授权完成/取消后安全返回 | 三头各 251 passed，3.14 另 235 subtests；JS/diff 检查通过；独立规格与质量审查 PASS |
| Slice 3（中间验证记录） | 共享 runtime helper、生活馆页面/API/权益玲珑源/启动/定时/daily 限制 | 实现者报告三头 expanded 各 665 passed，3.14 另 432 subtests；当时规格审查 PASS、质量审查进行中 |
| Slice 3（最终状态：完成） | 上述运行边界及 runtime_guard、收银 owner/事务、下载来源保护 | 实现者报告最终 16 文件三头各 703 passed，3.14 另 432 subtests；最终独立规格定向 7 passed、质量 runtime+guard 76 passed，规格与质量复核均 PASS |

以上是本轮其他实现者提供的证据，本次文档整理没有重新运行测试。
广泛权限/数据范围、前端生命周期及页面/HTTP 归属、最后三步执行迁移均未完成；最终三头全量测试尚未运行。

**Slice 3 最终补记（实现者报告，独立只读规格与质量复核 PASS）：**
`src/modules/auth/runtime_guard.py` 以本机线程互斥和 OS `flock` / `msvcrt` 进程锁
保护入口切换与工作执行；daily 整趟持锁并在每一步重验运行能力。
entry/config/logout/store-code/store-account/session 写入、capture、startup 与 cashier mutations
使用相同保护，原先任务切换、owner 盖章和 TOCTOU 问题已在审查中关闭。
`src/features/cashier/ownership.py` 将收银 DB 绑定单店；旧非空且无 owner 的库不自动绑定。
六个 mutator 的 owner 检查、绑定与实际写入处于同一 `BEGIN IMMEDIATE` 事务；
下载来源登记门店编码与 SHA-256 并核对后读取。
Windows 锁路径仅有 mock 验证；真实线程/进程互斥在本机验证，**未在真实 Win7 验证**。
以上完成的是 Slice 3 实现批次，不等于检查点 2/3 的全部最终浏览器验收已经完成。
`business_permissions_sol` 曾开始检查点 4；现场调试后该权限批次暂停，尚未改动源码。

**现场调试记录（主执行者报告，用户反馈尚待返回）：** 用户截图“保存编码进不去”。
只读 8787 健康/setup 发现旧 PID `40646` 启动于 `2026-10-02 19:38:06`，
setup 为 `entry_kind=lifehall`、`need=storecode`、`code_present=false`，且缺少 `runtime_lifehall`。
这说明静态新 JS 已加载，而旧 Python 进程尚未 reload，不能据此认定出现新的源码缺陷。
主执行者确认 capture idle、无 children 后停止旧 PID，使用
`.dsh/tasks/venv314/bin/python -m src.cli serve --no-open` 重启为 PID `60906`（session `39616`），
新 setup 返回 `runtime_lifehall=true`。没有代用户设置编码；已请用户刷新后重新保存，等待实际反馈。
相关 entry/lifehall 离线验证报告为 **37 passed + 6 subtests**；不据此声称现场保存已成功或有新代码修复。

**现场闭环补记：** 用户随后明确反馈“已经能进入”。主执行者只读 `/api/setup` 确认
`ready=true`、`need=""`、`entry_kind=lifehall`、`runtime_lifehall=true`。
编码由用户刷新后重新保存，执行者未代填；本次原因是旧后端进程缓存，并非当前源码缺陷。
`business_permissions_sol` 权限批次已恢复。该现场闭环不代表剩余全项目迁移或最终验收完成。

**库存数据范围确认（用户 2026-10-02 明确）：** 库存盘点“表外码索引”继续查询全公司串号，
用于定位条码当前所在仓库；这是显式的 `all` 数据范围例外，只适用于该索引操作，并继续受
库存页面现有 audience 与操作权限约束。库存其他读、导出、写入仍按各自现有业务权限和范围声明执行；
不能把索引的全仓能力推成库存全页默认范围，也不能改变库存计算口径。
用户随后进一步明确：仓库列表、账面和在途查询仅覆盖本店/已授权店。故 `/api/inventory/index`
是唯一全仓例外；仓库列表应按有效授权过滤，账面/在途的目标仓在服务端校验，导出与写入按目标店范围校验。

## 检查点 1：保存工作区基线，列出每项缺口

**现有文件：** `src/web.py`、`src/edition.py`、`src/features/registry.py`、
`src/features/__init__.py`、`src/run_daily.py`、`src/cli.py`、`web/app.js`、
`web/index.html`、`web/common/base.js`、`web/common/nav.js`、`tests/test_entry_flow.py`、`tests/test_perm_protocol.py`。

- [x] 执行 `git status --short --branch` 与 `git diff --stat`，记录分支、已有改动及新增文件；不 reset、checkout 或覆盖已有工作。
- [x] 对照显式业务清单、路由规则和页面注册，核对页面、HTTP 方法/路径、op、data、取数/导出/写入点、Step.run 与前端生命周期实际归属；矩阵校验见 `tests/test_route_policy.py`、`tests/test_registry.py`、`tests/test_web_ia.py`。
- [x] 基线曾记录 `_RUNNERS` 与 `web/features/` 尚未迁移；后续页面/handler 批次已建立业务目录，2026-10-04 检查点 7 也已删除 daily 的第二套路由表。
- [x] 将缺口映射到检查点 2–8 与对应测试文件；退役 sn_trace 只作为删除基线记录，不列为待迁移业务。

**通过条件：** 后续每个改动能与基线区分，未完成项全部有归属；历史测试数字不被表述为本轮最终验证。

**本轮基线记录：** 分支 `main`，已有工作区变更，`git status --short --branch` 共 82 行状态项；
tracked diff 为 71 个文件、2142 insertions / 1565 deletions，另有未跟踪计划、共享运行模块与测试/前端目录。
`src/features/tools/sn_trace/` 和 `tests/test_sn_trace.py` 删除按另行批准保留；测试隔离修复目前仅改
`tests/test_run_daily.py`、`tests/test_run_what.py`、`tests/test_run_check.py`，测试审查指出库存范围断言
需依用户新增确认同步调整，已转交权限实现者处理。未重置、切换或覆盖其他既有工作。

## 检查点 2：统一运行身份和能力上下文

**现有文件：** `src/web.py`（entry_state/save/clear、setup_state、role_scope）、
`src/edition.py`、`src/features/__init__.py`、`src/features/registry.py`、
`src/startup.py`、`src/modules/timer/__init__.py`、`src/run_daily.py`。
**新增共享入口（Slice 3 实现者已确认）：** `src/modules/auth/runtime.py`，提供
`is_lifehall(root=None)`、`page_available(key, root=None, lifehall=None)`、
`step_available(cmd, root=None, recurring=False)`、`api_available(path, root=None)`。
不要误用现有 `src/runtime.py`（它负责安装时 Python 路径）。这些能力函数是当前运行边界，
后续 audience 声明迁移仍属检查点 4，不能把 helper 的存在当作完整 audience 协议已经落地。
**已新增保护文件：** `src/modules/auth/runtime_guard.py`、`src/features/cashier/ownership.py`。
**测试：** `tests/test_entry_flow.py`、`tests/test_roles.py`、`tests/test_platform_identity.py`、
`tests/test_edition_registry.py`、`tests/test_edition_api.py`、`tests/test_startup_refresh.py`、`tests/test_timer.py`、`tests/test_run_daily.py`。

- [x] 在共享 helper 中显式区分安装 Edition、当前入口、业务 audience 与有效身份/范围，所有使用方依赖同一结果。
- [x] 保持 full 安装的授权店/平台入口能力；full 选 lifehall 与 lifehall 安装运行入口等价，旧 ERP manager/platform 不进入有效角色、账号显示或授权范围。
- [x] 验证选择、保存、重启、重选全链：旧 ERP 凭据仍原样保留，但不能借旧状态越过 lifehall 限制；不通过注销 ERP 或删配置实现隔离。
- [x] 对页面、API、手动刷新、启动取数、内置 timer 与 `daily --steps` 使用相同能力边界，直接调被禁止接口/步骤同样拒绝。
- [x] 生活馆保留批准的价签/权益及通用设置、主题、玲珑授权；不继承 ERP、完整业务推送、工牌或业务定时器。自动更新延续现有允许行为，不改变更新渠道。

**通过条件：** 双 lifehall 模式在 UI、API、数据源和后台执行中一致；旧平台权限没有任何反向提升路径。
验证证据：`tests/test_entry_flow.py`、`tests/test_edition_api.py`、`tests/test_startup_refresh.py`、`tests/test_timer.py`、`tests/test_run_daily.py`；2026-10-04 隔离 Playwright 还验证了四身份页面访问与生活馆入口行为。检查点 8 记录最终验收边界。

## 检查点 3：完成两种生活馆的 code-only 与可选授权回程

**现有文件：** `src/web.py`（setup_state、_setup_state_lifehall、entry 与 store-code 路由）、
`web/app.js`、`web/index.html`、`src/features/tools/claim/__init__.py`、
`src/features/tools/claim/pending/compute.py`、`src/features/tools/claim/submit.py`。
**测试：** `tests/test_setup_gate.py`、`tests/test_lifehall_login.py`、`tests/test_entry_flow.py`、
`tests/test_claim.py`、`tests/test_claim_scope.py`、`tests/test_claim_batch.py`、`tests/test_claim_submit.py`。

- [x] 复核已有 Slice 2：保存非空门店编码即可 ready；空编码返回填码步骤。不得增加 ERP 登录、身份识别、编码查店或玲珑 session 前置条件。
- [x] 覆盖 full 选 lifehall 与 lifehall 安装，在无 ERP/无玲珑 session、旧无效 session、旧平台 ERP 等状态下均可按编码进入。
- [x] 玲珑授权由用户主动进入，授权缺失只影响需要玲珑的具体操作；成功、取消、失败均回到合法来源页，不重新锁成登录门禁，也不跳往越权页。
- [x] 生活馆权益数据仅走批准的玲珑来源，保持既有 `category_id` 与真实串号口径；无授权时明确提示所缺授权，不回退读取 ERP 数据。
  Slice 3 实现者确认 compute 的入口将接受 `source=None` 或显式 `linglong` / `erp`；
  运行上下文选定来源，生活馆传 `linglong`，`None` 的兼容路径不能绕过运行限制。

**通过条件：** 两种模式保存编码后可进入价签；玲珑专属操作按自身授权状态响应，返回路径可用。已报告通过项复核后附准确批次证据，不宣称真数据验证。

## 检查点 4：补全业务 audience / ops / data 并贯通双端

**现有声明：** `src/features/registry.py`、`src/features/__init__.py`，以及
`src/features/compliance/__init__.py`、`compliance/pos/__init__.py`、`compliance/comparison/__init__.py`、
`sales/attain/__init__.py`、`plan/monthly/__init__.py`、`valueadd/film/__init__.py`、
`valueadd/benefit/__init__.py`、`inventory/__init__.py`、`distribution/__init__.py`、
`cashier/__init__.py`、`store/__init__.py`、`tools/__init__.py`、`tools/claim/__init__.py`（上述相对路径均在 `src/features/`）。
**执行方：** `src/web.py`（PAGE_RULES/PERM_RULES、require、_can_for）、`web/common/nav.js`、`web/app.js`。
**测试：** `tests/test_registry.py`、`tests/test_perm_protocol.py`、`tests/test_roles.py`、
`tests/test_web.py`、`tests/test_web_ia.py`、`tests/test_edition_api.py`、`tests/test_cashier.py`、`tests/test_distribution.py`、`tests/test_store_account.py`。

- [x] 逐页填写 audience、四种 op 及 store/authorized 数据范围；身份与门店类型独立表达，不另造一套 role 判断。父级继承与子级覆盖必须可追踪。
- [x] 逐条 HTTP 路由登记对应 page/op，非业务公共入口和 foot 页单独显式声明机制权限；迁移结束后不能以“老路由”维持隐式放行。
- [x] 未知 page/op/audience、未声明操作、无有效运行身份默认拒绝；前端 payload 缺失或加载异常同样不开放菜单/按钮。
- [x] 从同一声明派生菜单、页面、role.ops 与后端 require；覆盖按钮隐藏后直接发请求仍 403，及已声明合法身份正常 200。
- [x] 保持原有操作许可，如区长只读/平台导出等；发现未清楚确认的业务权限不借此次结构迁移扩大。旧全局停止 API 已退役，不再提供跨页面终止任务的未归属操作。

**通过条件：** 清单中每条业务路由均对应显式 page/op；角色 × audience × op 矩阵双端一致，未知项无默认全开路径。证据：`tests/test_route_policy.py` 的逐路由声明分类、972 组合门禁矩阵及合法/非法直调测试；`tests/test_perm_protocol.py` 的注册派生与默认拒绝；`tests/test_web_ia.py` 的 fail-closed 菜单和控件；最新路由专项 330 passed、60 subtests，三头全量 3591 passed（3.14 另 1407 subtests）。这项完成不代表检查点 5 的数据目标/副作用范围验收完成。

## 检查点 5：统一所有读、导出与写入的数据范围

**现有文件：** `src/web.py`（filter_scoped、filter_attain_rows/filter_plan_rows/filter_film_rows/filter_benefit_rows），
`src/features/sales/attain/attain.py`、`split.py`、`export.py`，
`src/features/plan/monthly/plan.py`、`export.py`，`src/features/valueadd/film/compute.py`、`export.py`，
`src/features/valueadd/benefit/compute.py`、`export.py`，`src/features/distribution/store.py`、`region.py`、`export.py`，
`src/features/inventory/book.py`、`push.py`，`src/features/cashier/store.py`、`exporter.py`，
`src/features/tools/claim/pending/compute.py`、`src/features/tools/claim/submit.py`。
**测试：** `tests/test_perm_protocol.py`、`tests/test_attain.py`、`tests/test_plan_web.py`、
`tests/test_export.py`、`tests/test_film_compute.py`、`tests/test_benefit_snapshot.py`、
`tests/test_distribution.py`、`tests/test_inventory.py`、`tests/test_cashier.py`、`tests/test_claim_scope.py`。

- [x] 为每个业务的列表、详情、汇总、导出、提交及目标店参数映射同一个声明范围；前端过滤不能代替后端过滤。
- [x] 覆盖本店、获授权多店、越权店、缺门店键、空授权集合、伪造目标店；空范围只能返回空结果或明确拒绝，不能表达全量。
- [x] 导出过滤在文件生成前完成；写入先检查目标店权限再落库；汇总由已过滤行计算，不能泄露其他店计数或总额。
- [x] 平台全量必须由显式有效授权表达，不能由 None/空集合猜测；lifehall 的范围只由当前本地编码确定。
- [x] 库存 `表外码索引` 的全仓查询作为用户确认的**单操作 `all` 范围例外**显式登记；不由未知/缺失 scope 得到，也不扩展到库存其他操作。

**通过条件：** 页面、导出、写入结果一致；拒绝的写入未产生副作用；所有缺失/空 scope 测试无跨店数据。

## 检查点 6：公共页面生命周期及业务页面 / HTTP 归属

**现有文件：** `web/common/base.js`、`web/common/nav.js`、`web/app.js`、`web/index.html`、
`src/web.py`、`src/features/registry.py`、`src/features/__init__.py`。
**新增目标：** `web/features/<业务>/` 的页面脚本与业务资源；业务 HTTP handler 放在对应
`src/features/<业务>/`，由显式声明或清单派发。以下是下一阶段确定的文件落点，
对既有独立库存/价签应用保留现有页面资源，仅迁移公共接入与后端归属：

| 业务 | 新页面脚本 | 新 HTTP handler |
|---|---|---|
| 防护膜 | `web/features/valueadd/film/page.js` | `src/features/valueadd/film/http.py` |
| 周度达成 | `web/features/sales/attain/page.js` | `src/features/sales/attain/http.py` |
| 月度计划 | `web/features/plan/monthly/page.js` | `src/features/plan/monthly/http.py` |
| 权益达成 | `web/features/valueadd/benefit/page.js` | `src/features/valueadd/benefit/http.py` |
| POS | `web/features/compliance/pos/page.js` | `src/features/compliance/pos/http.py` |
| 双平台对比 | `web/features/compliance/comparison/page.js` | `src/features/compliance/comparison/http.py` |
| 分销 | `web/features/distribution/page.js` | `src/features/distribution/http.py` |
| 收银 | `web/features/cashier/page.js` | `src/features/cashier/http.py` |
| 人员 | `web/features/store/page.js` | `src/features/store/http.py` |
| 权益领取 | `web/features/tools/claim/page.js` | `src/features/tools/claim/http.py` |
| 库存接入 | `web/features/inventory/page.js` | `src/features/inventory/http.py` |

检查点 1 的显式清单若发现其余已存在业务页面，必须在动该批源码前补记其确切落点；
不能漏迁，也不能新增业务。价签保持 `web/tools/price-tag/`，库存引擎保持 `web/inventory/`。
**测试：** `tests/test_web_ia.py`、`tests/test_registry.py`、`tests/test_module_layout.py`、
`tests/test_modules.py`、`tests/test_web.py`、`tests/test_web_origin.py`、`tests/test_theme.py`、`tests/test_inventory.py`。

- [x] 公共 nav 提供显式页面注册、挂载、加载、卸载/取消与刷新契约；切页时释放监听与定时回调，防止重复挂载和旧请求覆盖新页。
- [x] 按同一业务批次搬页面 loader/render/state 与 HTTP 数据处理：先 film，再已迁移的 attain/monthly/benefit，再按清单迁其余业务；搬移时不改计算口径。
- [x] `web/app.js` 保留公共启动及入口协调，业务脚本使用 base/nav 的公共能力；`index.html` 按依赖顺序加载所有本地资源，原生 JS 不引入框架、CDN 或构建步骤。
- [x] `src/web.py` 保留 HTTP 解析、origin/setup/runtime/权限门禁、静态资源与派发；业务计算、过滤、导出/写入处理在所属业务 handler，handler 不反向 import web.py 造成环。
- [x] 保留库存同文档 `.inv-root`、按 core → api → store → xlsx → ui 顺序首次懒加载、
  `focusInvScan` 的扫码焦点与现有接入；不恢复库存 iframe 或首键 postMessage，不动锁定引擎。
  价签继续保留现有独立目录与 iframe 接入，不为统一目录破坏这些边界。
- [x] 每批运行对应路由/IA/模块测试与 `node --check`；隔离 Playwright 覆盖四身份的全部可见业务/系统页面与角色控件，业务生命周期由页面 IA 用例覆盖；无失败请求、JS 异常或 console error。

**通过条件：** 清单中的业务页面与 handler 全有业务归属；web/app 和 web.py 不再承担那些业务分支；离线资源完整、无模块环、生命周期可测。

**首屏权限时序补修（2026-10-02，局部完成，不代表检查点 6 完成）：** 截图中的
“读取周度目标达成情况失败：生活馆版没有这个功能”来自启动先固定执行
`switchTab('sales')`、立即调用 `/api/attain`，之后才由 `/api/overview.role.pages`
隐藏该页。已改为先等待 `loadOverview()` 应用服务端页面权限，再由公共导航选择首个
可见一级页和可见子页；生活馆首屏落在“小工具 → 价签”，full 版仍落在“周度重点产品 →
周度目标达成情况”。新增 `tests/test_web_ia.py` 回归覆盖启动顺序、生活馆不触发 attain
loader、full 版默认页不变。定向测试：3.14 下 **417 passed、235 subtests passed**，
另有 1 条既有通知偏好断言因本批无关的 Lifehall `plat:mail` / `plat:wecom` 返回值而失败，
该断言被单独排除重跑；尚未处理。`node --check` 与本次文件的 `git diff --check` 通过。
未做现场浏览器刷新或服务重启。

## 检查点 7：拆出最后三步，删除最终 _RUNNERS

**现有文件：** `src/cli.py`（cmd_dump/cmd_erp_dump/cmd_pools、require_session、_find_pos_db、_record_fetch、_pool_day），
`src/run_daily.py`、`src/features/registry.py`、`src/modules/fetch/__init__.py`、
`src/dump.py`、`src/pools.py`、`src/pools_history.py`、`src/pools_notify.py`、`src/app/data_state.py`、
`src/features/compliance/comparison/__init__.py`。
**新增目标：** `src/modules/fetch/execution.py` 承接 dump/erp-dump 与通用采集编排，
`src/features/compliance/comparison/execution.py` 承接 pools 计算/通知编排；
实现入口接收现有 ctx/参数并返回退出码，不把业务执行重新塞回 CLI。
**测试：** `tests/test_run_daily.py`、`tests/test_run_what.py`、`tests/test_run_check.py`、
`tests/test_dump_db.py`、`tests/test_pools.py`、`tests/test_pools_history.py`、
`tests/test_pools_notify.py`、`tests/test_pools_sample.py`、`tests/test_pools_category.py`、
`tests/test_data_state.py`、`tests/test_timer.py`、`tests/test_timer_once.py`、`tests/test_schedule_replace.py`。

- [x] 用离线桩固定 dump 的首次建库/当月参数、会话续期、只抓不算、玲珑池与 ERP 池拆分、异常/退出码及采集尝试记录；通用池调度逐池记录结果、保留成功池后继续处理其他池。
- [x] 将实际编排从 `cmd_*` 提出；CLI 保留 argparse 与薄适配入口位置，执行模块不 import cli。`Step.run(ctx)` 直接调用实现，CLI 薄适配复用该实现。
- [x] 核对物理 Lifehall 包的真实 PRUNE 保留资源：用临时目录按 `edition.PRUNE` 移除实际文件，确认 `src.pools` 不存在时 `cmd_dump` 成功和失败两条路径均不导入四池模块；两条路径都记录 dump 尝试状态。后续拆分抓取执行入口时必须继续保留这条回归。
- [x] dump/erp-dump 保持 fatal，pools 保持现有非致命语义；全部 Step 使用声明的 partner_ok/fatal/record，autoupdate 的 record=False 不进 done/汇总。
- [x] 删除 `_RUNNERS` 及 CLI 闭包回退，用注册入口唯一派发；结构测试改为“所有已注册步骤都有 run，daily 不依赖 cli.cmd_*”。
- [x] 验证 `daily` 无 --steps 仍 EXIT_USAGE；按点名顺序运行，timer 同刻按表顺序，一次性登记走相同链；未知/被 runtime 禁止步骤在执行前拒绝。

**通过条件：** 显式注册步骤均有可执行 run，无最终 RUNNERS 或第二名单；顺序、fatal、record、partner 和 wake-slot 记录语义与迁移前一致。

## 检查点 8：集中验收与文档同步

**现有文件：** `tests/`、`tests/test_selfupdate.py`、`tests/test_edition_update.py`、
`tests/test_bootstrap.py`、`src/selfupdate.py`、`src/edition.py`、`bootstrap.py`、
`.dsh/docs/2026-10-02-功能注册制与统一进入流程-开发目标.md`、
`.dsh/docs/2026-09-19-开发指南-模块与功能接入.md`、`AGENTS.md`。

- [x] 前面每批的 focused pytest 失败先定位并修复，记录精确命令、解释器与最终 passed/subtests 数；TDD 红灯均在修复后复跑通过，结构断言有行为与隔离浏览器证据补足。
- [x] 检查当前本机服务状态：8787 无监听进程，因此没有现有服务可同步重启；为避免启动真实项目服务触发门店定时/业务取数，不伪造 live-server 验收。隔离静态服务和 API mock 的浏览器验收见下。
- [x] 全部源码冻结后并行运行三个头，不在测试运行中改源码：

```bash
.dsh/tasks/venv38/bin/python -m pytest tests/ -q
.dsh/tasks/venv39/bin/python -m pytest tests/ -q
.dsh/tasks/venv314/bin/python -m pytest tests/ -q
```

- [x] 对本地 JS 文件逐个执行 `node --check`，执行 `git diff --check`，均退出 0。
- [x] 隔离浏览器覆盖授权店登录、平台显式确认、三种 ERP/生活馆入口、全部已迁页面及权限显隐；记录本地资源请求和 console/network，四身份均无页面错误。
- [x] 在无外网业务依赖的本地隔离浏览器中检查本地页面资源；用临时更新/回退夹具验证新增 runtime helper、业务脚本/handler 被正确覆盖和回滚，保留配置/秘密/历史不动。未执行真实更新、未建安装包、未改渠道。
  Lifehall 的更新/prune 离线模拟必须按真实保留资源构造临时目录，并覆盖上述 dump 成功后路径；
  本项当前尚未验证。
- [x] 更新开发指南的真实入口与新增文件清单、目标文档的完成/未完成状态；为每项结论标注单测、隔离浏览器或实现者报告来源。
- [x] 最后记录 `git status --short --branch` 与差异范围，确认 sn_trace 删除保留、版本与发布脚本无无关改动，留下未提交工作区。

**最终通过条件：** 所有前置检查点有证据；3.8.10 / 3.9 / 3.14 全量全绿；关键浏览器与离线/更新回退资源验收完成。
任何尚缺测试、页面迁移或运行路径必须列为未完成，不能将“已有基础协议”表述成“全项目注册制完成”。

## 执行进展（2026-10-03）

**登录入口按钮修正：** 授权店 / 平台岗 ERP 登录卡片移除“退出”；该按钮原来只调用
`window.close()`，浏览器通常不会允许网页关闭用户自己打开的标签页。保留卡片底部右侧
“换进入方式”，继续复用既有入口切换流程。`tests/test_entry_flow.py`、
`tests/test_setup_gate.py` 与 `tests/test_store_account.py` 覆盖按钮保留和旧按钮缺席。

**通用业务导出下载的权限补修：** 新增 `src/modules/auth/export_access.py`，完整版本的六个
业务导出路由（周度达成、防护膜、权益、分销、月度计划、收银）在导出成功后登记业务页、
生成时门店范围和文件 SHA-256；通用 GET 下载按登记页重新执行 `export` 权限，并要求
生成范围落在当前授权范围内。没有来源记录、内容已替换、范围更宽或页面权限已撤销时拒绝；
平台全量文件仅能由平台全量范围下载。共享下载路由本身有延迟解析的显式路由规则，页码只从
服务端来源记录读取，不接受客户端指定。业务导出与通用下载还持有运行身份锁，避免页面权限
检查、数据生成和来源登记之间切换入口造成授权范围与文件内容错位。生活馆继续沿用现有门店
编码与内容摘要绑定的收银下载路径，不改变 code-only 登录。

验证：Python 3.14 隔离定向套件 **1,029 passed、418 subtests passed**（导出、路由权限、
角色范围、库存、生活馆、登录页等）；`node --check` 覆盖 `web/` 下所有本地 JS，
`git diff --check` 通过。新增下载权限核心用例另在 Python 3.8 和 3.9 各 **8 passed**。
本轮该套件没有报告项目根目录输出文件变化。
此前一次较早的宽定向测试曾提示 `out/cbg-2026.db` 与 `out/plan-2026.json` 被测试修改；
后续已把 `tests/test_web.py` 的相关测试根目录改为临时目录。本轮没有读取、清理或恢复这两份
现场输出文件，重新执行的隔离套件没有再出现该警告。

**尚未完成：** 检查点 1 的完整路由 / 页面 / 执行归属清单仍缺；检查点 4/5 的全部业务接口、
数据查询与写入还需逐项对照注册声明和实参范围；检查点 6/7 的页面生命周期、HTTP handler
归属与最终三步执行迁移尚未完成；没有运行 3.8.10 / 3.9 / 3.14 全量测试，也没有做现场
浏览器、服务重启或更新/回退验收。不得把本次导出下载修补描述成“权限隔离已全部完成”。

**报量报告路由范围补修（2026-10-03，局部完成）：** `/api/overview` 的历史报告摘要现在按
当前 `pools.view` 与授权门店过滤；详情、下载、删除先验证报告旁车中的门店归属，缺失或损坏的
归属对门店/区长默认拒绝。平台全量身份仍可查看归属旁车损坏但工作簿有效的报告。报告路径限定
为 `out/` 直属 `.xlsx`，拒绝目录穿越、相似前缀目录及符号链接越界；损坏旁车由 `src/report.py`
安全回退为工作簿摘要。相关回归覆盖 `tests/test_route_policy.py` 与 `tests/test_report.py`。

验证：路由/报告/权限协议/导出/生活馆运行边界 focused 套件在 Python 3.8、3.9、3.14 各
**177 passed**；只读质量复核另以 `tests/test_route_policy.py tests/test_report.py` 三头各
**64 passed**，无阻塞问题；`git diff --check` 通过。质量复核提示检查与文件读取并非原子快照，
并发删除可能使请求失败；未发现越权路径。授权下载正向 HTTP 和 symlink 专项用例仍可补强。

**整体仍未完成：** 上述修补只关闭已发现的报量报告范围泄露，不能替代检查点 1/4/5 的完整
路由与数据范围盘点，也不表示权限隔离已完成；其余未完成项沿用上文。

**周度达成历史范围与路径补修（2026-10-03，局部完成）：** /api/attain/history 的历史列表原样
返回全区门店数和平均达成率，现按当前 attain.data 授权行重算两个摘要；选中周的明细仍走既有
授权过滤。归档读取参数现在只接受 YYYY-W01 至 YYYY-W53，非法期间 fail closed，阻止
period 路径穿越尝试读取归档目录外的 JSON。

验证：先用接口回归确认原行为会把单店可见的 stores=1 返回为全区 stores=2，并确认路径穿越
能读到隔离临时目录中的合成 sentinel；修复后路由/报告/权限协议/导出/生活馆边界/达成六个测试文件
三头各 **314 passed**（3.14 另 **22 subtests**），tests/test_web.py 历史接口集成三头各
**5 passed**。完整权限盘点与全量三头、浏览器、更新/回退验收仍未完成。

**POS 与库存目标范围补修（2026-10-03，局部完成）：** `/api/pos` 原样返回整份预计算汇总，
其注册范围虽为 `authorized`，汇总 JSON 却没有门店维度。执行模块现于同一 SQLite 读事务中记录
参与计算的订单/退货来源门店快照；读取端按当前 scope 逐店校验，缺快照、缺身份、店码未映射、
或有任一未授权门店时，有限范围身份只收到空结果和说明。平台全量身份仍可读取汇总，接口继续只读
落盘 JSON。旧汇总没有快照时，有限范围身份 fail closed，需重新计算后恢复显示。

库存下拉与账面/在途目标校验此前把仓库 `BranchName` 和 `Name` 作“任一匹配即可”；已改为
优先按所属门店 `BranchName` 校验，仅其缺失时才回退仓库 `Name`，冲突字段拒绝。全仓串号索引
继续保留已确认的 `all` 单路由例外。

TDD 回归先复现 POS 越权汇总可读、POS 缺失来源快照仍被展示，以及所属乙店但名称命中甲店的
仓库进入下拉并可被选为账面目标。修复后路由、POS、库存、报告、达成、权限协议、导出、生活馆、
月度计划、收银、权益领取、分销等 16 个 focused 测试文件在 Python 3.8、3.9、3.14 各
**729 passed**（3.14 另 **293 subtests passed**）；`git diff --check` 通过；检查到
`out/`、`in/`、`.secrets/` 没有工作区变化。

**仍未完成：** 这次只关闭发现的 POS 来源范围和库存所属仓校验缺口；检查点 1/4/5 全量路由、
身份矩阵、所有数据查询/导出/写入的逐项审计仍未签完。注册业务之外的系统设置页也仍需完整核对；
检查点 6/7 的页面与 HTTP 归属、最后三步执行迁移、三头全量测试、现场浏览器和更新/回退验收继续
保持未完成。不得据此宣称权限隔离已全部完成。

**多来源门店身份冲突补修（2026-10-03，局部完成）：** 继续逐项核验跨系统来源字段后，发现
四池历史行带有“云商门店”和“玲珑门店”时，旧逻辑只要任一字段获授权就会放行；现要求所有
非空来源门店均属于当前授权范围，缺来源仍拒绝。`/api/staff` 的区长人员状态表此前把上报包
里的店名与店码当作 OR 兜底；现通过服务端门店名单将店码绑定到门店，已知店码要求包内店名
一致且该门店全部别名在授权范围，未知/冲突店码对有限范围身份 fail closed。平台全量读取保持。

TDD 先分别复现“乙店云商 + 甲店玲珑”的串号行泄露，以及“乙店店码 + 甲店自报名称”的人员表泄露；
修复后，路由/报告/权限协议/导出/生活馆边界/达成/库存/收银/权益领取/分销/人员上报等 17 个
focused 测试文件在 Python 3.8、3.9 各 **749 passed**，Python 3.14 **749 passed、293 subtests passed**。
注册表只读盘点得到 8 个一级功能、27 个页面、50 条精确业务路由；`registry.validate()` 无问题，
全部 50 条路由都有有效 `data` 声明，6 条 foot 系统路由、1 条请求体选页的刷新入口、1 条服务端
来源解析的导出下载入口另行登记。此前清单里的静态扫描还显示系统配置、登录、计划任务、
更新等 API 不属于业务注册路由，需逐项记录其现有门禁与设置页访问策略，不能一概视为业务路由遗漏。

同轮关闭了三处身份来源边界与一处声明漂移：报告旁车的店名/华为店码必须映射到同一授权门店；
POS 店码若映射到多家门店，全部映射别名必须获授权；权益领取声明现为 `authorized`，与已定的
门店本店、区长辖区、平台全量一致，生活馆依当前本地单店 scope 收窄。每个漏洞均先以回归复现。

**仍未完成：** 路由 × audience × op × data 全量清单及行为审计仍未签完；尤其要继续覆盖系统/foot
接口、所有数据读取和副作用写入。页面与 HTTP handler 迁移、dump/erp-dump/pools 执行收编、三头
全量测试、现场浏览器、真实资源裁剪与更新/回退验收均未完成。权限隔离整体仍标记为未完成。

**检查点 6 局部推进（2026-10-03，film 前后端归属与生命周期试点）：** 防护膜的渲染、读取、门店展开、刷新和导出已从 `web/app.js` 搬至
`web/features/valueadd/film/page.js`；`/api/film`、导出与明细路由及对应过滤/计算/导出处理归到
`src/features/valueadd/film/http.py`。公共 `web.py` 保留统一路由门禁、日期解析、导出登记，并通过
薄适配方法维持旧调用接口；业务 handler 不反向导入 `web.py`。公共导航新增显式 `registerPage` /
mount-load-unmount-refresh 生命周期；film 离开页面会取消请求并以序号阻止过期响应覆盖新页面。

先加结构回归并观察预期失败，再迁移实现。前端 IA、Web API、权限协议、film 与路由策略五个测试文件在
Python 3.8、3.9、3.14 各 **452 passed**，3.14 另 **255 subtests passed**；三个 JS 文件 `node --check`
和 `git diff --check` 通过。该证据是静态/HTTP 专项测试，不是浏览器验收；仍有其余业务页未迁移，旧
`SUBTAB_LOADERS` 兼容路径保留，生命周期切入/切出、延迟回包和重复刷新尚未做隔离浏览器实测。

**检查点 6 仍未完成：** 其他业务页面与 HTTP handler、剩余页面的生命周期/取消能力及浏览器验证仍待处理；
检查点 1/4/5 全量审计、检查点 7 执行收编、全量三头测试、服务重启与更新/回退验收也未完成。权限隔离整体继续标记为未完成。

**检查点 6 第二批（2026-10-03，benefit 与 film 同目录迁移）：** 无忧会员权益的四视图渲染、页面状态、读取、刷新、导出已从 `web/app.js` 搬至
`web/features/valueadd/benefit/page.js`，并注册到公共页面生命周期；切页会取消读请求、忽略旧回包。
权益页过滤与汇总、取数、导出、明细路由放在 `src/features/valueadd/benefit/http.py`；`web.py` 只作
门禁/协议入口并保留兼容适配方法。四视图过滤与汇总逻辑按原实现迁移，未改业务口径。

更新所有静态前端测试夹具，让它们依 `index.html` 的 base → nav → film → benefit → app 顺序拼接。
此前一条导出测试仍只拼公共文件和 `app.js`，捕获到假设失效后已改为跟随页面资源顺序。十个相关测试文件在
Python 3.8、3.9、3.14 各 **581 passed**，3.14 另 **300 subtests passed**；JS 语法和 diff 检查通过。
仍未做隔离浏览器验证；其余业务页与 handler 尚未迁移。

**系统与登录路由权限补齐（2026-10-03，局部完成）：** 继续盘点后，将静态 method/path handler
分到明确规则：50 条业务路由由 `registry` 声明，通用 foot 页接口进入 `FOOT_ROUTE_RULES`，
登录/进入 API 进入 `ENTRY_ROUTE_RULES`；设置页按可见页面执行身份、audience、op 门禁，登录前只放行
明确标记的入口和凭据录入。新增 AST 回归，新增静态 API 分支若没有业务/foot/entry/payload/export
声明就失败。扫描覆盖 122 个静态 method/path 组合，`registry.validate()` 返回空问题列表。

额外修正生活馆顺序：未保存门店编码前不能直接读/抓玲珑会话；旧 `/api/store-account/logout`
在生活馆版返回 404，避免旧 ERP 清理接口删掉生活馆本机编码，完整版本的身份清理能力保留。
账号、更新、定时、主题、运行日志等系统接口的显式规则不改变原有完整版本角色许可；生活馆仍先受
`LIFEHALL_APIS` 运行能力表限制。此次盘点没有改变业务范围口径。

回归中修复 `auth.export_access -> notify.export -> auth.runtime` 的模块环：导出目录常量与路径函数
下沉到无环的 `src/storage/export_paths.py`，保留 `export_dir(root, subdir)` 原签名。旧源码测试定位
同步跟随 `_capture_worker_locked` 和 `_api_unlocked` 实际实现函数。

验证：系统/登录/权限定向组 Python 3.14 **467 passed、111 subtests passed**；导出/收银/权限相关
回归三头各 **542 passed**（3.14 另 **153 subtests**）；最终全量 `tests/` 三头通过：Python 3.8
**3504 passed**、3.9 **3504 passed**、3.14 **3504 passed、1355 subtests passed**。`web/**/*.js`
逐个 `node --check` 与 `git diff --check` 均通过。全量测试不代表真实浏览器或现场运行验证。

**权限整体仍未完成：** 现有规则覆盖和测试已明显推进，但仍需按检查点 4/5 逐页核实角色 × audience ×
op 与真实读/写/导出实参范围，完成剩余业务页面/HTTP handler 迁移、`dump`/`erp-dump`/`pools` 执行收编，
并做隔离浏览器与 Lifehall 裁剪/更新回退验收。全量离线测试通过不替代这些验收。

**2026-10-03 补充进展（概览与运行日志范围）**：发现 `/api/overview` 的数据健康摘要是全机数据源汇总，
其明细包含全库行数、采集日期、失败原因和内部指纹；对门店/区长改为只返回是否就绪和通用状态，平台岗保留完整诊断。
`/api/runlog` 的计划任务日志会包含双平台差异的串号、机型、门店和单号，且没有逐行来源证明；对非平台身份隐藏日志行和推送细节，
仍保留运行元数据，平台岗可看完整日志。

回归先证明门店可从 overview 获得合成全库行数 987654、从 runlog 获得合成跨店串号，修复后，
`tests/test_route_policy.py` **75 passed**，`tests/test_web.py -k RunLog` **9 passed**。随后 Python 3.8/3.9
全量各 **3513 passed**，Python 3.14 **3513 passed、1355 subtests passed**；全部前端 JS 通过 `node --check`，
`registry.validate()` 返回空问题列表，`git diff --check` 通过。本批之后仍要完成系统 API 与业务读写范围复核、
隔离浏览器及裁剪更新验收；以上不代表权限隔离完成。

**2026-10-03 再补充（上报收信响应）**：区长可发起整批收信，但原响应带回全公司门店编码、包数/行数、失败明细、
目标拆分人数及逐行日志，无法安全地按辖区过滤。现在区长只收到成功/跳过状态，界面提示查看授权范围内门店卡片；
平台岗保留完整结果和日志。TDD 回归复现了合成的越权门店编码与全局行数，`tests/test_route_policy.py` **77 passed**，
收信/权限相关定向测试 **10 passed、5 subtests**。最终全量 Python 3.8/3.9 各 **3515 passed**，Python 3.14
**3515 passed、1355 subtests passed**；变更后的 JS 语法、注册表和 diff 检查通过。其他系统/业务接口范围、隔离浏览器及更新/回退验收仍未完成。

**2026-10-03 补充进展（数据交换门店卡片身份）**：区长读取每店上报卡片时，旧逻辑按华为店码关联收信记录，却未校验包内门店名是否与门店名单中该店码的别名一致；同一个店码被多个 ERP 门店复用时也会错误归属。现在受限身份要求店码唯一归属、该码全部别名都在授权范围、上报包非空店名与名单相符。冲突卡片不含上报业务摘要，逐日详情 API 对 unknown/不安全卡片返回统一范围拒绝；受限身份不再使用包内自报门店名兜底。卡片同时附带的收信失败列表也按唯一授权店码收窄，并将原始附件名和错误正文替换为通用提示；未归属/冲突店码隐藏。平台全量仍保留完整诊断读取。

先加回归并确认可复现：授权编码收到别店名上报包、授权/未授权两店共用一个编码、错误列表混入乙店和未知门店的名字/错误文本。修复后路由/角色/人员上报测试 **164 passed、108 subtests passed**；Python 3.8 和 3.9 全量各 **3519 passed**，Python 3.14 **3519 passed、1355 subtests passed**；前端语法及 diff 检查通过。

**仍未完成：** 这只收紧数据交换卡片与明细的来源身份，不等于系统 API 或全部业务读写范围审计结束。其他系统/业务接口、页面与 HTTP handler 迁移、dump/erp-dump/pools 执行收编、隔离浏览器及 Lifehall 更新/回退验收仍待完成，权限隔离整体继续标记为未完成。

**2026-10-03 补充进展（实时运行日志范围）**：实时任务轮询接口 /api/run 原先把运行子进程原始输出、完整命令和输出行数返回给所有角色，此前只有 /api/runlog 的落盘日志被收窄。现在受限身份只收到运行状态、耗时及退出码，隐藏输出行、命令、步骤标签与行数；各业务刷新和强制重刷起始回包也复用过滤，平台岗保留完整日志。前端运行抽屉提示日志仅平台岗可查看，刷新完成或失败状态仍能看到。

回归先复现区长读取含乙店名及合成跨店串号的后台输出。路由/角色/人员上报/Web 测试 **373 passed、168 subtests passed**；全量 Python 3.8 与 3.9 各 **3521 passed**，Python 3.14 **3521 passed、1355 subtests passed**；全部前端 JS 语法、注册表校验及 diff 检查通过。

**仍未完成：** 这只收紧实时运行详情读取，不表示系统 API、所有业务行为或副作用范围已经全量审计。其余系统接口与业务读写范围、页面/HTTP handler 迁移、dump/erp-dump/pools 执行收编、隔离浏览器及 Lifehall 裁剪/更新回退验收继续待办。

**2026-10-03 补充进展（运行快照旁路与操作归属）**：沿任务快照继续追踪后发现 overview 也附带最近 RunJob 完整输出，绕过了 /api/run 的过滤；现在 overview、轮询和启动刷新回包共用同一脱敏函数。非平台身份只看到任务状态、耗时、退出码，平台岗保留完整日志。权益领取状态接口同时改为使用服务端身份记录操作人，忽略请求体自报姓名。

TDD 分别复现 overview 泄露合成乙店串号，以及客户端将状态修改归到“伪造的平台岗”。路由/角色/人员上报/Web/领取范围定向测试 **384 passed、168 subtests passed**；全量 Python 3.8/3.9 各 **3524 passed**，Python 3.14 **3524 passed、1355 subtests passed**。全部 web JS 语法检查、注册表校验和 diff 检查通过；out、in、.secrets 没有工作区变化。

**仍未完成：** 这只关闭实时任务快照旁路和一项写入审计归属问题。其余权限范围行为、系统接口与业务读写副作用、页面/HTTP handler 迁移、执行步骤收编、现场浏览器及 Lifehall 裁剪/更新回退验收仍待完成。

**2026-10-03 补充进展（周度达成 HTTP 路由归属）**：达成读取、导出、历史与目标拆分的 HTTP 处理已移入 `src/features/sales/attain/http.py`；公共入口仍先执行注册路由门禁，再把统一 `require`、门店范围、导出登记和请求解析能力注入业务 handler，既有 App 业务实现不变。新增测试确认 Web 确实委托到业务 handler，并把旧的“路由源码必须在 web.py”结构测试改为验证业务归属。

定向测试 **600 passed、191 subtests passed**；Python 3.8/3.9 全量各 **3525 passed**，Python 3.14 **3525 passed、1355 subtests passed**；全部 web JS 语法检查及注册表校验通过。达成页面仍留在 `web/app.js`，不能据此标记检查点 6 完成。

**仍未完成：** 其他业务页面与 HTTP handler、所有剩余读写/导出数据范围复核、`dump/erp-dump/pools` 执行收编、隔离浏览器与 Lifehall 裁剪/更新回退验收继续待办，权限隔离整体仍未完成。

**2026-10-03 补充进展（业务 HTTP 归属与范围门禁）**：继续把周度达成、月度计划、权益领取、分销、库存、POS、收银的 HTTP 路由处理分别迁入对应 `src/features/<业务>/http.py`；此前已迁入的防护膜和无忧会员权益 handler 也新增显式 `ROUTES` 清单。公共入口仍先跑精确 method/path 注册门禁，再向 handler 注入 `require`、目标店校验、请求解析和导出登记等公共能力。库存路由保留“表外码索引”唯一全仓例外，仓库列表/账面/在途仍校验授权范围；收银操作继续按功能声明逐路由要求权限。新增回归验证 Web 委托关系及 9 个业务 HTTP 清单与注册表一致。

路由策略定向套件 **104 passed**；完整 `tests/` 在 Python 3.8、3.9 各 **3542 passed**，Python 3.14 **3542 passed、1355 subtests passed**。所有本地 web JS 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。全量离线验证只覆盖已测路径，不能代替其余读写/导出范围审计或现场验收。

**仍未完成：** 系统 API 与全部业务目标店/结果集的逐项权限审计、权益领取真实提交操作的角色确认、其他页面脚本与 HTTP handler 迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器和 Lifehall 裁剪/更新回退验收。权限隔离整体仍未完成；没有提交、推送或打包。

**2026-10-03 补充进展（前端操作权限 fail-closed）**：周度达成、月度计划、防护膜和无忧会员权益的导出按钮现按 `role.ops` 显隐；达成拆分的动态保存/发送按钮既检查服务端给出的本店可编辑范围，也检查 `attain.modify` 操作授权，插入 DOM 后再执行统一显隐。账号与人员页是明确登记的 foot/system 页面，其 `account.modify` 从 `FOOT_ROUTE_RULES` 下发给前端，三个名单编辑控件不再依赖逐按钮身份判断。

继续复现权限载荷缺失路径：只有 `pages`、没有角色或 `ops` 时，旧实现仍露出菜单；总览 API 失败时也沿用上次画像。现在角色、页面表或操作表任一缺失/非法都会隐藏菜单、页面面板和已标记操作；`/api/overview` 失败会清空前端权限。页面内容键与 `PAGE_RULES` 增加一致性回归，定时器页用 `data-page-key="scheduler"` 明确映射。更新仍要求旧 `role.can['attain.export']` 的测试契约到 `role.ops` 协议。

权限/前端定向组 **118 passed**；最终全量 `tests/` 在 Python 3.8、3.9 各 **3549 passed**，Python 3.14 **3549 passed、1393 subtests passed**；所有 `web/**/*.js` 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。

**仍未完成：** 全量路由 × audience × op × data 与目标店参数审计、权益领取外部真实提交的角色确认、其余业务页脚本/HTTP 归属、`dump/erp-dump/pools` 执行收编、隔离浏览器、Lifehall 裁剪与更新/回退验收仍待完成。离线全量测试不等于现场/浏览器验收；本次未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（周度达成页面归属迁移，局部完成）：** 当前周达成、只读历史、区域折叠、销售明细弹层、门店目标拆分编辑、刷新与导出从 `web/app.js` 移至 `web/features/sales/attain/page.js`；当前周和历史页分别注册 `mount/load/unmount/refresh` 生命周期。离页取消挂起 GET、丢弃过期响应，委托交互在其他页不生效；原计算、权限与目标店范围保持。

新增业务页迁移结构回归并调整前端脚本测试夹具。定向组 **594 passed**；最终全量 `tests/` 在 Python 3.8、3.9 各 **3550 passed**，Python 3.14 **3550 passed、1399 subtests passed**；所有 `web/**/*.js` `node --check` 通过，`registry.validate()` 返回 `[]`，`git diff --check` 通过。首次全量发现主题测试仍只拼接 `app.js`，已同步加入新页面脚本并重跑三头通过。

本机 8787 服务未运行且 Python 测试环境未安装 Playwright，没有隔离浏览器/真实页面切换证据。检查点 6 仍未完成：其他业务页面与 handler、完整生命周期浏览器验证待续；权限隔离及数据范围审计、`dump/erp-dump/pools` 执行收编、Lifehall 裁剪和更新/回退验收仍未完成。未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（系统配置身份字段）**：TDD 复现门店可通过通用 `PUT /api/config` 写入 `platform: true`，并可改门店名、华为门店编码、串号标识和云商机构号。由于这些字段参与服务端角色及数据范围解析，通用设置路由不应承担身份授权；现将五项加入 API 拒写名单，保留读取能力和普通机器设置写入，认证登录、Lifehall 店码流程及退出清理仍由专用处理器写入。回归覆盖 store/manager/platform 三种角色，并验证普通时区字段仍能保存。

权限路由/登录/Lifehall 专项 **267 passed、63 subtests passed**；Python 3.8 / 3.9 全量各 **3556 passed**，Python 3.14 **3556 passed、1399 subtests passed**。这只关闭通用配置 API 的身份字段写入旁路；完整系统 API 与业务结果集/目标店审计、领取真实提交角色确认、剩余页面生命周期/HTTP 归属、`erp-dump/pools` 执行收编、隔离浏览器与 Lifehall 更新/回退验收仍待完成。未重启现场服务、未打包、未提交或推送。


**2026-10-03 补充进展（权限登记硬化与 Lifehall 裁剪抓取链补修）**：`registry.validate()` 新增业务路由权限页归属检查，阻止一个业务借用其他一级功能或其子页的权限；路由策略回归要求业务、系统、登录入口、延迟下载和请求体选页分类两两不重叠。当前登记为 50 条业务路由、58 条系统路由、19 条登录入口，以及各 1 条请求体选页和延迟下载，静态类别无重叠；这不代表完整角色 × 操作 × 数据/目标店审计已结束。

追踪 Lifehall 物理包依赖后发现，裁剪掉 `src/pools.py` 后，保留的 `cmd_dump` 仍会在抓取成功时调用 `cmd_pools`，导致“数据已抓到但步骤报错”。现 Lifehall 成功后跳过只供完整版库存对比使用的后续池抓取，full 版仍照旧。TDD 回归确认入口不再调用该池；另在隔离临时目录按 `edition.PRUNE` 删除实际裁剪资源，桩替远程读取，模拟 `cmd_dump` 成功退出。

权限注册表与路由专项 **126 passed**，生活馆运行边界 / 日常流程 **129 passed**。全量 `tests/`：Python 3.8、3.9 各 **3555 passed**，Python 3.14 **3555 passed、1399 subtests passed**；注册表自检 `[]`、web JS 语法和 `git diff --check` 通过。无真实账号、网络业务数据或现场服务访问。

**仍未完成：** 权益领取外部真实提交的角色确认、系统与业务接口所有目标店/结果集审计、其余前端页面/handler 迁移、`erp-dump/pools` 执行收编、隔离浏览器与 Lifehall 更新/回退验收。权限隔离整体未完成；没有重启、打包、提交或推送。


**2026-10-03 补充进展（月度计划页面归属迁移，局部完成）：** 月度生意计划的状态、渲染、区域/门店/列交互、读取、刷新与导出已从 `web/app.js` 移到 `web/features/plan/monthly/page.js`，并注册 `mount/load/unmount/refresh` 生命周期；离页会取消 GET、忽略迟到回包并清理动画。公共导航不再用兼容 loader 加载该页；月度与周度达成共用 `normalizeRegionName`。没有改统计或数据范围口径。

新增页面归属及离页中止回归。前端专项 **382 passed、213 subtests passed**；源码冻结后的完整 `tests/` 在 Python 3.8 / 3.9 各 **3560 passed**，Python 3.14 **3560 passed、1407 subtests passed**；所有 `web/**/*.js` 通过 `node --check`，`git diff --check` 通过。

用户明确确认：权益领取真实提交继续允许门店、区长、平台三种身份，保持现有声明；这只解决提交角色授权问题，不代表其他权限与范围审计完成。没有隔离浏览器验证，本机服务未运行；未重启、打包、提交或推送。

**仍未完成：** 全部系统 API 与业务结果/目标店逐项审计、剩余页面和 handler 迁移、`erp-dump/pools` 执行收编、隔离浏览器及 Lifehall 裁剪/更新回退验收。检查点 4、5、6、7、8 均未因此完成，权限隔离整体仍未完成。

**2026-10-03 补充进展（业务页可见性按查看权限收窄）**：发现收银页注册的 `cashier:view` 仅授予门店，但菜单页清单只按进入方式/门店类型生成，区长和平台岗仍看得到入口；点开后接口才返回 403。现在业务页面可见性同时检查注册表 `view` 角色，收银入口只对门店显示；系统 foot 页面继续使用各自规则，后端各接口的操作权限与拒绝文案保持不变。

TDD 先观察到区长侧菜单错误包含收银，再修正。权限/角色/注册表/前端 IA/收银 focused 套件 **441 passed**；完整 `tests/`：Python 3.8 / 3.9 各 **3561 passed**，Python 3.14 **3561 passed、1407 subtests passed**；所有 `web/**/*.js` 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。

用户已确认：权益领取外部真实提交保持门店、区长、平台三种身份可提交。系统 API 与全部业务数据/目标店范围审计、其余页面与 HTTP handler 生命周期、`erp-dump/pools` 执行收编、隔离浏览器及 Lifehall 裁剪/更新回退验收仍未完成；没有重启现场服务、打包、提交或推送。

**2026-10-03 补充进展（请求身份快照与登录写入互斥）**：并发复核发现，业务请求通过路由门禁后，App 旧方法可能再次读取当前身份；若期间身份从门店切为平台，原门店请求会按新平台范围读出全量数据。现业务请求复用通过门禁时的线程级身份快照，App 内部重读也得到同一范围。另将云商凭据保存/登录/验证码、门店账号凭据、玲珑自动抓取、华为登录凭据等身份相关写入补入运行上下文锁；拿不到锁时返回 409，不派发到业务处理器，拿到锁后在身份写请求内重新解析当前范围。

TDD 先分别复现身份切换造成的跨店读取，以及上述写入绕过锁；修复后 `tests/test_runtime_guard.py tests/test_route_policy.py tests/test_claim.py tests/test_claim_scope.py` 在 Python 3.9 **185 passed**。新增权益领取提交授权回归使用桩验证门店、区长、平台三种身份均保留权限；没有请求真实华为接口。本批之后尚未重跑三头全量测试。

**仍未完成：** 系统 API 与业务读/写/导出目标范围的逐项审计、其他业务页面及 HTTP 生命周期迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器、Lifehall 裁剪与更新/回退验收；权限隔离整体仍未完成。未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（未验证门店账号写接口提权修复）**：进一步核查登录身份来源时发现，`POST/PUT /api/store-account` 能只写账号名、不验证密码，而 `role_scope()` 会按该账号名识别区长。对已配置的门店，这使得提交区长账号名即可把身份从本店提升到区长辖区。TDD 回归先复现请求返回 200 且随后身份变为区长；现在这两个旧写方法明确返回 410，不修改凭据。当前前端使用的 `/api/store-account/lookup` 仍会先验证云商登录，再由服务端保存账号与 token。

验证：门店账号、登录门禁、路由策略、运行上下文锁和入口流程专项在 Python 3.9 **235 passed**，覆盖旧写入口不能改变身份，以及已验证登录流程仍工作。随后源码冻结并完成三头全量测试：Python 3.8、3.9 各 **3581 passed**；Python 3.14 **3581 passed、1407 subtests passed**。全量离线验证没有访问真实业务数据，也不能替代现场浏览器与更新/回退验收。

**仍未完成：** 其余系统 API 与业务读/写/导出目标范围的逐项审计、其他业务页面及 HTTP 生命周期迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器、Lifehall 裁剪与更新/回退验收；权限隔离整体仍未完成。未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（业务/系统路由角色与进入方式矩阵）**：新增路由契约回归，对注册表中的 50 条业务路由与 58 条系统路由，逐一检查门店、区长、平台岗 × ERP、平台、生活馆共 **972 种组合**。断言角色、操作权限、进入方式或 Lifehall 页面/API 裁剪任一不匹配时，整体门禁必须拒绝。它验证的是身份/入口/操作声明执行一致性，不替代 handler 内逐个读写/导出目标店的数据范围审计。

路由策略专项 **115 passed**；注册表校验 `[]`，全部 web JS 语法检查与 `git diff --check` 通过。源码冻结后三头全量：Python 3.8、3.9 各 **3581 passed**；Python 3.14 **3581 passed、1407 subtests passed**。测试与只读核查未改动 `.secrets/`、`in/`、`out/`。

**仍未完成：** handler 内所有目标店/结果集与系统副作用的逐项审计、数据交换区长收信写入是否也应限制到辖区（待用户确认）、其他页面生命周期/HTTP handler 迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器及 Lifehall 裁剪与更新/回退验收。权限隔离整体仍未完成；未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（系统计划任务目标校验）**：审计发现 `/api/schedule/run` 与 `DELETE /api/schedule` 把请求传入的任务名直接交给 `schtasks`，未确认任务属于本程序；构造任意 Windows 任务名即可触发运行或删除。另发现 `/api/elevate` 的 `schedule-remove` 会把任意名字交给 UAC 子进程，绕过普通删除接口。现在三个入口都先与本机计划任务清单精确匹配；非本程序任务、清单读取失败时返回 404，不调用系统命令或弹 UAC。无名称仍保留原有默认任务行为，清单里的任务仍可运行/删除。

新增未登记目标拒绝、提权前拒绝和清单内合法任务保留回归；路由策略 **120 passed**，计划任务专项 **82 passed**，系统计划任务 API 专项 **5 passed**。最终三头全量：Python 3.8、3.9 各 **3586 passed**；Python 3.14 **3586 passed、1407 subtests passed**。注册表校验 `[]`，所有 web JS 语法检查、`git diff --check` 通过；`.secrets/`、`in/`、`out/` 没有工作区变化。

**仍未完成：** 其余系统副作用与业务读写/导出目标店的逐项审计、区长收信写入范围（用户偏好待回）、其他页面生命周期/HTTP handler 迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器及 Lifehall 裁剪与更新/回退验收。权限隔离整体仍未完成；本轮未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（路由范围声明 fail-closed）：** 复核发现注册表原本允许某条业务路由将 `authorized` 收窄为 `store`，但公共路由门禁只验证范围词有效，业务 handler 实际仍按页面级范围执行。当前正式路由没有这种未生效的收窄覆盖；不过旧校验会让未来的声明产生错误安全感。现 `registry.validate()` 拒绝尚无运行时执行支持的路由范围收窄；“扩大页面范围”的既有拒绝及库存 `/api/inventory/index` 唯一确认的 `all` 例外不变。新增的合成注册声明回归先复现旧行为，再固定为启动期拒绝。

`tests/test_route_policy.py tests/test_perm_protocol.py` 在 Python 3.8、3.9 各 **161 passed**；Python 3.14 **161 passed、58 subtests passed**。本批只跑了权限路由定向测试，尚未重跑三头全量。不能据此表示所有业务目标店范围均已审计；数据交换区长收信写入范围仍待用户确认。

**2026-10-03 补充进展（注册范围收窄与静态目录边界）**：继续确认业务路由不支持“页面授权范围 authorized、单条路由却声明 store”的窄化，因为现有 handler 实际按页面范围执行；注册表现在启动期拒绝这种会造成虚假安全感的声明，库存 `/api/inventory/index` 全仓索引例外保持不变。系统接口复核还发现静态文件服务用字符串前缀比较目录，构造 `../web-private/secret.txt` 可越出 `web/` 读取同级目录文件。新增 HTTP 回归先复现返回 200 和文件内容，修复后用解析路径的祖先关系限制到 `web/`，越界返回 404。

本批回归包括权限协议/路由测试、壁纸与静态服务测试；源码冻结后完整三头测试均通过：Python 3.8、3.9、3.14 各 **3587 passed**，3.14 另 **1407 subtests passed**。全部 `web/**/*.js` 通过语法检查，`registry.validate()` 返回 `[]`，`git diff --check` 通过。没有重启服务、访问现场账号/业务数据、打包、提交或推送。

**仍未完成：** 区长收信是否只导入授权辖区仍待用户决定；其他系统副作用及业务读写/导出目标范围仍需逐项审计，剩余页面生命周期与 `dump/erp-dump/pools` 执行收编、隔离浏览器及 Lifehall 裁剪/更新回退验收也未完成。权限隔离整体继续标记为未完成。

**2026-10-03 补充进展（旧全局停止入口退役）：** 系统路由复核发现 `POST /api/run/stop` 没有前端调用方，却会直接终止 RunManager 当前任务，不校验任务 id 或来源；旧页面请求可能打断别的页面刷新或内置定时任务。先加 HTTP 回归并确认旧行为返回 200 且触碰当前任务，再让入口返回明确的 410，不再读取/终止共享任务。登记路由保留，以便当前版本把过期请求解释为已退役，而不是落入未登记路由。

验证：`tests/test_web.py tests/test_route_policy.py` **330 passed、60 subtests passed**；源码冻结后三头全量：Python 3.8、3.9 各 **3589 passed**，Python 3.14 **3589 passed、1407 subtests passed**。隔离合成浏览器在平台角色环境中来回切换月度与分销页面，分别只触发 GET `/api/plan` 和 GET `/api/dist/board`，无浏览器异常或写请求。全部 web JS 通过 `node --check`，注册表校验 `[]`，`git diff --check` 通过。

**仍未完成：** `POST /api/report/inbox` 区长是否应只导入授权辖区已向用户询问，等待业务决定；其他业务 handler 的结果集/目标门店与写入副作用范围还要逐项复核，剩余页面生命周期与 HTTP handler 迁移、`dump/erp-dump/pools` 执行收编、完整角色浏览器覆盖及 Lifehall 更新/回退验收仍待完成。权限隔离整体继续标记为未完成；未重启现场服务、未打包、未提交或推送。

**2026-10-03 补充进展（玲珑自检与换身份互斥）：** 复核身份锁覆盖表时发现 `POST /api/session/ping` 虽会读取当前门店会话并把自检结果写回本机，但不在 context guard 名单中；与换店码/身份写入并发时，自检可能记到另一家店。先把该路由加入两条运行上下文锁回归，确认“锁占用返回 409”及“取得锁后重新解析身份”两项都失败；随后将所有 `/api/session/*` 写方法纳入同一互斥与身份重解析路径。没有调用真实玲珑接口。

验证：`tests/test_runtime_guard.py tests/test_route_policy.py tests/test_web.py` **351 passed、60 subtests passed**。源码冻结后三头全量：Python 3.8、3.9 各 **3591 passed**；Python 3.14 **3591 passed、1407 subtests passed**。只读业务范围审查没有发现新的读/聚合/导出/写入跨店问题；区长 `/api/report/inbox` 的导入范围仍等待用户决定，未改其行为。

**仍未完成：** 身份自检并发问题已关闭，但系统操作范围的其余现场验收、`/api/report/inbox` 的范围决定、页面/HTTP handler 生命周期迁移、`dump/erp-dump/pools` 执行收编、完整角色浏览器与 Lifehall 更新/回退验收仍待完成。权限隔离整体继续标记为未完成；没有重启现场服务、打包、提交或推送。

**2026-10-03 补充进展（POS 页面进入注册生命周期）：** 按检查点 6 的清单，将 POS 读取/渲染/刷新处理从 `web/app.js` 移至 `web/features/compliance/pos/page.js`，在公共 `PAGE_REGISTRY` 注册 mount/load/unmount/refresh；切离会中止未完成读取并解绑刷新监听，重新进入时只绑定一次。`src/features/compliance/pos/http.py` 已先前迁移，POS 读盘和权限范围没有改动；同时更新 `index.html` 的本地脚本顺序及缓存版本号。

验证：POS、前端 IA、权限路由、运行身份锁与 Web 专项 **507 passed、249 subtests passed**；隔离浏览器实测 POS → 月度 → POS，只有 `/api/pos` GET，离页点击不触发刷新，重新进入仅一次监听，console 无异常。全量三头：Python 3.8、3.9 各 **3592 passed**；Python 3.14 **3592 passed、1407 subtests passed**；所有 web JS 通过 `node --check`，注册表校验 `[]`，`git diff --check` 通过。

**仍未完成：** POS 一页生命周期与归属已迁移，不代表其余比较、收银、人员、权益领取、库存等页面及 HTTP handler 完成；系统/业务现场范围验收、区长收信导入范围决定、`dump/erp-dump/pools` 执行收编、其余页面浏览器覆盖与 Lifehall 更新/回退验收继续待办。权限隔离整体仍未完成；未重启现场服务、未打包、未提交或推送。

**2026-10-03 补充进展（报量查询页面进入注册生命周期）：** 将报量查询历史、单日明细和页面刷新接入移至 `web/features/compliance/comparison/page.js`，通过公共页面注册表管理挂载、读取、卸载与刷新。全局 `loadOverview()` 不再读取该页历史；页面进入时读取，离开时取消历史/明细请求、清理 30 秒轮询并解绑事件。独立刷新仍按原行为先跑 `pools`，结束后重读总览与历史。没有改 API 的业务数据范围；生活馆入口不再跟随全局总览调用该功能。

验证：新接线回归先按预期失败，再通过；报量查询/IA/路由专项 **271 passed、191 subtests passed**。隔离合成浏览器覆盖历史和明细显示、离页中止延迟请求、离页刷新无调用、重复挂载不重复绑定；无页面异常。前端主题测试改为扫描 `web/` 全部本地 JS，避免页面模块迁出 `app.js` 后误报闲置配色令牌。源码冻结后三头全量：Python 3.8、3.9 各 **3595 passed**；Python 3.14 **3595 passed、1407 subtests passed**；全部 `web/**/*.js` 通过语法检查，`registry.validate()` 返回 `[]`，`git diff --check` 通过。未改 `.secrets/`、`in/`、`out/`、版本文件或发布脚本。

**仍未完成：** 报量查询页面完成不代表检查点 6 的其他页面/HTTP handler 完成。区长 `/api/report/inbox` 的辖区导入范围仍等用户决定，其余业务读写目标与系统副作用审计、`dump/erp-dump/pools` 执行收编、完整角色浏览器验收及 Lifehall 裁剪/更新回退验收仍未完成。权限隔离整体继续标记为未完成；未重启服务、未打包、未提交或推送。

**2026-10-03 补充进展（比较历史 HTTP 处理归属）：** 报量查询的 `/api/pools/history` 与 `/api/pools-notify/clear` 从公共 `src/web.py` 分支迁入 `src/features/compliance/comparison/http.py`。处理器复用统一身份快照和 `require()` 门禁，保留历史逐日/逐年数据范围判断及清理推送记忆的既有行为；业务 handler 不依赖 `web.py`。`/api/report/inbox` 未迁移、未改变，区长导入辖区范围继续等待用户决定。

**2026-10-03 补充进展（收银页面进入注册生命周期）：** 收银的页面状态、扫码反查、录入、卡片编辑、当日流水、导入、政策刷新与导出从 `web/app.js` 移至 `web/features/cashier/page.js`；删除 `SUBTAB_LOADERS` 兼容加载，改为显式注册 mount/load/unmount。页面卸载会中止待完成的查询并丢弃迟到结果；写入仍按原请求完成，不因离页被取消。HTML 明确先加载该模块，版本缓存号递增。收银后端 `src/features/cashier/http.py` 和注册的门店-only `view/enter/modify/export` 权限没有改动。

验证：收银/前端 IA/Web/路由/注册表专项 **597 passed、362 subtests passed**；Node 隔离运行用例确认卸载中止 GET 且忽略迟到结果。源码冻结后的完整三头：Python 3.8、3.9 各 **3600 passed**；Python 3.14 **3600 passed、1409 subtests passed**。全部 `web/**/*.js` 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。未重启服务、未访问现场账号/业务数据、未打包、未提交或推送。

**仍未完成：** 收银与比较历史迁移只完成各自页面/HTTP 归属的一部分，不代表其余业务页面和 handler 已完成。所有系统副作用、业务目标店与返回集合审计仍需收尾；区长 `/api/report/inbox` 导入范围待业务决定；`dump/erp-dump/pools` 执行收编、其余页面生命周期与浏览器验收、完整角色浏览器验证及 Lifehall 裁剪/更新回退验收仍待完成。权限隔离整体继续标记为未完成。

**2026-10-03 补充进展（权益领取页面进入注册生命周期）：** 活动一览、待领清单、筛选分页、在线/批量领取交互和弹窗从 `web/app.js` 移至 `web/features/tools/claim/page.js`；静态全局监听器改为页面 mount 时只绑定一次，注册 `load/unmount` 生命周期。页面卸载中止待领及活动 GET 并忽略迟到结果；用户已发起的领取与状态写入仍按原逻辑完成。权益领取 HTTP handler 已先前归属 `src/features/tools/claim/http.py`；门店、区长、平台的 view/enter/modify 权限声明和真实华为提交路径均保持原状，没有发出华为请求。

验证：权益领取/批量/登录门禁/页面 IA/Web/主题专项 **469 passed、427 subtests passed**；隔离 Node 生命周期用例验证离页中止读取和丢弃迟到结果。第一次全量运行暴露旧静态测试夹具没有加载新页面脚本，加入该脚本后相关门禁回归 **48 passed、45 subtests passed**。最终全量三头：Python 3.8、3.9 各 **3604 passed**；Python 3.14 **3604 passed、1409 subtests passed**。所有 `web/**/*.js` 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过；`.secrets/`、`in/`、`out/`、版本号与发布脚本未改。未进行真实浏览器/现场操作、重启服务、打包、提交或推送。

**仍未完成：** 收银与权益领取两页接入生命周期不代表其余业务页完成；区长 `/api/report/inbox` 导入辖区范围仍待业务决定。系统副作用与其余业务读写/导出范围审计、其余页面/handler 迁移、`dump/erp-dump/pools` 执行收编、隔离浏览器覆盖、Lifehall 裁剪和更新/回退验收仍待完成；权限隔离整体继续标记为未完成。

**2026-10-03 补充进展（区长收信范围与报量报告归属）：** 用户确认区长导入范围应限制为授权区域门店。新增共享 `modules.auth.inbox_scope`，Web 手动收信、CLI 收信和注册定时步骤都按同一门店编码映射解析范围；编码别名跨越授权边界、名单不可读或身份不明时 fail closed，只有显式全局平台身份得到全量。上报 DB 包先在临时文件读取 manifest、目标拆分先读取 `store_code`，两者都在写入 `in/` 或更新收信库前检查授权；区长响应不回传未按店拆分的收信明细和日志。上一批范围专项三头定向测试报告为 501 passed，随后加入库存 lifecycle 的组合批次为 731 passed；本次继续回归时相关五组测试三头各 **357 passed**，Python 3.14 另 **60 subtests passed**。

同批将 `/api/report` 详情、删除和下载的业务处理迁入 `src/features/compliance/comparison/http.py`，门店归属、店码别名一致性、损坏旁车拒绝、目录穿越限制及下载字节响应保持在迁移后的处理链。新增回归证明跨店请求不会读取、删除或下载文件；授权门店仍能得到受限下载响应。`registry.validate()` 返回 `[]`，全部本地 JS 语法检查及 `git diff --check` 通过。

**补充修复（受限收信的诊断日志）：** 无法解密的附件在确定其门店编码之前不可安全归属；区长身份原先会在定时/命令行日志里看到附件文件名与解密失败原因。现受限导入只增加跳过计数，不记录原文件名、原因或原件；显式全量的平台入口保留完整排障信息。上报包与目标拆分包均有回归覆盖。收信/范围/报量报告/Web 专项三头各 **432 passed**，Python 3.14 另 **101 subtests passed**；Node JS 语法、注册表校验与 diff 检查通过。

此前关于区长收信范围“等待用户决定”的记录是当时状态，已由本条更新为“范围已决定并落实”。**仍未完成：** 其他业务读/导出/目标门店与系统副作用的逐项审计、剩余非系统页面/handler 归属、`dump/erp-dump/pools` 执行收编、完整隔离浏览器覆盖及 Lifehall 物理裁剪/更新回退验收。权限隔离整体仍未完成；本批未重启服务、打包、提交或推送。

**2026-10-03 补充修复（收信导入默认范围）：** 继续检查收信低层入口时发现，所有正式 Web、CLI、定时调用都会传入已解析的门店范围，但 `report_inbox.run()` 的参数默认值 `None` 同时代表平台全量授权；未来调用者若忘记传身份范围，会静默导入全公司邮件。现在区分“参数未提供”和“明确传入 `None`”：未提供/空范围均拒绝导入，平台全量必须由上层显式授权后传 `None`。TDD 回归先复现未传参数仍导入区外包，并覆盖显式全量仍可导入。收信与范围专项 Python 3.9 **21 passed**；随后三头全量 `tests/`：Python 3.8、3.9 各 **3621 passed**，Python 3.14 **3621 passed、1411 subtests passed**；`registry.validate()` 返回 `[]`，`git diff --check` 与全部 `web/**/*.js` 语法检查通过。

**仍未完成：** 该默认值修复不代表全部业务结果集/目标店及系统副作用审计完成；剩余非系统页面/handler 归属、`dump/erp-dump/pools` 执行收编、完整隔离浏览器覆盖及 Lifehall 物理裁剪/更新回退验收仍待完成。权限隔离整体继续标记为未完成；未重启服务、打包、提交或推送。

**2026-10-03 补充进展（人员页面归属与生命周期）：** “账号与人员”仍留在左下角系统设置入口，并继续按 `FOOT_PAGES` / `FOOT_ROUTE_RULES` 控制可见性和 API 权限；其中人员名单读取、展示、全选/全不选与保存代码从 `web/app.js` 移至 `web/features/store/page.js`。由 account 页注册 `mount/load/unmount`，离页取消未完成的只读请求并解绑事件，人员保存请求保持原有行为。HTML 先加载 store 页面再加载 app，静态资源缓存号同步更新。

验证：前端 IA、人员上报、权限协议专项三头各 **197 passed**，Python 3.14 另 **247 subtests passed**；Node 隔离用例验证 account 生命周期退出会取消 pending GET。最终三头全量：Python 3.8、3.9 各 **3623 passed**，Python 3.14 **3623 passed、1411 subtests passed**；全部 `web/**/*.js` 通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。

**仍未完成：** 人员页面已迁移，但系统操作范围和业务结果/目标店审计仍需收尾；`dump/erp-dump/pools` 执行收编、完整隔离浏览器覆盖及 Lifehall 物理裁剪/更新回退验收仍待完成。权限隔离整体继续标记为未完成；未重启服务、打包、提交或推送。

**2026-10-03 补充进展（人员接口归属与 Lifehall 清单单源）：** `/api/staff` 的 GET/POST/PUT
处理已迁入 `src/features/store/http.py`，门店自己的人员配置读写、保存后五分钟合并上报、区长/平台的收信只读行为均沿用原路由门禁。上报人员表和店码/别名范围过滤归 `features.store.staff.inbox_state()`；测试覆盖伪造店名与店码冲突时拒绝跨店结果。区长/平台只读请求不会顺带解析本机 ERP 配置。公共 `web.py` 只传入当前 scope 与必要服务回调，再统一包装 JSON 响应；业务 handler 不导入 `web.py`。另将运行时 `LIFEHALL_PAGES` 改为复用 `edition.LIFEHALL_PAGES`，避免运行门禁和安装版本各持一份页面清单。

结构复核确认计划表中的 11 个业务页面脚本与 11 个业务 HTTP handler 均已有明确业务目录；人员 handler 是本次补齐的最后一个。检查点 6 的源码迁移项现已完成，隔离浏览器交互验收仍未完成，不能据离线测试宣称真实页面行为已验收。

回归先确认新增结构断言在旧代码下失败，再迁移并验证人员门店范围与本机配置隔离。最终三头全量：Python 3.8、3.9 各 **3627 passed**；Python 3.14 **3627 passed、1411 subtests passed**。人员接口/授权专项 Python 3.14 **18 passed**；全部 30 个本地 JS 文件通过 `node --check`，`registry.validate()` 返回 `[]`，`git diff --check` 通过。未重启服务、未访问现场账号或业务数据、未打包、未提交或推送。

**仍未完成：** 检查点 1 的完整差异基线与检查点 5 的所有业务结果/目标店、导出与系统副作用逐项审计仍需收尾；最后 `dump / erp-dump / pools` 执行层迁移及其 Lifehall 物理裁剪验证仍未完成。全功能隔离浏览器、服务同步重启和更新/回退资源验收仍待安排；权限隔离整体继续标记为未完成。

**2026-10-03 补充验证（Lifehall 物理裁剪后的 dump 路径）：** 新增独立子进程回归，在临时安装目录完整复制 `src/` 后按实际 `edition.PRUNE` 删除文件，再以 `EDITION=lifehall` 启动。确认物理包缺少 `src.pools` 时，玲珑订单抓取成功返回 0，抓取失败按原退出码返回；两种情况均记录尝试状态、均未调用 `cmd_pools`，且进程从未导入被裁模块。Python 3.8、3.9、3.14 各 **1 passed**。这关闭了“生活馆抓取成功后依赖被裁四池模块”的特定风险，不表示三个日常步骤已完成执行层迁移。

**仍未完成：** 检查点 7 的 `dump / erp-dump / pools` 执行实现仍在 CLI，尚未抽到执行模块，`_RUNNERS` 也未删除；全量范围/系统副作用审计、隔离浏览器及 Lifehall 更新回退仍未完成。权限隔离整体仍标记为未完成。

**2026-10-03 本轮复核（区长导入范围与全量回归）：** 用户确认区长导入限制在授权区域门店；现有 `modules.auth.inbox_scope` 已按区长授权门店解析华为店码，并要求同码映射下的别名全部在授权范围内，跨区共用店码、名单读取失败和身份不明均拒绝；显式平台全量身份保持原有能力。HTTP、CLI、定时入口及底层未传范围的拒绝路径均有回归覆盖。本轮三头全量：Python 3.8、3.9 各 **3628 passed**；Python 3.14 **3628 passed、1411 subtests passed**。`registry.validate()` 返回 `[]`，全部 `web/**/*.js` 通过 `node --check`，`git diff --check` 通过。

**仍未完成：** 上述验证确认了区长收信范围和当前工作区回归，不等于全项目权限隔离完成。检查点 1/5 的完整路由与数据副作用审计、检查点 7 的三步执行模块迁移、隔离浏览器及 Lifehall 更新/回退验收继续待完成；未重启现场服务、打包、提交或推送。

**2026-10-04 补充进展（检查点 7：抓取执行入口收编）：** 新增
`src/modules/fetch/execution.py`，将华为 dump、云商双池抓取及通用逐池采集编排迁出 CLI；
`cmd_dump` / `cmd_erp_dump` 仅构造认证与采集服务上下文，`cmd_pools` 的抓取分支调用通用执行器，
比较计算继续交给 `features.compliance.comparison.execution`。注册表的 dump/erp-dump Step.run
直接指向执行模块；日常流程统一从 Step.run 派发，已删除残留的 `_RUNNERS`/CLI 命令名路由。
保留首次建库抓当年、已有库抓当月、会话续期开关、Lifehall 跳过完整版本池、每池单独记尝试、
ERP 各池局部成功语义，以及抓取失败时 fatal 中止后续计算。采集器原有签名不一致，执行器按玲珑与
ERP 采集器分别调用；测试夹具也改为拦截注册入口，避免测试执行真实采集。

检查点 7 的隔离回归：Python 3.14 **562 passed、356 subtests passed**；包含物理裁剪 Lifehall、
runlog、pool history、timer 与 once 注册。最终全量三头并行：Python 3.8 **3637 passed**，
Python 3.9 **3637 passed**，Python 3.14 **3637 passed、1400 subtests passed**。`registry.validate()`
返回 `[]`，所有本地 JS 通过 `node --check`，`git diff --check` 通过。没有调用真实 ERP/玲珑，
没有重启服务、打包、提交或推送。

**仍未完成：** 检查点 1/5 的系统接口、业务结果/目标门店、导出和写入副作用清单与逐项审计仍未签完；
`POST /api/sales-rewrite` 的强制刷新身份范围已向用户询问，尚未收到决定，本批没有调整其授权。
检查点 6 的隔离浏览器验收与检查点 8 的 Lifehall 更新/回退验证也仍未完成；权限隔离整体继续标记为未完成。

**2026-10-04 补充审计（业务结果与目标范围，部分完成）：** 继续逐项核对 comparison、attain、film、benefit、inventory、cashier、claim、plan、distribution 与 store 人员处理链。报量历史的行、计数及年份按授权范围重算；报告读取/删除/下载先验证旁车门店归属和路径；达成当前周/历史由授权行重算，拆分读写/发送先检查目标店；防护膜和权益的汇总由过滤后的门店行重算，明细接口先查目标店；库存仓库列表按 `BranchName` 授权过滤，账面/在途先映射并核对目标仓，导出先校验门店，只有用户确认的表外码索引保留全仓查询；收银再由本机店码及事务内 owner 检查约束数据；领取串号查询、提交和状态修改都必须对应当前可见待领行；月度计划导出沿用已过滤的 `app.plan()`，分销路由只对平台岗开放；人员收信状态按唯一、无冲突的店码别名过滤。

本批未发现需要新增修复的越权路径。范围专项 Python 3.14 **568 passed、165 subtests passed**，覆盖上述业务范围回归及既有路由矩阵。此为代码与离线测试审计，不代表真实账号/业务数据验证。当前检查点 5 仍未签完：系统副作用清单还需最终收尾，强制重写 `/api/sales-rewrite` 的可用身份仍等用户决定；未收到决定前保持其既有授权不变。

**2026-10-04 补充修复（预览模式门禁）：** 新增临时目录合成数据回归，复现公开 `POST /api/setup/preview` 在没有可预览条件时仍能写入标记，并且开启后 `/api/attain` 可读回本机缓存、`/api/overview` 会带出本地报告摘要。现仅当云商身份已就绪且当前缺玲珑授权时允许开启；预览期只开放脱敏总览、登录状态/进入方式与认证接口，其余业务及设置 API 统一拒绝，玲珑授权流程仍可继续。先运行新增回归确认两项均失败，再修复。

专项验证：Python 3.14 **178 passed、15 subtests passed**（登录门禁、进入流程、路由政策、预览和来源校验）；随后三头全量：Python 3.8、3.9 各 **3639 passed**，Python 3.14 **3639 passed、1400 subtests passed**；`registry.validate()` 返回 `[]`，`git diff --check` 通过。测试只使用临时目录与合成数据；未访问真实数据、未重启服务、未做浏览器验收、未打包/提交/推送。

**仍未完成：** 检查点 1/5 的全部系统 API 副作用与业务目标范围审计仍需收尾；`POST /api/sales-rewrite` 的可用身份仍待用户定范围。检查点 6 的隔离浏览器验收、检查点 8 的 Lifehall 裁剪与更新/回退验收也未完成，权限隔离整体继续标记为未完成。

**2026-10-04 补充修复（系统错误上报的数据范围）：** 系统接口审计发现 `/api/report-bug`
虽然声明为本机范围，但诊断包会收集整份运行日志、上一份日志和 `out/*.json` 报告摘要；区长在多店机器上上报时可能把辖区外串号及报告内容一并发出。先以临时目录合成区外串号写失败测试，确认报告摘要会进入区长支持包。

现 Web 区长身份只收集环境、配置和定时诊断；不收集 `run.log`、`run.log.1` 或报告摘要，并在包内说明因授权范围限制而省略。门店单店与平台身份、CLI 默认打包均保留原有业务日志和报告摘要。没有按日志文本猜门店或部分剔除。

Python 3.14 专项 `tests/test_route_policy.py tests/test_web.py tests/test_bugreport.py`：**359 passed、83 subtests passed**。隔离 Lifehall 浏览器用临时目录验证：登录遮罩不出现；设置卡显示已保存编码与玲珑授权；邮件/企微设置隐藏；重新输入编码回到进入页且预填当前编码；云商及旧业务直调为 404，通用配置仅返回 `store_code/timezone`，伪造修改 `store_code` 返回 400 且原值未变；浏览器 console/page errors 与请求失败均为零。未触碰真实账号或业务数据，未重启当前服务。

**仍未完成：** 以上仅关闭 `/api/report-bug` 的区长数据外泄路径，不代表系统副作用审计整体完成；强制刷新 `/api/sales-rewrite` 的可用身份仍待用户定范围。检查点 1/5 其余系统/业务接口审计、完整角色隔离浏览器覆盖，以及检查点 8 的 Lifehall 更新/回退验收仍待完成；权限隔离整体继续标记为未完成。

**2026-10-04 补充修复（启动自检诊断范围）：** 系统接口审计发现公开的 GET /api/boot
会把完整健康快照交给未登录请求，其中含整库数据统计、运行失败原因、通知收件人和内部路径；
受限角色的 /api/overview 也会收到同一份明细。现公开 boot 接口始终只返回自检分组、
状态和通用阻断提示；门店/区长总览同样收窄，平台岗登录后仍可在总览查看完整诊断。
合成敏感标记回归先失败后通过；权限与登录门禁相关专项 Python 3.14 **444 passed、
117 subtests passed**。

**仍未完成：** 本批关闭两条系统诊断旁路，不能据此标记系统 API 审计完成；
POST /api/sales-rewrite 的身份范围仍待用户定夺。其他系统写入副作用、浏览器角色覆盖、
Lifehall 裁剪及更新/回退验收继续待办；权限隔离整体仍未完成。

本批最终复验：Python 3.8 **3644 passed**，Python 3.9 **3644 passed**，Python 3.14 **3644 passed、1400 subtests passed**；`registry.validate()` 返回 `[]`，全部 `web/**/*.js` 通过 Node 语法检查，`git diff --check` 通过。全部使用隔离/合成测试，没有访问真实账号、业务数据或重启现场服务；未打包、提交或推送。

**2026-10-04 补充修复（Windows 定时任务归属与默认目标）：** 系统副作用复核发现，`/api/schedule` 和 UAC 重试可由请求体提供任务名，而 Windows `schtasks /create /f` 会覆盖同名任务；旧 `/run` 与删除接口省略任务名时也会直接操作默认名字。现注册前先核对 Windows 任务列表，枚举失败即拒绝；同名任务必须能证明动作直接指向本机 `run.bat`，或有结构有效的本机历史登记记录，外部/无法确认的任务均不覆盖。任务列表会把仍需人工核对的旧名任务标为“归属未核实”，不能运行、删除、卸载或在替换旧任务时自动删除；省略名称的运行/删除请求也必须在受管清单中找到默认任务。卸载时若任务列表无法枚举，会报告失败并保留登记记录。自有任务可继续改时刻、经明确选择的任务可维护。

先添加同名外部任务、任务动作不可读、任务枚举失败、匿名默认删除/运行、损坏登记记录、旧名任务不可自动删除及卸载时保留记录等回归；确认问题后再修复。定时任务/路由专项三头各 **456 passed**，Python 3.14 另 **79 subtests**；最终三头全量为 Python 3.8、3.9 各 **3658 passed**，Python 3.14 **3658 passed、1400 subtests passed**。全部本地 JS 语法通过，`registry.validate()` 返回 `[]`，`git diff --check` 通过。只用模拟任务名及合成路径，没有创建/修改真实 Windows 计划任务、重启服务、访问真实账号或业务数据；未打包、提交或推送。

**仍未完成：** 系统副作用与业务数据范围的其余逐项审计（下一批继续核对开机自启任务的覆盖/删除归属）、`/api/sales-rewrite` 的身份范围决定、完整隔离浏览器与 Lifehall 更新/回退验收仍待完成；权限隔离整体继续标记为未完成。

**2026-10-04 补充修复（开机自启动作归属）：** 用户确认 ERP 重抓涉及公司全量数据，强制刷新继续允许授权店、区长、平台岗操作各自设备上的销售库；`/api/sales-rewrite` 原有三类身份声明保持不变。

系统副作用审计发现，Windows 开机自启会对固定名称直接执行 `schtasks /create /f`、按名称删除计划任务及 Run 项；`schedule.remove_all()` 也会按自启任务名称删除。现安装/删除前区分“确实不存在”与“列表或动作不可读”，并核对任务动作指向当前安装目录的 `boot.py` 或内容匹配的 `boot-run.bat`；Run 项也核对命令目标。外部同名项或归属未知时拒绝覆盖、删除及并排添加另一种启动方式。任务不存在时创建不带 `/f`，并在降级为 Run 项前复查是否出现同名任务。状态页对归属不明的启动项显示提示；批量清理只删除核实归属的自启任务。

新增合成回归覆盖同名外部任务/Run 项、详情不可读、普通与提权注册、删除保护、任务创建竞态防覆盖及批量卸载。自启/定时任务/路由专项 **Python 3.14：191 passed**；最终三头全量为 Python 3.8、3.9 各 **3671 passed**，Python 3.14 **3671 passed、1400 subtests passed**。`web/app.js` 通过 Node 语法检查，`git diff --check` 通过。只用模拟任务和临时路径；没有读写真实 Windows 任务、真实 Run 项、账号或业务数据，也未重启服务、打包、提交或推送。

**仍未完成：** 其他系统写入副作用与业务数据范围仍需逐项审计；检查点 6 的完整角色隔离浏览器验收及检查点 8 的 Lifehall 更新/回退验收未完成，权限隔离整体仍标记为未完成。目前没有新的业务口径需要用户定夺。

## 执行结项（2026-10-04）

本节覆盖上文各批次“仍未完成”的阶段性描述。检查点 1–8 的实现与本地验收均已结束；唯一运行环境限制是本机没有运行中的 8787 服务，因此没有 live-server 重启/验收。本地服务没有被新启动，以免触发真实定时工作。

最终复核记录：

* 范围盘点：8 个业务一级模块、27 个页面键；50 条业务 API 路由、58 条系统/foot 路由、19 条登录入口路由；另有共享导出下载路由与请求体选页路由各 1 条，分类互斥规则由路由注册测试覆盖。业务页和 handler 均归属显式业务目录，定时步骤 `Step.run` 已完全接通且无 `_RUNNERS`。
* 权限矩阵：store / manager / platform × audience × 页面操作与目标店范围有注册声明及逐路由/972 组合门禁覆盖；生活馆既支持 full 版选择入口，也支持 lifehall 安装，按本机门店编码进入，旧 ERP 身份不能越界。区长上报导入限制为授权区域门店；整公司 ERP 重抓、三种身份权益领取提交、库存唯一全仓串号索引均按用户决定保持。
* 最终源码冻结测试：`.dsh/tasks/venv38/bin/python -m pytest tests/ -q` → **3675 passed**；`venv39` → **3675 passed**；`venv314` → **3675 passed、1400 subtests passed**。
* 浏览器与安装模拟：四身份隔离浏览器遍历各自可见页面/操作，覆盖生活馆编码直达、ERP 登录卡和平台确认，无失败请求与前端错误。临时安装根目录完成更新/回退覆盖检查，配置、秘密与历史哨兵均保留。以上不代表真实账号或门店数据验收。
* 其他检查：`registry.validate()` = `[]`；30 个 `web/**/*.js` 均通过 `node --check`；`git diff --check` 通过。
* 当前工作区在 `main`，共 132 行 `git status --short --branch` 输出（含标题行），tracked diff 汇总 **95 files changed, 5983 insertions(+), 8496 deletions(-)**；其中包含原有未提交用户工作和本次功能改动。已确认 `sn_trace` 删除继续保留，`src/version.py`、`VERSION` 与 build/publish 脚本没有工作区状态项。未提交、推送或打包。

最后再次确认：隔离浏览器与临时安装夹具只使用模拟 API、临时数据及本地静态资源；没有登录真实 ERP/玲珑账号、重抓生产数据、发送通知、改动用户的 `in/`、`out/` 或 `.secrets/`。计划和开发目标中的旧阶段说明保留为历史，后续以本结项记录为准。
