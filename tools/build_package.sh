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

> **这一版的主线是「能在 Windows 7 上跑」。** 如果店里那台老电脑是 Win7，
> 装之前请先看本节第一条 —— 有个补丁要**先打**，顺序错了装了也起不来。
>
> Win10 / Win11 的机器**照常升级**，功能一个不少（另外还白捡了下面二、三两节）。

**一、支持 Windows 7 的老电脑了（这一版的主线）。**

* 之前在那台 Win7 上双击 `install.bat`，会直接甩一段英文报错、装不下去 —— 现在能装了。
* ⚠ **Win7 上有两件事必须按顺序做，漏一步就白装：**

  | 顺序 | 做什么 | 不做会怎样 |
  |---|---|---|
  | 1 | 先打 **KB2533623** 补丁 | 装了 Python 也起不来 |
  | 2 | 再装 **Python 3.8.10** | 3.9 以上的安装包在 Win7 上**直接报错装不上** |

  Python 3.8.10 下载页：<https://www.python.org/downloads/release/python-3810/>
  选 **Windows installer (64-bit)**。装的时候记得勾 `Add python.exe to PATH`。
* **为什么卡在 3.8.10 不动**：3.9 起的 Python 依赖一个 Win7 上**根本不存在**的
  系统文件（`api-ms-win-core-path-l1-1-0.dll`，见 Python 官方 issue 40740），
  所以 3.8.10 是**最后一个能在 Win7 上跑的版本**。这一版特意把代码降到
  "3.8 也能跑"，就是为了这台机器 —— 三套 Python（3.8 / 3.9 / 3.14）
  每一版发布前都跑过全套测试。
* 安装时**会记住这台电脑用的是哪个 Python**。一台电脑上装了不止一个 Python 时，
  以前会出现「依赖装进了 A、启动却用 B」，报 `No module named 'requests'`
  —— 看着像当初没装成功，其实是装到另一个 Python 里去了。现在不会了。
* **提示按系统给**：Win7 上只给 3.8.10 的下载地址，别的系统给最新版。
  以前四个 `.bat` 里写死了"装 Python 3.14"，Win7 的同事照着做会卡在安装包报错上
  —— 现在 `.bat` 里**一个版本号都不写**，由程序按系统说。
* ⚠ **Win7 上出厂只有 IE11**，而报告页是个网页、**IE11 打不开**（一片空白）。
  装个 Chrome 或 Edge，并设成默认浏览器 —— 「自动抓华为会话」也要用它。

**二、装和跑都不再需要管理员权限了，一次 UAC 都不弹。**

（Win7 上同样适用 —— 这一版之后，装机那一次 UAC 也取消了。）

* 以前双击 `install.bat` 会弹一次蓝色 UAC，用来注册"以管理员身份启动"的计划任务。
  **现在不用了** —— 开机自启写的是当前用户自己的注册表启动项，普通权限就够。
* 顺带修好了一个**会让人白折腾很久**的毛病：服务以管理员身份跑的时候，
  **「自动抓华为会话」是坏的** —— Edge / Chrome 拒绝以管理员运行，进程起来后
  把命令行交棒出去就自己退 0，链接跑到了你原来那个浏览器里，
  程序却报「Edge 启动后立刻退出（退出码 0）」。报错还写"可能的原因"，
  让人以为是猜的。
* 现在：报错会**明说"已确认：服务是管理员身份"**，并给出改回来的三步操作。

> **老版本装过的电脑要做一件事**：到「设置 → 后台服务」，
> 点那个「**以管理员身份修复**」按钮（只在需要时才出现），
> 它只弹**这一次** UAC，把旧版本留下的提权任务删掉。
> 然后 `stop.bat` 停掉服务、用普通权限双击 `start.bat`。
>
> ⚠ **设「每天定时对账」时，有的电脑会弹一次 UAC** —— 那是正常的，**只弹这一次**。
> 定时任务是 Windows 的**系统级**设置，系统规定只有管理员能建
> （开机自启不一样，那个写你自己的用户设置，不要权限）。
> 程序会**先用普通权限试**：系统允许就直接建好、一次 UAC 都不弹；
> 报「注册失败」时才轮到这个 UAC。
> **它不会让程序变成管理员运行**：建出来的任务电脑会当普通程序跑。
> 报「注册失败」时界面上会当场出现一个「**以管理员身份重试**」按钮，点它就行。
>
> ⚠ **这一步不做，问题会一直在**，而且症状看着像三件不相干的事：
> ① 怎么启动都提示"管理员"；② 「自动抓会话」还是失败；
> ③ 连「每天定时对账」都注册不上（旧任务是管理员建的，普通权限覆盖不了）。
> 其实都是那一条旧任务在作祟。
>
> 如果那个按钮也没成（UAC 点了"否"，或者按钮没出现），拿管理员权限手动删：
> 右键「命令提示符」→「以管理员身份运行」，粘这一条回车：
>
> ```
> schtasks /delete /tn "CBG报量对账-开机自启" /f
> schtasks /delete /tn "CBG报量对账" /f
> ```

**三、定时任务那一块修好了（这一版的重点）。**

* **「以管理员身份重试」以前点了没反应** —— 因为旧任务被管理员建过之后
  **普通权限连读都读不到**，程序看不到它，就以为"没有旧任务、不用提权"，
  于是什么都没做。而实际点注册时系统报的是 `错误: 拒绝访问。`
  （任务库不让普通权限建，或者 `/f` 覆盖不了旧的）。
  现在这个按钮**真的会去建** —— 弹一次 UAC，用管理员权限把那条任务重新注册一遍，
  旧同名任务一并覆盖。
* **提权建的任务，界面上照样看得到时间和命令。** 以前提权建出来的任务
  归 `Administrators` 所有，而程序是普通权限 → 查都查不动，
  「定时执行」里只剩一个任务名、时间和命令全是空的。
  用户看到的就是「**没有管理员权限就看不到定时执行设置了**」。
  现在**程序会把注册时用的参数自己记一份**（`.secrets\\schedule.json`），
  界面显示走这份记录，**不看 Windows 的脸色**。删任务时会一并抹掉。
  这个文件跟会话、账号放在一起，**升级不会动它**。
* **修好了「删除」按钮的一个哑巴 bug**：删除时程序拿的是带反斜杠的完整任务名
  （`\\CBG报量对账-21点20`），而记录里的键是不带反斜杠的 ——
  对不上就**永远删不掉记录**，界面表现是"删了之后时间和命令还挂在那儿"。
  现在两种名字都认。
* **修好了「以管理员身份修复定时任务」按钮**：它以前发的是带反斜杠的完整任务名，
  而注册那条路**拒收带反斜杠的名字** → 一点就 400，
  表现还是"点了完全没反应"。现在会自动收敛成不带反斜杠的名字。
* 「读不到详情」那段黄色提示现在**只在真正没有记录的旧任务上出现** ——
  有记录的话界面照常显示，不会再让人以为必须去提权。

> 那台已经有旧任务的电脑，点一次「**以管理员身份修复定时任务**」就好
> （⚠ 点之前先确认上面「执行时间」填的是你要的时间，修复就是拿它去重建）。

**Win10 / Win11 的机器**：功能不受影响，升级后行为跟以前一样（只是不再需要管理员）。

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
