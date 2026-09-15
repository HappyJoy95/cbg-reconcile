# AGENTS.md — cbg-reconcile

给接手这个项目的 AI / 开发者。**先读这一页**，再去翻别的。

## 这是什么

比对「云商（盛联 ERP）里卖出的、归属本店的串号」有没有都报量给华为（CBG）。
没报的列成差异清单，推邮件 / 企业微信。装在**门店自己的 Windows 电脑**上，
每天定时跑一次。

## 先跑起来

```bash
python -m pytest tests/ -q          # 544 条，约 20 秒。改完必须全绿
python bootstrap.py selftest        # 逐项自检（版本 / 门店 / 依赖 / 会话 / 服务）
python -m src.cli serve             # 起控制台 → http://127.0.0.1:8787
```

依赖：`requests` / `pyyaml` / `openpyxl`（见 `requirements.txt`）。
代码要求 **Python 3.9+**（开发机 3.9、门店 3.14，两头都要能跑 ——
所以别用 `match`、别写 `dict | dict`、注解用 `Optional[...]` 而不是 `X | None`）。

## 目录

```
src/cli.py         命令行入口 + 所有 cmd_* 子命令（auth/ping/check/serve/selftest…）
src/web.py         控制台后端（HTTP + JSON API，无框架）与 App/Handler
web/app.js         控制台前端（原生 JS，**无构建步骤**，改完刷新即可）
web/index.html     页面骨架（id 要和 app.js 里 $('#xxx') 对得上）
src/browser.py     自动抓华为会话（CDP 读 cookie + localStorage 取 csrf）
src/cbg.py         华为 CBG 接口客户端（订单列表、门店详情、ping）
src/erp.py         云商 ERP 客户端（销售明细、登录换 token、验证码）
src/reconcile.py   对账核心：两边串号做差集
src/report.py      差异清单落盘（xlsx + json）
src/selfupdate.py  自更新 / 历史版本回退 / 版本检查
src/schedule.py    计划任务（schtasks / crontab）
src/autostart.py   开机自启（计划任务 HighestAvailable / 注册表 Run）
src/winutil.py     schtasks 的两个坑（输出编码、字段本地化）集中在这里
bootstrap.py       所有 .bat 的统一入口（**纯标准库**，装依赖前就能跑）
tests/             544 条单元测试（pytest）
tools/build_package.sh  打发布包（见下）
运维手册.md         完整手册（部署/维护用，**641 行，不发门店**）
门店操作手册.md     发门店的精简版（五六步，打包时进包的是这份）
update-debug.py    更新失败时的现场诊断脚本
```

## 发版

```bash
# 1. 改 src/version.py 里的 VERSION
# 2. commit message 必须是这个格式（历史版本列表靠它筛）：
#      release: v1.4.11
# 3. bash tools/build_package.sh          # 正式包 → dist/
#    bash tools/build_package.sh beta     # 测试包（名字/BUILD.txt/发布说明都带 beta）
```

### 正确顺序：先升 VERSION，再打 beta 包

```
改代码 → 升 VERSION → 打 beta 包试 → 试好了 → push → 打正式包
```

⚠ **beta 包和正式包的版本号同源** —— 它表示"**这一版正在测**"，
不是"另一个版本"。所以 `beta` 只是给**同一个版本**加个标记，它本身不改版本号：

* 想打 `v1.4.11-beta`，前提是 `VERSION` 已经是 `1.4.11`；
* 如果没升版就打（比如 `VERSION` 还是 `1.4.10`），会出来 `v1.4.10-beta` ——
  看着像"1.4.10 的补丁"，很别扭（**真这么干过，被用户指出**）。

"还没到发版时机"的正确表达方式是：**升了 `VERSION`，但没 push** ——
线上版本号不变，门店就看不到"有新版本"，而 beta 包名字是对的。

* 正式包里**不放**给开发者看的文档，只放 `门店操作手册.md` + `发布说明.md`。
  自检会拦 `README.md` / `设计文档.md` / `运维手册.md` / `AGENTS.md`。
* 门店靠**自更新**升级（读 `main` 的 `src/version.py` 比版本号 → 下 zip 覆盖代码），
  所以**推上去 ≠ 发版**：得让远程 `VERSION` 变大，门店才看得到"有新版本"。

### ⚠ git 仓库 ≠ 包内容 —— 两个维度，别混为一谈

**仓库是整个项目，包只是门店电脑上要跑的那个子集。** 每次发版前先想清楚
"这次门店会拿到什么"，因为**不是每个 commit 对门店都有意义**。

| | 进包 | |
|---|---|---|
| `src/` `web/` `tests/` `config/` | ✅ | 代码、前端、测试、门店映射表 |
| 根目录那堆 `*.bat` / `*.py` | ✅ | 门店要双击的 |
| `门店操作手册.md` `发布说明.md` | ✅ | 门店要看的 |
| `.secrets/erp.env` `out/` | ✅ | **空模板 / 空目录**（真凭据永不进包） |
| `tools/build_package.sh` | ❌ | 打包工具，门店不打包 |
| `AGENTS.md` `README.md` `设计文档.md` `运维手册.md` | ❌ | 给开发者 / AI 的 |
| `.git/` `.gitignore` `dist/` `.pytest_cache/` | ❌ | 仓库自身的东西 |

举例（v1.4.10 → v1.4.11）：仓库层面改了打包脚本、加了 AGENTS.md、手册改名，
**但包内的实质改动只有文档**（641 行的指南 → 131 行的门店操作手册），
**代码一行没变**。所以"改了三个东西"和"门店升级后有什么不同"是两件事。

推论：
* 纯仓库改动（改打包脚本、写文档）**不必为它单独发版** —— 门店零感知；
* 发版的 commit 里如果混着仓库改动，写发布说明时只讲**进包的那部分**；
* 打包自检会拦 `README.md` / `设计文档.md` / `运维手册.md` / `AGENTS.md` ——
  它们不该出现在包里（`rsync` 排除 + 反查断言，两道）。
* **自更新的结果和包一致**：`selfupdate` 是"照仓库原样铺"，但排除了
  `NEVER_TOUCH`（`config/` `.secrets/` `out/` `dist/` `tools/`）。
  所以 zip 装出来的目录和自更新之后的目录，文件集合是一样的。

## 改动的规矩（用户明确要求过）

* **改完一条先别急着 push、别急着升版本号。** 攒着，等用户说"推吧"。
  要给别人测就先打 **beta 包**。
* 提交信息写清**为什么**（这个项目的注释和提交信息都是"记录踩过的坑"风格，
  请保持）。中文。

## 六个踩过的坑（都真踩过，别再踩）

**1. 路径比较别用 `str(Path)` —— Windows 上是反斜杠**

```python
str(Path("src") / "cli.py")   # Windows → 'src\\cli.py'，POSIX → 'src/cli.py'
```

拿它和写死的 `"src/cli.py"` 比，**在 Windows 上永远不相等**。
后果：自更新被判"这不像我们的包"而拒绝（门店连卡三次），
以及 `ALLOW_EVEN_IF_NEVER` 失效 → 门店映射表永远更新不下去。
→ 统一走 `selfupdate._rel_key()`；测试里用 `PureWindowsPath` 钉（`TestRelKey`）。

**2. 启动路径上的输出不能有失败模式**

后台服务由 `pythonw.exe` 起（**没有控制台**），或 stdout 被重定向时，
Python 按 locale 编码写输出（中文 Windows 是 GBK）。这时打印 `⚠`（U+26A0）
会抛 `UnicodeEncodeError` —— 而它在 `serve_forever()` **前面**，一抛服务就起不来。
→ `cli._harden_stdout()` 把 stdout 配成 `errors="replace"`；
启动路径上的 print 全部包 try。**别在启动路径用 emoji。**

**3. 前端表格放 HTML 必须包 `{html: ...}`**

`table()` 对**字符串**单元格默认 `esc()`。传带 `<b>` 的字符串 → 页面上原样显示
`<b>v1.4.6</b>`。这个坑踩过**三次**。
→ `{ html: ... }`；`TestFrontendWiring` 里有两条测试盯着。

**4. 检查更新/下载都要走 `api.github.com`，别用 raw / codeload**

`raw.githubusercontent.com` 有 ~5 分钟 CDN 缓存（加时间戳参数没用），
刚发的版本它还在返回旧的；`codeload` 会发**缓存的旧 zip**。
→ 版本读 `api.github.com/.../contents`，zip 下 `api.github.com/.../zipball`
（`_zip_urls()` / `_version_api_url()`），另一个源留作退路。

**5. 自更新只许碰代码**

`config/`（门店配置）、`.secrets/`（云商账号、华为会话、浏览器登录态）、
`out/`（历史报告）**一根手指都不许碰**。唯一的文件级例外是
`config/stores.yaml`（随程序走的门店映射表）。
→ 改 `selfupdate` 时务必跑 `TestWhitelist` / `test_rollback_only_touches_code`。

**6. 门店电脑上别假设有 git**

自更新走"下 zip 覆盖文件"，不调 git（`selfupdate` 顶部写了原因）。
`.bat` 必须**纯 ASCII + CRLF**（打包脚本会断言），中文一律由 Python 打印。

## 数据与凭据（别提交）

`.gitignore` 已排除：`.secrets/`、`config/store-*.local.yaml`、`out/`、`dist/`、
`BUILD.txt`、`run*.bat`（安装时按本机 Python 路径生成）。

优先级别搞错：云商凭据 **环境变量 > `.secrets/erp.env` > `~/.dsh/secrets/erp.env`**。
华为会话按店存：`.secrets/cbg-<门店码>.json` —— **换店等于换一份会话**，
要用那个店的账号重新抓一次（否则"登得上、抓不到"）。
