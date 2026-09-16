# AGENTS.md — cbg-reconcile

给接手这个项目的 AI / 开发者。**先读这一页**，再去翻别的。

## 这是什么

比对「云商（盛联 ERP）里卖出的、归属本店的串号」有没有都报量给华为（CBG）。
没报的列成差异清单，推邮件 / 企业微信。装在**门店自己的 Windows 电脑**上，
每天定时跑一次。

## 先跑起来

**全程不需要管理员权限**（装、跑、开机自启都不需要；唯一可选提权的是
`bootstrap.py autostart --elevated`，而那条路会让抓会话失败，见坑 7）。

```bash
python -m pytest tests/ -q          # 708 条，约 27 秒。改完必须全绿
python bootstrap.py selftest        # 逐项自检（版本 / 门店 / 依赖 / 会话 / 服务）
python -m src.cli serve             # 起控制台 → http://127.0.0.1:8787
```

依赖：`requests` / `pyyaml` / `openpyxl`（见 `requirements.txt`）。
代码要求 **Python 3.8+**（三头都要能跑 —— 门店 Win7 老机器只能 3.8.10、
开发机 3.9、门店新机器 3.14。所以别用 `match`、别写 `dict | dict`、
别写运行期求值的 `list[str]`，注解用 `Optional[...]` 而不是 `X | None`）。

> **为什么底线是 3.8 而不是 3.9**：3.9 起 CPython 依赖
> `api-ms-win-core-path-l1-1-0.dll`，**Windows 7 上没有这个 DLL，安装包直接起不来**
> （bugs.python.org/issue40740）。3.8.10 是最后一个支持 Win7 的版本，
> 门店还有 Win7 老电脑，卡在 3.9 就等于把那台机器判死刑。
> `bootstrap.MIN_PYTHON` 和 `tests/test_bootstrap.py::test_min_version_is_3_8`
> 两头钉着这个数 —— 别"顺手"提回去。

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
src/schedule.py    计划任务（schtasks / crontab）+ 自己记的注册参数
                   （`.secrets/schedule.json`，见坑 7 第 3 条）
src/autostart.py   开机自启（**默认注册表 Run·普通权限**；计划任务那条要显式开）
src/winutil.py     schtasks 的两个坑（输出编码、字段本地化）集中在这里
src/runtime.py     记住"安装时用的是哪个 Python"（多 Python 机器不装错）
src/elevate.py     按需提权：只把"删旧任务/建定时任务"那一步弹一次 UAC
bootstrap.py       所有 .bat 的统一入口（**纯标准库**，装依赖前就能跑）
tests/             708 条单元测试（pytest）
tools/build_package.sh  打发布包（见下）
运维手册.md         完整手册（部署/维护用，**不发门店**）
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
| `src/store-config.default.yaml` | ✅ | **门店配置模板**（三行是空的） |
| `.secrets/` `out/` `config/store-*.yaml` | ❌ | **这台电脑自己的东西**，安装时按需生成 |
| `tools/build_package.sh` | ❌ | 打包工具，门店不打包 |
| `AGENTS.md` `README.md` `设计文档.md` `运维手册.md` | ❌ | 给开发者 / AI 的 |
| `.git/` `.gitignore` `dist/` `.pytest_cache/` | ❌ | 仓库自身的东西 |

⚠ **包里一个会覆盖门店设置的文件都没有**（`.secrets/`、`out/`、
`config/store-*.yaml` 三样都不进包，由 `bootstrap.ensure_layout()` 按需生成）。
所以**升级时整个目录拷过去覆盖就行**，不需要"记得跳过某几个目录" ——
那种要人记住的规矩迟早出错。改打包脚本时务必守住这条。

举例（v1.4.10 → v1.4.11）：仓库层面改了打包脚本、加了 AGENTS.md、手册改名，
**但包内的实质改动只有文档**（一本几百行的完整手册 → 一份一百来行的门店操作手册），
**代码一行没变**。所以"改了三个东西"和"门店升级后有什么不同"是两件事。

推论：
* 纯仓库改动（改打包脚本、写文档）**不必为它单独发版** —— 门店零感知；
* 发版的 commit 里如果混着仓库改动，写发布说明时只讲**进包的那部分**；
* 打包自检会拦 `README.md` / `设计文档.md` / `运维手册.md` / `AGENTS.md` ——
  它们不该出现在包里（`rsync` 排除 + 反查断言，两道）。
* ⚠ **别以为"自更新之后的目录 = 包里的目录"** —— 这两套**排除规则不一样**，
  实际差三个文件（2026-09-16 实测比出来的）：

  | | 自更新（照仓库铺） | 正式包 |
  |---|---|---|
  | `AGENTS.md` `README.md` `运维手册.md` | **会给**（只排除 `NEVER_TOUCH`） | **不给**（打包脚本单独排除） |
  | `发布说明.md` | **不给**（它根本不在仓库里） | 给 |
  | `BUILD.txt` | 自己写一份（`selfupdate` 里） | 打包时写 |

  影响：**升级过的门店目录里会多出三本开发者文档**，而 `发布说明.md`
  会一直停在当初拷包那一版（它不在 git 里，自更新拿不到新的）。
  想彻底对齐就得二选一 —— 要么把三本文档也加进 `NEVER_TOUCH`，
  要么把它们纳入包。**现在是有意留着的**（门店目录里多几份文档无害，
  真出问题时反而能就近查），但**别说成"两边一致"**。

## 改动的规矩（用户明确要求过）

* **改完一条先别急着 push、别急着升版本号。** 攒着，等用户说"推吧"。
  要给别人测就先打 **beta 包**。
* 提交信息写清**为什么**（这个项目的注释和提交信息都是"记录踩过的坑"风格，
  请保持）。中文。

## 九个踩过的坑（都真踩过，别再踩）

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

**7. 默认别用管理员权限跑 —— 它会把「自动抓会话」弄坏**

**装、跑、开机自启，全程都不需要管理员**，一次 UAC 都不用弹（2026-09-16 起）。
门店装机时那次 UAC 早先用来注册"以管理员身份启动"的计划任务，**已取消**。

* **代价换不来任何东西**：每天那条定时对账任务的 `schtasks /create` **不带 `/rl`**，
  本来就是普通权限跑的；程序目录在 `D:\cbg-reconcile`，普通用户就能写。
  唯一需要管理员的是"注册那个提权任务本身" —— 纯自我循环。
* **而它会把抓会话弄死**：服务是管理员 → 它拉起的 Edge / Chrome 也是管理员 →
  浏览器（Chrome 138 起明确禁止）拒绝以管理员运行，进程把命令行交棒出去就自己退 0，
  我们给的 `--user-data-dir` / `--remote-debugging-port` 落不到活着的实例上。
  实测症状：报「Edge 启动后立刻退出（退出码 0）」，**链接跑到了用户原来那个浏览器里**。
* **`elevated=None` 不许再"猜当前进程"**（`_win_install` 里曾经是 `is_elevated()`）——
  从提权进程里调就静默注册成管理员模式，门店完全无感。现在 `None` = 普通权限。
* 报错要**给证据**：写「**已确认**：`IsUserAnAdmin()` 返回真」，不要写
  "可能的原因（按可能性排）" —— 后者会让人以为在猜，然后去试别的、白折腾一轮。
* 换回普通权限时，**旧的提权任务删不掉必须报出来**（`_drop_task()` 的返回值以前被丢掉）：
  任务留着的话，开机照样以管理员拉起服务，界面写着"普通权限"、报错说是"管理员"。
* ⚠ **光"报出来"还不够** —— 这条真的又坑了用户一轮：升级到普通权限后，那条旧任务
  因为删不掉一直留着，于是**三个症状同时出现**：① 怎么启动都提示管理员；
  ② 抓会话还是失败；③ 连"每天定时对账"也注册不了（旧的是管理员建的，
  普通权限 `/f` 覆盖不了）。所以光提示不行，得让用户**能一键做掉**。
* **按需提权**（`src/elevate.py`）：这两件事只有管理员能做，而它们都是一次性的 ——
  所以只把**那一步**弹一次 UAC 重跑（`ShellExecuteW(runas)` + 结果写文件回传），
  **不要**让用户"右键 install.bat 以管理员身份运行"，那会把 `pip install` 一起提权跑掉。
  界面上是「以管理员身份修复」按钮（只在 `mode == 'task'` 时露出来）和定时任务
  失败后的「以管理员身份重试」。
* ⚠ 提权要用**带控制台的 `python.exe`**，不能用 `sys.executable`（服务是 `pythonw.exe`
  起的，没控制台）—— 否则用户点完 UAC 什么都看不到。见 `elevate.console_python()`。
* ⚠ **有一种情况我们改代码也救不了**：那台电脑登录的是**内置 Administrator
  账户**（RID 500），它默认**不受 UAC 管**（`FilterAdministratorToken` 默认 0）——
  启动的**每个**进程都是管理员，**永远不弹授权框**。⚠ 把 UAC 滑块拉到最高
  **对它没用**（那是给普通管理员账户的另一条设置）。用户真为此白试过好几轮。
  → `elevate.always_admin_reason()` 把它查出来（读 `EnableLUA` /
  `FilterAdministratorToken` + 账户名），界面上**别再说"改成普通权限"**，
  直接给两条解法（换个普通用户账户 / 把内置 Administrator 纳入 UAC 管 + 重启）。
* ⚠ **提权 `/create` 出来的任务，普通权限以后连读都读不到**（踩过）：
  任务所有者是 `Administrators`，而服务跑在**过滤令牌**下 →
  `schtasks /query /tn <名> /xml` 被拒 → 界面上「定时执行」只剩一个名字、
  时间和命令全空。用户看到的就是**"没有管理员权限就看不到定时任务的设置"**。
* ⚠ **"提权只用来删、建还是普通权限建"也被实测推翻了**（2026-09-16，同一个坑的第二幕）：
  门店那台机器上**普通权限 `/create` 直接报 `错误: 拒绝访问。`**；
  界面上「以管理员身份重试」当时写的是"先提权删、再普通权限建"，
  而旧任务压根不在列表里（读不到）→ **短路成"不提权"，点了毫无反应**。
  两种成因（旧同名任务覆盖不了 / 账户被 UAC 过滤后压根建不了）都只有提权能解。
  → 现在的规矩，三条一起看：
  1. **首选仍是普通权限建**（`/api/schedule` POST）—— 成了就不弹 UAC，
     任务归当前用户，以后读/改/删都不用管理员。多数机器到这就够了。
  2. **失败了才提权**，而且提权那一步**直接跑 `schedule-install`**
     （`/create` 自带 `/f`，旧同名任务一并覆盖）。`schedule-remove` 单独留着，
     给"删管理员建的任务"用。
  3. **提权建就必须自己记一份注册参数**（`.secrets/schedule.json`，
     `schedule._remember` / `_recall`）—— 之后普通权限读不到详情，
     界面上的时间和命令全靠这份记录。`_win_task_info` 收 `record` 参数，
     有记录就填上并标 `detail_source="record"`。
     **没有这份记录就别提权建**，否则又回到"看不到设置"。
     记录放 `.secrets/`（selfupdate 的 `NEVER_TOUCH`），删任务时一并抹掉（`_forget`）。
  `_win_task_info` 里另有 `unreadable` 标记把"读不到"和"没有"分开 ——
  有记录时**不算读不到**（`detail_source` 会说清是记录来的）。
* ⚠ 判断系统别 patch 全局 `os.name`：`mock.patch.object(os, "name", "nt")` 会让
  `ctypes/__init__.py` 也以为在 Windows 上 → macOS 上 `import ctypes` 直接 ImportError。
  用 `elevate.is_windows()` 这个可 patch 的小函数。

**8. 版本门槛和版本提示，两处都踩过**

* **门槛不能写 3.9**：Win7 只能装 3.8.10（见开头那段）。
* **`bootstrap.py` 里 `list[str]` 这类运行期注解会要命**：它让"版本太旧"那段
  友好提示**在任何能触发它的解释器上都到不了** —— 3.8 及以下会先在 `def` 那行
  `TypeError`，用户看到的是一屏英文 traceback。测试也抓不到（测试跑在 3.9+ 上）。
  → `bootstrap.py` 顶部有 `from __future__ import annotations`，**别删**。
* **别在提示里写死版本号**：原先四个 `.bat` 都写"装 Python 3.14"，
  而 Win7 门店照着做会卡在安装包报错上。→ `bootstrap.needs_python_help()`
  按 `is_win7()` 分开说；bat 是纯 ASCII 判断不了系统，就**一个版本号都不写**。

**9. 浏览器被"强制 `UserDataDir`"时，独立 profile 抓会话整个失效**

`UserDataDir` 是 Edge / Chrome 的**强制策略**。设了它，浏览器就
**直接忽略命令行上的 `--user-data-dir`** —— 于是：

* 我们给的独立目录根本没被用上，浏览器开在**用户日常那个 profile** 里；
* URL 跑到用户已经开着的浏览器里，我们等的调试端口**永远没人监听**；
* 报出来的是「启动后立刻退出（退出码 21）」或者链接跑到别处去了。

⚠ **这是门店实测查出来的（用户找的，不是我们）** —— 前面好几轮一直在查
权限、查 profile 残留、查浏览器版本，**全都不对**。悦荟那台的部署问题
就是它。⚠ 换浏览器没用（Chrome 有同名策略）、重装没用 ——
它通常是**域 / 组策略推下来的**。

→ `browser.forced_user_data_dir()` 读注册表查出来，`_forced_dir_error()`
给两条能直接粘的 `reg delete`，`launch()` 里**硬失败**。

⚠ **必须硬失败，不许"退而求其次用那个被强制的目录"**：那等于去读
**用户日常浏览器的 cookie** —— 本模块顶部那条隐私边界
（只读我们自己启动的 profile）就是这么破的。这是**设计约束，不是保守**。

**同一个坑的另一半：403 不等于没权限。** 悦荟那次前面报过 403，
我们差点又绕回"门店配置 / 账号权限"去查（那张排除表里早就排除过一轮）。
真相是**登录流程还没走完**：cookie 拿到了但尚未生效，自检（打的正是华为接口）
于是 403；**登录一完成，同一次抓取就成功了**。
→ 报 403 先确认窗口**真的进到门户首页**了，再重抓一次。

## 数据与凭据（别提交）

`.gitignore` 已排除：`.secrets/`、`out/`、`dist/`、`BUILD.txt`、
`run*.bat`（安装时按本机 Python 路径生成）、
**`config/store-*.yaml`（门店配置 —— 以前是跟踪的，现在只发模板
`src/store-config.default.yaml`）**。

优先级别搞错：云商凭据 **环境变量 > `.secrets/erp.env` > `~/.dsh/secrets/erp.env`**。
华为会话按店存：`.secrets/cbg-<门店码>.json` —— **换店等于换一份会话**，
要用那个店的账号重新抓一次（否则"登得上、抓不到"）。
