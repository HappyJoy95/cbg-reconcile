#!/usr/bin/env bash
# 打一个发给门店电脑部署的**正式包**。
#
#   bash tools/build_package.sh
#
# 产出（dist/ 下）：
#   cbg-reconcile-v<版本>-<日期>.zip   发给门店的包
#   cbg-reconcile-v<版本>-<日期>.sha256  校验和（确认拷过去没坏）
#
# 两件容易出错的事在这里一次解决：
#   1. **绝不能把会话/浏览器 profile 打进去** —— 那是本机的登录态，换了机器没用还有风险
#   2. .bat 里**一个中文都不能有** —— 批处理的编码受控制台代码页摆布，
#      写死编码在某些机器上就是方块。中文提示一律由 Python 打印。

set -euo pipefail

# 用法：
#   bash tools/build_package.sh            # 正式包（发给门店）
#   bash tools/build_package.sh beta       # 测试包 —— 包名和 BUILD.txt 都带 beta 标记
#   CBG_BETA=1 bash tools/build_package.sh # 同上（环境变量也行）
#
# beta 包的用处：改了东西想先在门店/本机试，但**还不想发版**。
# 它不改 src/version.py，所以线上版本号不动 —— 只是文件名和指纹上多一个标记，
# 好跟你手上的正式包区分开（不然两个 zip 长得一样，很容易发错）。
BETA="${1:-${CBG_BETA:-}}"
case "${BETA}" in
  1|true|yes|beta|BETA) BETA=1 ;;
  "") BETA="" ;;
  *) echo "参数只认 beta（或留空）。收到的是：${BETA}"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
NAME="cbg-reconcile"                              # 包内顶层目录用 ASCII —— 少一层乱码风险
# 版本号从代码里读，别手写 —— 手写迟早跟 version.py 对不上
VER="$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/version.py")"
[ -n "${VER}" ] || { echo "读不出版本号（src/version.py）"; exit 1; }
SUFFIX=""; [ -n "${BETA}" ] && SUFFIX="-beta"
ZIPNAME="cbg-reconcile-v${VER}${SUFFIX}-${STAMP}.zip"
DIST="${ROOT}/dist"
STAGE_ROOT="$(mktemp -d)"
STAGE="${STAGE_ROOT}/${NAME}"

echo "==> 打包 ${ZIPNAME}"
mkdir -p "${STAGE}" "${DIST}"

# ---------------------------------------------------------------- 复制源码
rsync -a \
  --exclude '.secrets/' \
  --exclude '.dsh/' \
  --exclude 'config/store-*.yaml' \
  --exclude 'out/' \
  --exclude 'dist/' \
  --exclude 'tools/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude '.DS_Store' \
  --exclude '.git/' \
  --exclude 'run.sh' \
  --exclude 'run.bat' \
  --exclude 'run-now.sh' \
  --exclude 'run-now.bat' \
  --exclude '.gitignore' \
  --exclude 'README.md' \
  --exclude 'AGENTS.md' \
  --exclude 'agent.md' \
  --exclude '运维手册.md' \
  "${ROOT}/" "${STAGE}/"
# ⚠ `.dsh/` **必须排除** —— 它是工作区隔离区（记忆日志 / 备份 / 临时任务 /
#   本机 venv），跟 `.secrets/` 一样是**这台电脑自己的东西**，进包毫无意义，
#   而且里面写着踩坑记录和内部路径，发给门店既没用也不合适。
#   加 `--exclude` 是防"忘了"：这行以前不在，只是当时 `.dsh/` 里恰好没东西，
#   直到有一天本机 venv 建在里面，才被下面"不能有本机绝对路径"那条自检逮住。
# ⚠ `update-debug.py` 不排除：自更新是"照仓库原样铺"，包里有、更新后也该有 ——
#   不然同一个版本号会有两种内容（zip 装的没有、自更新的有）。
#   它是更新失败时的现场诊断脚本，留着有用。

# ------------------------------------------------- 凭据 / 报告 / 门店配置
# 这三样一个都不放进包（.secrets/ 、out/ 、config/store-*.yaml）。
#   它们是**这台电脑自己的东西**：云商账号、历史报告、门店配置。
#   以前包里带着空模板（或某家店的真实配置），后果是**手工把新包拷到
#   已有安装上时会把门店的设置冲掉** —— 每次拷贝都得记着「跳过这三个目录」，
#   迟早出错。现在包里一个都不带，**整个目录直接覆盖就是安全的**。
#   安装时由 bootstrap.ensure_layout() 按需生成（模板在 src/store-config.default.yaml）。
echo '    · 凭据 / 报告 / 门店配置：不进包（安装时按需生成）'

# ------------------------------------------------------------------ 拷 bat
# ⚠ 以前这里把 bat 转成 GBK，结果在**控制台代码页不是 936** 的机器上全是方块。
#   现在 bat 里一个中文都没有，提示全交给 Python 打印
#   （Windows 上 Python 走 WriteConsoleW，跟代码页无关），所以只要补 CRLF。
#   这里顺手**断言纯 ASCII** —— 谁哪天往 bat 里塞了中文，打包直接失败。
echo "==> 拷贝 bat（纯 ASCII，只补 CRLF）"
for f in install start stop selftest uninstall diagnose; do
  python3 - "$ROOT/$f.bat" "$STAGE/$f.bat" "$f" <<'BATCONV'
import sys, pathlib
src, dst, name = sys.argv[1], sys.argv[2], sys.argv[3]
raw = pathlib.Path(src).read_bytes()
bad = sum(1 for b in raw if b > 127)
if bad:
    raise SystemExit(f"    [X] {name}.bat 里有 {bad} 个非 ASCII 字节 —— "
                     f"中文必须交给 Python 打印，不能写在 bat 里")
out = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
pathlib.Path(dst).write_bytes(out)
print(f"    [OK] {name}.bat（纯 ASCII，CRLF）")
BATCONV
done

# ------------------------------------------------------------ 构建指纹
# 门店电脑上跑的往往是拷过去的旧版本 —— 没有这个，没人知道对面是哪一版，
# "我明明修好了 / 你那边怎么还这样" 一来回就是一轮。
#
# beta 包把标记也写进指纹：这样控制台标题下面那行会显示「v1.4.10 · beta · …」——
# 门店（或你自己）一眼就知道手上这个是测试包，不是正式版。
BUILD_STAMP="$(date '+%Y-%m-%d %H:%M')"
if [ -n "${BETA}" ]; then
  printf 'beta · %s\n' "${BUILD_STAMP}" > "${STAGE}/BUILD.txt"
  echo "==> 构建指纹：beta · ${BUILD_STAMP}（测试包）"
else
  printf '%s\n' "${BUILD_STAMP}" > "${STAGE}/BUILD.txt"
  echo "==> 构建指纹：${BUILD_STAMP}"
fi

# ---------------------------------------------------------------- 发布说明
# ⚠ 用 Python 写而不是 heredoc：正文里有大量反引号（`install.bat` 这种），
#   不带引号的 heredoc 会把它们当**命令替换**执行掉（真踩了）。
#
# ⚠ **每次发版都要改下面「这一版的变化」那一节** —— 它是门店唯一能看到的
#   "这次升级到底动了什么"。忘了改的话，门店拿到的说明跟实际不符，
#   比没有说明更糟（"你说明明没改"）。
python3 - "${STAGE}" "${VER}" "${BUILD_STAMP}" "${BETA:-}" <<'RELNOTES'
import pathlib, sys

stage, ver, stamp, beta = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = f"""# CBG 报量对账 · 发布说明

**版本** v{ver}　**构建** {stamp}

---

## 这一版的变化

> **v2.0.0 的主线：多了一个「POS 合规」标签页，程序开始在本地存一份订单数据。**
>
> * **已经装着 v1.6.1 的电脑**：看第一～四节。
>   「补历史数据」那件事**程序会自己做**，不用你操作。
> * **从 v1.6.0 或更早升上来的**：第五节那几条（Win7 补丁、管理员权限、
>   验证码提示）仍然适用。

**一、控制台多了一个「POS 合规」标签页。**

算的是门店的 **POS 使用率**：卖出去的钱里，有多少是走非现金支付的。

| | 怎么算 |
|---|---|
| **分母** | 本店订单金额，**去掉**国补单、即时零售单、Care+ 服务单 |
| **分子** | 上面这些单里**非现金支付**的金额 |
| **POS 使用率** | 分子 ÷ 分母 |

* ⚠ **这个数没有达标线，界面故意不做红绿** —— 它只把事实摆出来。
* ⚠ **国补在系统里没有机器能认的标记**，只能靠营业员填的备注认。
  所以同一个指标给了**两个口径**（按订单标签 / 按备注），数字会有出入。
  **备注写错了这边就会算错** —— 这块只能靠备注写对。
* 另外还给一个「**申诉后**」的数：把有争议的国补单剔除之后是多少。
* ⚠ **最近两个月标着「暂定」**：口径是「退货算在**退货发生**的那个月」，
  所以**上个月的分数还会被这个月的退货改**。要对外引用，就引用两个月以前的。
* 建店那个月分母可能是 0（整月只有国补 / 即时零售单），
  那个月显示「—」，**不是 0%**。

**二、程序开始在本地存一份订单数据。**

* 存在 `out\\cbg-2026.db`（**一年一个文件**）：华为拉回来的订单、明细、
  支付、退货都在里面。报量对账的华为那一侧，现在**从这份数据里读**，不再每次现拉。
* **第一次跑的时候，它会自动把全部历史补进来**（比较慢，几分钟到十几分钟），
  之后每天只抓当月增量。
  **不补的话，POS 页只有当月一个数、前面几个月全是空的。**
* ⚠ **`out\\` 目录不要删、不要挪** —— 里面有你的历史报告和这份数据。
  升级、卸载都不会动它。
* 顺带修好了报量对账里的**两个误报**（所以差异清单会比以前少几条）：
  * 以前「已退货」的原单会被整个滤掉，于是云商那边其实还在的销售，
    被误报成「没报量」；
  * 一个串号挂在两张单上时，以前可能取到 Care+ 服务单那张 ——
    报量栏里的产品名和金额是错的。

**三、每天那次定时对账，现在跑三步。**

（以前只有一步：对账。现在前面多一步抓数据，后面多一步算 POS。）

| 顺序 | 干什么 | 要多久 |
|---|---|---|
| 1 | 去华为把**当月**订单拉回来，补进本地数据 | 一两分钟（看当月单量） |
| 2 | 报量对账（就是以前那一步） | 跟以前差不多 |
| 3 | 算 POS 合规 | 几秒 |

* ⚠ **第 1 步失败 ⇒ 后面两步都不跑，也什么都不发。** 这是**故意**的：
  数据没拉全的时候算出来的差异清单**是错的**，而且**看着很合理**，
  照着它去补报只会报一批假的。**宁可今天没有报告，也不发一份假的。**
* **所以升级后如果某天没收到报告**：先看控制台首页的运行日志
  （或 `out\\run.log`）—— 多半是华为会话过期，到「会话」页重新抓一次就好。
* 定时任务的命令程序会**自己更新**（老版本那份写的是旧命令）。
  想让它立刻生效，也可以到「设置 → 定时任务」**重新注册一次**。

**四、华为会话过期时，程序会自己续一次。**

以前是「对账时顺手续」，现在挪到了第 1 步（抓数据之前）——
位置才对：**华为只在这一步被登录**。
续期是**无头**的，**不会弹浏览器窗口**；**失败也不会覆盖你手上那份好的会话**。
续不上才会报错，并让你手动登录一次。

**五、如果你是从 v1.6.0 或更早升上来的**

下面这几条仍然适用：

* **Windows 7 的电脑**：先打 **KB2533623** 补丁，**再**装 **Python 3.8.10**
  （顺序反了装了也起不来；3.9 以上在 Win7 上根本装不上）。
* **装和跑都不需要管理员权限**。老版本如果在那台电脑上留了提权任务，
  到「设置 → 后台服务」点一次「**以管理员身份修复**」。
* 抓华为会话撞上**验证码**时会当场提示你，按提示手动登一次即可。
* 定时任务注册失败时，界面上会当场出现「**以管理员身份重试**」按钮，点它就行。


---

## 这是什么

比对「云商里卖的、归属本店的串号」有没有都报量给华为，没报的列成差异清单。
装在**门店自己的电脑**上，每天定时跑一次，结果推邮件 / 企业微信。

## 装之前先确认四件事

**1. 这台电脑装了 Python**（**3.8 以上都行**）

- 装的时候**必须勾上** `Add python.exe to PATH`
- 双击 `install.bat` 时如果提示找不到 Python，就是这一步没做
- ⚠ **Windows 7 的电脑 —— 顺序不能反**：
  1. **先打 KB2533623 补丁**，再装 Python（没打的话装了也跑不起来）
  2. Python **只能装 3.8.10** —— 3.9 以上在 Win7 上装不上。
     到 <https://www.python.org/downloads/release/python-3810/>
     下 **Windows installer (64-bit)**
- ⚠ Win7 上**看不到版本提示也正常**：`.bat` 里故意不写版本号，
  双击之后由程序按系统告诉你该装哪个

**2. 这台电脑是哪个店**

- 包里的门店配置**是空的** —— 双击 `install.bat` 时会自动建一份
  `config/store-SCN231409.yaml`（文件名固定，内容是模板）
- **启动后在控制台「设置 → 门店」填那三行**：
  `store_code`（华为门店编码）/ `marker`（串号标识）/ `erp_store_name`（云商门店名）
  —— 改完**立刻生效，不用重启**
- ⚠ **填错不会报错，只会静默算错**（拿别家的报账来比）。
  跑一次 `selftest.bat`，第 0 节会打印当前配的是哪个店，对着核一下
- ⚠ **换了门店要重新登录华为**：会话文件是按店存的
  （`.secrets/cbg-<门店码>.json`），换店等于换了一份会话 ——
  到「会话」页用**那个店的账号**重新抓一次

**3. 华为账号**

每个店用自己的账号。会话是按店的，拿 A 店的会话去查 B 店会报"没有权限"。

**4. 浏览器用 Edge 或 Chrome**

报告页是个**网页**，**IE11 打不开**（会是一片空白）。

- `start.bat` 会自动打开浏览器 —— 请把 Edge 或 Chrome 设成**默认浏览器**
- **Windows 7** 上出厂只有 IE11，Edge 不一定装了；没有的话装一个 Chrome
- 「自动抓华为会话」也要用这个浏览器，IE11 不行

## 安装（四步）

1. 解压到**一个不会被挪走的目录**，比如 `D:\\cbg-reconcile`
   - 路径里**别带空格**，也别放桌面（用户目录名可能带空格）
2. 双击 `install.bat` —— 装依赖，然后问一句要不要开机自启（**回车即开**）
   - **不需要管理员权限，不会弹 UAC**
   - 开机自启用的是当前用户的注册表启动项，普通权限就够
3. 双击 `start.bat` —— 打开控制台
4. 在控制台里依次配：**门店那三行** → 云商账号 → 邮件 / 企微
   → 会话页抓一次登录 → 设置页加定时任务（建议设在**关门前**，比如 21:00）

   > 顺序不重要，**改完都立刻生效、不用重启**。只有一条：
   > **先把门店配对，再加定时任务** —— 不然到点那一跑会对到别的店账上去。

完整步骤看 **`门店操作手册.md`**。

## 出问题先做这个

**双击 `diagnose.bat`** —— 生成 `diagnose-result.txt`，把那个文件发回来就行。

它**不需要 Python 也能跑**（要查的往往就是"Python 没装好"，那时任何 Python
脚本都起不来）。里面有：`where python`、依赖检查、`run.bat` 全文、日志全文、
计划任务列表，以及**真跑一次的完整报错**。

## 包里有什么

| 文件 | 干什么 |
|---|---|
| `install.bat` | 装依赖 + 问要不要开机自启 |
| `start.bat` / `stop.bat` | 起 / 停后台服务 |
| `selftest.bat` | 逐项自检（第 0 节打印版本和 Python 版本） |
| `diagnose.bat` | 出问题时一键收集信息 |
| `uninstall.bat` | 卸载（**默认不删**报告和凭据） |
| `门店操作手册.md` | 门店操作手册（**五六步，有问题先翻这个**） |
| `发布说明.md` | 就是本文件 |
| `config\\stores.yaml` | 14 家店的映射表（**程序数据**，随版本更新） |

> ⚠ **`.secrets\\`、`out\\`、`config\\store-*.yaml` 这三样不在包里** ——
> 它们是这台电脑自己的东西（云商账号、历史报告、门店配置），
> **双击 `install.bat` 时会自动建好**（只建缺的，绝不覆盖已有的）。
>
> 这么做的直接好处：**升级时把新包整个目录拷过来覆盖就行**，
> 不用再提心吊胆地"跳过这三个目录" —— 包里根本没有会覆盖它们的东西。

> `run.bat` / `run-now.bat` **故意不在包里** —— 它们是**安装时**按这台电脑的
> Python 路径生成的，别人机器上的拷过来没用。
>
> 想手动跑一次对账就双击 `run-now.bat`（它跑完会停住让你看结果）；
> `run.bat` 是给计划任务调的，跑完窗口自己关。

## 怎么确认升级/安装的是这一版

打开控制台，标题下面那行就是：

```
v{ver} · {stamp}
```

命令行也行：双击 `selftest.bat`，**第 0 节**会打印版本。
"""
if beta:
    body = body.replace(
        "---",
        "> ⚠️ **这是测试包（beta），不是正式版。** 只用来验证改动，"
        "别长期留在门店电脑上。\n\n---", 1)
    body = body.replace(f"v{ver} · {stamp}",
                        f"v{ver} · beta · {stamp}（测试包会显示 beta）")
pathlib.Path(stage, "发布说明.md").write_text(body, encoding="utf-8")
print(f"    ✓ 发布说明.md（v{ver}{' · beta' if beta else ''}）")
RELNOTES


# ---------------------------------------------------------------- 指南与文档
# 仓库布局 == 安装布局，直接平铺（不再有 packaging/ 中间层）
#
# ⚠ **包里只放门店真正会看的文档。**
#
# 以前还拷 `README.md`（41 KB）和 `设计文档.md`（53 KB）—— 文档占了顶层内容的
# 3/4，而这两份对门店没用：README 是写给开发者的（接口契约、已知坑、怎么跑测试），
# 设计文档是技术架构。更糟的是 README 里那节「部署到门店电脑」讲的是**手工流程**
# （拷目录、手建 .secrets\erp.env、写 run.bat），跟 install.bat 那套不是一回事 ——
# 远程指挥门店时，他翻到 README 就会照着做错。
#
# 两份都在 git 仓库里，维护的人照样看得到；门店这边留指南 + 发布说明就够了。
cp "${ROOT}/门店操作手册.md" "${STAGE}/门店操作手册.md"

# ---------------------------------------------------------------- 自检
echo "==> 打包前自检"
fail=0
check_absent() {
  if [ -e "$1" ]; then echo "    ✗ 不该出现：${1#${STAGE}/}"; fail=1; fi
}
check_absent "${STAGE}/.secrets/cbg-SCN231409.json"
check_absent "${STAGE}/.secrets/browser-profile"
check_absent "${STAGE}/.secrets/curl.txt"
check_absent "${STAGE}/.dsh"
# 这三个目录是「这台电脑自己的东西」，进包 = 手工拷贝时冲掉门店设置
check_absent "${STAGE}/.secrets"
check_absent "${STAGE}/out"
check_absent "${STAGE}/run.sh"
check_absent "${STAGE}/dist"
[ -f "${STAGE}/src/cli.py" ]      || { echo "    ✗ 缺 src/cli.py"; fail=1; }
[ -f "${STAGE}/web/index.html" ]  || { echo "    ✗ 缺 web/index.html"; fail=1; }
[ -f "${STAGE}/config/stores.yaml" ] || { echo "    ✗ 缺 config/stores.yaml"; fail=1; }
# 门店配置模板**必须**在包里 —— 没有它，新机器装完就没有配置文件，程序起不来
[ -f "${STAGE}/src/store-config.default.yaml" ] \
  || { echo "    ✗ 缺 src/store-config.default.yaml（门店配置模板）"; fail=1; }
_storecfg="$(ls "${STAGE}"/config/store-*.yaml 2>/dev/null || true)"
if [ -n "${_storecfg}" ]; then
  echo "    ✗ 包里混进了门店自己的配置（会冲掉门店填好的那份）："
  echo "${_storecfg}" | sed 's/^/       /'
  fail=1
fi
[ -f "${STAGE}/install.bat" ]     || { echo "    ✗ 缺 install.bat"; fail=1; }
[ -f "${STAGE}/start.bat" ]       || { echo "    ✗ 缺 start.bat"; fail=1; }
[ -f "${STAGE}/stop.bat" ]        || { echo "    ✗ 缺 stop.bat"; fail=1; }
[ -f "${STAGE}/boot.py" ]         || { echo "    ✗ 缺 boot.py（开机自启要用）"; fail=1; }
[ -f "${STAGE}/bootstrap.py" ]    || { echo "    ✗ 缺 bootstrap.py（所有 bat 都靠它）"; fail=1; }
[ -f "${STAGE}/uninstall.bat" ]   || { echo "    ✗ 缺 uninstall.bat"; fail=1; }
[ -f "${STAGE}/diagnose.bat" ]    || { echo "    ✗ 缺 diagnose.bat"; fail=1; }
[ -f "${STAGE}/run_check.py" ]    || { echo "    ✗ 缺 run_check.py（计划任务靠它记日志）"; fail=1; }
[ -f "${STAGE}/门店操作手册.md" ] || { echo "    ✗ 缺 门店操作手册.md"; fail=1; }
[ -f "${STAGE}/发布说明.md" ]      || { echo "    ✗ 缺 发布说明.md"; fail=1; }
# 本机生成的 run 脚本绝不能进包 —— 里面写着**开发机**的 Python 绝对路径，
# 门店电脑上跑不了。（上次就漏了 run-now.sh 进去。）
_stray="$(ls "${STAGE}"/run*.sh "${STAGE}"/run*.bat 2>/dev/null || true)"
if [ -n "${_stray}" ]; then
  echo "    ✗ 包里混进了本机生成的 run 脚本（里面是开发机的 Python 路径）："
  echo "${_stray}" | sed 's/^/       /'
  fail=1
fi
[ -f "${STAGE}/requirements.txt" ] || { echo "    ✗ 缺 requirements.txt"; fail=1; }
# ⚠ 这几份**不该**进包：README 是给开发者的（而且里面那节"部署到门店"讲的是
#   手工流程，跟 install.bat 那套不一样，门店照着做会错），设计文档是技术架构，
#   AGENTS.md / agent.md 是给 AI 看的开发规矩和动手指令。
#   加这条是防止以后谁顺手又拷回去 —— 门店那边"文档越多越乱"。
#
# ⚠ 这道断言以前**漏了 `AGENTS.md`**（rsync 排除了，但反查没查它）——
#   而 AGENTS.md 正文里写着"rsync 排除 + 反查断言，两道"，等于文档承诺了、
#   代码没做。2026-09-16 补上，顺便加 `agent.md`。
#   名单要和 `--exclude` 那一段**一一对上**，对不上就是下次踩坑的开始。
for _doc in README.md 设计文档.md 运维手册.md AGENTS.md agent.md; do
  if [ -e "${STAGE}/${_doc}" ]; then
    echo "    ✗ 包里混进了不给门店看的文档：${_doc}（门店只看 门店操作手册.md）"
    fail=1
  fi
done
# 包里不能有任何本机绝对路径
if grep -rIl "/Users/ashui" "${STAGE}" 2>/dev/null | head -3 | grep -q .; then
  echo "    ✗ 有文件残留本机绝对路径："; grep -rIl "/Users/ashui" "${STAGE}" | head -3
  fail=1
fi
# 开发垃圾不能进包（门店同事会打开这个目录，看到缓存文件会困惑）
for junk in '.pytest_cache' '__pycache__' 'dist' 'tools' 'packaging' \
            'run.sh' 'run.bat' 'run-now.sh' 'run-now.bat' '.gitignore'; do
  if [ -e "${STAGE}/${junk}" ]; then
    echo "    ✗ 包里混进了开发文件：${junk}"
    fail=1
  fi
done
[ "$fail" = "0" ] && echo "    ✓ 全部通过" || { echo "打包中止"; exit 1; }

# ---------------------------------------------------------------- 压缩
# ⚠ 不能用命令行的 zip：macOS 的 zip **不设 UTF-8 标志位**，
#   中文文件名解到 Windows 上会全变成乱码。Python 的 zipfile 会自动设。
ZIP="${DIST}/${ZIPNAME}"
rm -f "${ZIP}"
python3 - "${STAGE_ROOT}" "${NAME}" "${ZIP}" <<'PY'
import os, pathlib, sys, zipfile

stage_root, name, zip_path = sys.argv[1], sys.argv[2], sys.argv[3]
base = pathlib.Path(stage_root) / name
count = 0
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f == ".DS_Store":
                continue
            p = pathlib.Path(root) / f
            z.write(p, p.relative_to(base.parent).as_posix())
            count += 1
print(f"    ✓ 写入 {count} 个文件")
PY
rm -rf "${STAGE_ROOT}"

# ---------------------------------------------------------------- 验证
echo "==> 验证包"
python3 - "${ZIP}" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
bad = [i.filename for i in z.infolist()
       if any(ord(c) > 127 for c in i.filename) and not (i.flag_bits & 0x800)]
print(f"    ✓ 中文文件名 UTF-8 标志位：{'全部正确' if not bad else f'❌ {len(bad)} 个有问题'}")
if bad:
    print("      ", bad[:3]); sys.exit(1)
print(f"    ✓ 解压完整性：{z.testzip() or '无损坏'}")
PY

echo
echo "==> 完成：${ZIP}"
echo "    大小：$(du -h "${ZIP}" | cut -f1)"
echo "    顶层内容："
python3 - "${ZIP}" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
tops = sorted({n.split("/")[1] for n in z.namelist() if n.count("/") == 1 and not n.endswith("/")})
dirs = sorted({n.split("/")[1] for n in z.namelist() if n.count("/") > 1 and n.split("/")[1]})
for n in tops + [d + "/" for d in dirs]:
    print("      " + n)
PY

# 校验和 —— 门店拷过去之后能确认文件没坏
_zipbase="$(basename "${ZIP}")"
if command -v shasum >/dev/null 2>&1; then
  ( cd "${DIST}" && shasum -a 256 "${_zipbase}" > "${_zipbase%.zip}.sha256" )
else
  ( cd "${DIST}" && sha256sum "${_zipbase}" > "${_zipbase%.zip}.sha256" )
fi
echo "    校验和：$(cut -d' ' -f1 "${DIST}/${_zipbase%.zip}.sha256")"
