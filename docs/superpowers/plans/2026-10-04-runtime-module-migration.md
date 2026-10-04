# 运行支撑模块目录迁移执行计划

> 依据：已获用户确认的 [设计文档](../specs/2026-10-04-runtime-module-migration-design.md)。
> 执行基线：当前 `main` 工作区已有大量未提交改动；所有改动均保留，不回滚、不重置。
> 目标是移动实现、维持旧导入路径与运行语义；不改业务口径、权限、API、数据或外部调用行为。

## 开始条件与硬边界

- 当前源码版本为 `26.1002.145826`；本轮不修改 `src/version.py`，也不打正式包或 beta 包。
- 先用兼容契约测试确认旧路径失效；专项测试和最终三头测试都在不含 `.secrets/`、`out/`、`in/`、门店配置的临时源码副本中运行，避免测试自身误写本机数据。
- 以 Python 3.8 可解析和运行作为最低线；不新增第三方依赖，不改根目录 `bootstrap.py`、`.bat` 的入口路径。
- `src/edition.py::PRUNE` 仍是唯一裁剪源；`PRUNE_ENABLED` 保持 `False`。自更新文件白名单、配置及 `.secrets/`、`out/`、`in/` 不得因迁移而变。
- 三阶段分别完成兼容、裁剪和聚焦测试后再进入下一阶段。全量三解释器测试只在源码冻结后并行运行。
- 不提交、不推送、不发版、不产出安装包；临时目录/内存中的包文件集检查只用于测试。

## 实施任务

### 0. 记录基线与导入契约

1. 在 `tests/test_module_layout.py` 增加迁移契约测试，先断言新路径存在、旧路径与新路径是同一个模块对象，并验证代表性的模块级 monkeypatch 可由两条路径观察到。
2. 记录当前测试、导入依赖、`src.web` 的公开符号和被测试补丁的模块级名称；重点覆盖 `App`、`Handler`、`serve`、`ROOT`、路由规则、ERP/CBG 客户端、timer/runner 入口。
3. 在没有模块的新路径时运行新测试并确认它按预期失败；不得用跳过或放宽断言掩盖失败。

### 1. 阶段 A：`src/integrations/`

迁移实现：

```text
src/integrations/__init__.py
src/integrations/{erp,erp_stub,cbg,browser,cdp,tdoc,pmall,mailer,wecom}.py
```

兼容入口保留：

```text
src/{erp,erp_stub,cbg,browser,cdp,tdoc,pmall,mailer,wecom}.py
```

步骤：

1. 新建显式包，不在 `__init__.py` 中自动导入客户端；先搬 `cdp.py`、`cbg.py` 等相对独立模块，调整包内相对导入并保留显式公开符号。
2. 对每个旧模块用薄兼容入口将 `sys.modules[旧名]` 指向新模块对象；用对象身份测试确认 `import src.erp`、`import src.integrations.erp` 及 `src.erp` 包属性一致。补丁 `src.erp.ErpClient` 后从新路径访问也须看到替身。
3. 按依赖顺序搬 browser/CDP、ERP/CBG、TDOC/PMall、mailer/WeCom；保留 Lifehall 所需的 CBG/browser 路径。
4. 更新全部旧路径生产导入为新路径或由现有系统模块门面调用；测试、外部兼容使用的旧路径继续有效。不要顺带重排系统模块职责。
5. 在 `src/edition.py::PRUNE` 增加 `src/integrations/erp.py`、`src/integrations/erp_stub.py` 和两个根兼容入口的精确裁剪项；确保 Lifehall 的裁剪后入口不会导入 ERP 实现，完整版本保留实现。
6. 增加/更新 `tests/test_module_layout.py`、`tests/test_edition.py`、`tests/test_edition_update.py` 和自更新白名单/临时安装测试：验证路径集、裁剪树可导入、旧路径可用、`config/` `.secrets/` `out/` `in/` 不受影响。
7. 运行 integrations、ERP/CBG/browser/通知、edition 与 self-update 相关专项测试；检查迁移后的导入图无环。

阶段 A 完成条件：新旧路径同对象；ERP 实现及旧入口在 Lifehall 裁掉且裁剪树仍可导入；完整版实现不缺；相关专项测试通过。

### 2. 阶段 B：`src/desktop/`

迁移实现：

```text
src/desktop/__init__.py
src/desktop/{autostart,elevate,runtime,schedule,service,winutil,runner}.py
```

根兼容入口保留上述七个 `src/<module>.py`。

步骤：

1. 先为七组新旧路径补模块身份与 monkeypatch 回归测试，尤其覆盖 `schedule` ↔ `autostart` ↔ `runtime` ↔ `winutil` 循环/延迟导入以及 `runner.manager` 单例身份。
2. 按相依顺序移动底层 `winutil`、`runtime`、`elevate`，再移动 `autostart`、`schedule`、`service`、`runner`；用显式相对导入处理新包位置，延迟导入保持延迟。
3. 兼容入口统一指向同一实现模块对象；不复制函数、常量、单例或可变配置。
4. 更新 `src/modules/timer`、`src/modules/health`、`src/modules/auth`、`src/cli.py`、`src/bugreport.py` 等内部调用点；根入口 `bootstrap.py` 保持经旧兼容路径惰性导入桌面模块（它是无需安装依赖即可运行的公共入口，也是既有测试替换点）。
5. 扩展布局规则和 `tests/test_autostart.py`、`tests/test_elevate.py`、`tests/test_runtime.py`、`tests/test_schedule.py`、`tests/test_service.py`、`tests/test_bootstrap.py`、`tests/test_run_daily.py`：验证 Run 项/任务名/命令行/权限语义不变、桩不启动 Windows 真任务。
6. 验证 Lifehall 与 full 都保留桌面支撑文件，包内导入图不依赖仓库外文件。

阶段 B 完成条件：旧模块对象与新模块一致；bootstrap、timer、health、CLI、服务和调度专项测试通过；普通权限、按需提权和 Windows 7/Python 3.8 约束未变。

### 3. 阶段 C：`src/http/` 与 `src.web`

目标包按职责组织，保持标准库 HTTP 服务：

```text
src/http/__init__.py       # 公开兼容命名空间与补丁转发
src/http/policy.py         # 页面可见性、操作权限表的注册表派生逻辑
src/http/app.py            # App、Handler、API dispatch、serve 与共享运行时命名空间
src/web.py                 # 旧路径兼容入口，指向相同模块对象
```

步骤：

1. 在改动前补齐 HTTP 契约测试：`src.web` 与新公开包对象身份、`App`/`Handler`/`serve`/`ROOT`/规则表可访问、模块级补丁有效；覆盖 loopback 默认、Host/Origin、setup/runtime 门禁、权限 guard、未知路由状态码和 Handler 生命周期。
2. 对照 `tests/test_web.py`、`tests/test_route_policy.py`、`tests/test_web_origin.py`、`tests/test_setup_gate.py`、`tests/test_runtime_guard.py`、`tests/test_edition_registry.py` 的补丁点，建立符号归属表。任何拆分都必须保证 `src.web.<symbol>` 与 `src.http.<symbol>` 是同一对象/可变对象，且旧测试替身仍被运行代码读取。
3. 先整体移动实现到包的兼容核心并让 `src.web` 指向同一模块对象；解决相对导入变化后跑 HTTP 专项测试，确保行为在“仅搬动”这一步无变化。
4. 把页面可见性和操作权限表派生逻辑抽入 `policy.py`；策略不依赖 `App`、`Handler` 或旧 `src.web`。`Handler` 与 `serve` 暂留 `app.py`：现有调用方和测试会通过 `src.web` 替换大量模块级依赖，且启动/路由测试直接检查 `serve` 与 `_api_unlocked` 的实现；拆开它们需要跨模块代理或改变补丁契约。此边界避免运行语义漂移，后续只有在另立兼容方案时再拆。
5. 业务 handlers 仍在 `src/features/**/http.py`；验证公共 HTTP 包不复制它们的业务逻辑，且功能侧只经路由注册/系统门面接入。
6. 更新模块布局依赖方向测试，确认根层 `src.web` 已成为兼容入口，CLI/feature handler/importer 无环；完整 Lifehall 裁剪树仍能加载公共控制台。
7. 运行 Web、权限、Edition、CLI 启动与相关 runtime 专项测试；确认状态码、响应字段和安全门禁逐项不变。

阶段 C 完成条件：`src.web` 继续保持兼容且与新公开命名空间同对象；HTTP 实现和独立策略位于 `src/http/`；HTTP 与功能 handler 无循环依赖；相关行为与门禁测试通过。

### 4. 冻结验证与交付检查

1. 停止所有源码编辑，确认最终 diff 仅包含本计划范围及其必要测试/文档；确认原有脏改动均仍在。
2. 在隔离源码副本中并行运行三套全量测试：

   ```bash
   .dsh/tasks/venv38/bin/python -m pytest tests/ -q
   .dsh/tasks/venv39/bin/python -m pytest tests/ -q
   .dsh/tasks/venv314/bin/python -m pytest tests/ -q
   ```

3. 运行注册表校验、全部 `web/**/*.js` 的语法检查和 `git diff --check`；只读复核 `src/version.py`、`PRUNE_ENABLED`、包文件规则、敏感目录保护未被意外修改。
4. 若任一解释器失败，先定位并修复，再重新冻结源码并重跑受影响测试；三头全量测试期间不编辑源码。
5. 汇报实际搬迁文件、保留的兼容行为、三头测试结果、工作区仍有的既有改动以及未做的门店现场验证。结束时不提交、不推送、不打包。

## 失败处理

- 任何身份测试、monkeypatch 测试或 Lifehall 裁剪树失败，先停在当前阶段，用最小复现定位 import/sys.modules/包裁剪原因，不进入后续阶段。
- 如果某模块无法在不改变公开补丁契约的情况下拆开，维持它在 `src/http/app.py` 或相应包内，记录保留原因；不以大量动态代理或重复实现换取文件拆分。
- 不清理既有未提交改动，也不以“基线已脏”为由删除失败测试或降低断言。

## 本次验收记录

- Python 3.8、3.9、3.14 全量测试均通过：每头 `3679 passed`；3.14 另有 `1417 subtests passed`。
- 全量测试在三个互相隔离的临时源码副本中运行。副本不含 `.secrets/`、`out/`、`in/` 和门店私有配置；仅为需要配置的测试生成合成区长名单与人员缓存，未读取凭据或联系 ERP/CBG。
- 三头 `compileall`、所有 `web/**/*.js` 的 `node --check`、`git diff --check` 均通过；`src/version.py` 无差异。
- `Handler`、`serve` 仍在 `src/http/app.py` 的原因见阶段 C 第 4 步；这项搬迁不做门店 Windows 现场验收。
