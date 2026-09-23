#!/usr/bin/env bash
# 打一个发给门店电脑部署的**正式包**。
#
#   bash tools/build_package.sh
#
# 产出（dist/ 下）：
#   cbg-reconcile-v<版本>-<日期>.zip          发给门店的包
#   cbg-reconcile-v<版本>-<日期>.sha256       校验和（确认拷过去没坏）
#   cbg-reconcile-v<版本>-beta<N>-<日期>.zip  测试包（`beta` 参数，自动编号）
#
# 两件容易出错的事在这里一次解决：
#   1. **绝不能把会话/浏览器 profile 打进去** —— 那是本机的登录态，换了机器没用还有风险
#   2. .bat 里**一个中文都不能有** —— 批处理的编码受控制台代码页摆布，
#      写死编码在某些机器上就是方块。中文提示一律由 Python 打印。

set -euo pipefail

# 用法：
#   bash tools/build_package.sh            # 正式包（发给门店）
#   bash tools/build_package.sh beta       # 测试包 —— **自动编号**（beta0、beta1、beta2…）
#   bash tools/build_package.sh beta3      # 指定编号（重打/覆盖某一个编号时用）
#   CBG_BETA=1 bash tools/build_package.sh # 同上（环境变量也行）
#
# beta 包的用处：改了东西想先在门店/本机试，但**还不想发版**。
# 它不改 src/version.py，所以线上版本号不动 —— 只是文件名和指纹上多一个标记，
# 好跟你手上的正式包区分开（不然两个 zip 长得一样，很容易发错）。
#
# ⚠ **编号是用户 2026-09-17 定的**。那之前包名是 `...-beta-20260918.zip`，
#   **同一天打第二个就把第一个盖掉了** —— 实测那天连打三回，只剩最后一个，
#   "门店手上是哪个"谁也说不清。
#
# **一个版本的完整生命周期**（用户原话：「以后就没有裸的 beta 了，
# 上来是 beta0，beta1 一直往后，确认没问题之后去掉 beta 变成正式版」）：
#
#     beta0 → beta1 → beta2 → … → 确认没问题 → （不带参数）正式包
#
#   * **从 beta0 开始**，不存在"裸的 beta"（不带数字的那种）——
#     裸的跟正式包在文件名上只差几个字母，最容易发错；
#   * 编号**同时写进 `BUILD.txt`**，所以**控制台标题下面那行**会显示
#     「v2.1.0 · beta2 · 2026-09-17 18:52」—— 在门店电脑上也能一眼看出是第几版；
#   * 确认没问题之后**不带参数**打正式包：**版本号不变**（还是 v2.1.0），
#     只是文件名和指纹上的 beta 标记没了。门店靠版本号判"有没有新版"，
#     所以「打正式包」本身不会让门店看到更新 —— 要 push 上去才算发版。
#   ⚠ `BUILD.txt` 里**必须仍然含 "beta" 这几个字母**：`dbmigrate` 那道
#   「beta 包不许动门店的库」的门槛就是靠它认的（`src/dbmigrate.py`）。
#   别把标记改成纯数字。
BETA_RAW="${1:-${CBG_BETA:-}}"
case "${BETA_RAW}" in
  ""|false|no) BETA_N="" ;;
  # ⚠ `beta` 单独一个词 = **自动接号**，不是"裸 beta" ——
  #   下面的编号一定 >= 0，不会退回不带数字的写法。
  1|true|yes|beta|BETA) BETA_N="auto" ;;
  beta[0-9]|beta[0-9][0-9]|BETA[0-9]|BETA[0-9][0-9])
    BETA_N="${BETA_RAW#[Bb][Ee][Tt][Aa]}" ;;
  [0-9]|[0-9][0-9]) BETA_N="${BETA_RAW}" ;;
  *) echo "参数只认 beta / betaN（N 是编号）或留空。收到的是：${BETA_RAW}"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
NAME="cbg-reconcile"                              # 包内顶层目录用 ASCII —— 少一层乱码风险
# 版本号从代码里读，别手写 —— 手写迟早跟 version.py 对不上
VER="$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/version.py")"
[ -n "${VER}" ] || { echo "读不出版本号（src/version.py）"; exit 1; }
# 正式包：发版号 = 封包时刻 yy.mmdd.hhmmss（beta 仍用仓库里现有 VERSION）
if [ "${BETA_N}" = "" ]; then
  VER="$(date +%y.%m%d.%H%M%S)"
  echo "==> 发版号（封包时间 yy.mmdd.hhmmss）：${VER}"
fi
DIST="${ROOT}/dist"

# beta 自动编号：数一下 dist 里**同一个版本**已经打到 beta 几了，接着往下排。
# 编号只从 dist 里已有的包推出来（没有额外的状态文件）—— 那个目录本来就得留着，
# 多一份计数器就多一个"对不上"的机会。
#
# ⚠ **从 beta0 开始**（用户 2026-09-17 定：「上来是 beta0，beta1 一直往后」）——
#   所以 `_max` 的初始值是 **-1** 不是 0：一个都没有的时候下一个是 `beta0`。
#   写成 0 的话第一包会变成 `beta1`，序列就没有 beta0 了。
# ⚠ 老的无编号包（`…-beta-20260917.zip`）推不出数字，**跳过不算数**。
if [ "${BETA_N}" = "auto" ]; then
  _max=-1
  for _f in "${DIST}/cbg-reconcile-v${VER}-beta"*.zip; do
    [ -e "${_f}" ] || continue                  # 没匹配上时 glob 会原样返回，跳过
    _n="$(basename "${_f}")"
    _n="${_n#cbg-reconcile-v${VER}-beta}"       # → `3-20260917.zip` 或 `-20260917.zip`
    _n="${_n%%-*}"                              # → `3` 或 ``
    case "${_n}" in ""|*[!0-9]*) continue ;; esac
    [ "${_n}" -gt "${_max}" ] && _max="${_n}"
  done
  BETA_N="$((_max + 1))"
fi

BETA_LABEL=""; [ -n "${BETA_N}" ] && BETA_LABEL="beta${BETA_N}"
SUFFIX=""; [ -n "${BETA_LABEL}" ] && SUFFIX="-${BETA_LABEL}"
ZIPNAME="cbg-reconcile-v${VER}${SUFFIX}-${STAMP}.zip"
STAGE_ROOT="$(mktemp -d)"
STAGE="${STAGE_ROOT}/${NAME}"

echo "==> 打包 ${ZIPNAME}"
mkdir -p "${STAGE}" "${DIST}"

# ------------------------------------------------- 工作区必须是干净的（2026-09-19）
# ⚠ 为什么卡这一条：下面那句 `rsync -a "${ROOT}/"` 拷的是**工作区**，不是 HEAD。
#   于是**未提交/未跟踪**的文件也会进包 —— 而门店走自更新的那条路拿的是
#   GitHub 上的 zip（**没有**这些文件）。两者不一致的后果很具体：
#   门店装了这种包、下一次自更新就被"清理旧文件"当成残留删掉（新版里没有它），
#   等于发了一个**短命包**。
#   "发布必须来自已提交状态"本来就是发版纪律，这里只是把它变成机器拦得住的。
if command -v git >/dev/null 2>&1 && [ -d "${ROOT}/.git" ]; then
  _dirty="$(git -C "${ROOT}" status --porcelain 2>/dev/null || true)"
  if [ -n "${_dirty}" ]; then
    echo "    ✗ 工作区不干净 —— 打出来的包会跟仓库里的不一致："
    echo "${_dirty}" | sed 's/^/       /'
    echo "    先 commit（或 stash）再打包；确要跳过就设 CBG_ALLOW_DIRTY=1。"
    [ "${CBG_ALLOW_DIRTY:-}" = "1" ] || exit 1
    echo "    （CBG_ALLOW_DIRTY=1，继续打包 —— 这份包**只能自己测**，别发门店）"
  fi
fi

# ---------------------------------------------------------------- 复制源码
rsync -a \
  --exclude '.secrets/' \
  --exclude '.dsh/' \
  --exclude 'config/store-*.yaml' \
  --exclude 'out/' \
  --exclude 'in/' \
  --exclude 'dist/' \
  --exclude '/tools/' \
  --exclude 'tests/' \
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
  --exclude '设计文档.md' \
  --exclude 'packaging/' \
  --exclude '运维手册.md' \
  "${ROOT}/" "${STAGE}/"
# ⚠ `tests/` **不进门店包**（用户 2026-09-22 方案 2）：门店不跑 pytest；
#   仓库 git 里保留测试。自更新 `_targets` 用 `SKIP_APPLY` 同步跳过 ——
#   两条路径一致，避免"手工包干净、自更新又把 tests 铺回来"。
# ⚠ `.dsh/` **必须排除** —— 它是工作区隔离区（记忆日志 / 备份 / 临时任务 /
#   本机 venv），跟 `.secrets/` 一样是**这台电脑自己的东西**，进包毫无意义，
#   而且里面写着踩坑记录和内部路径，发给门店既没用也不合适。
#   加 `--exclude` 是防"忘了"：这行以前不在，只是当时 `.dsh/` 里恰好没东西，
#   直到有一天本机 venv 建在里面，才被下面"不能有本机绝对路径"那条自检逮住。
# ⚠ `update-debug.py` 不排除：自更新是"照仓库原样铺"，包里有、更新后也该有 ——
#   不然同一个版本号会有两种内容（zip 装的没有、自更新的有）。
#   它是更新失败时的现场诊断脚本，留着有用。

# ------------------------------------------------- 中台邮箱（3.0.0 起）
# 用户 2026-09-19：「能不能在 3.0.0 安装时把授权码给门店，然后后续仓库里就不带这个，
# 以后一直默认？」⇒ 授权码**只在这里**从本机 `.secrets/mail.env` 读出来塞进包，
# 仓库/git 里始终没有它。安装时 `bootstrap._seed_central_mail()` 会把它播进
# 门店的 `.secrets/mail.env`（已有键不覆盖），自更新不会动 `.secrets/` ⇒ 一直有效。
CENTRAL_SRC="${ROOT}/.secrets/mail.env"
if grep -q '^MAIL_CENTRAL_PASSWORD=' "${CENTRAL_SRC}" 2>/dev/null; then
  grep '^MAIL_CENTRAL_PASSWORD=' "${CENTRAL_SRC}" > "${STAGE}/central-mail.env"
  echo "  · 已把中台邮箱授权码塞进包（之后门店一直默认用它）"
else
  if [ -n "${BETA_N}" ]; then
    echo "  ! 本机 .secrets/mail.env 里没有中台授权码 —— beta 包先这样"
  else
    echo "  ✗ 正式包必须有中台邮箱授权码：在 .secrets/mail.env 里加一行"
    echo "      MAIL_CENTRAL_PASSWORD=…"
    echo "    （它是**安装时给门店**的，不在仓库里；漏了就整包不带，门店发不出邮件）"
    exit 1
  fi
fi

# ------------------------------------------------- 邮件附件加密密钥（3.0.0 起）
# 用户 2026-09-21：「设计一个加密算法，**所有走邮件渠道的推送都用这个加密算法加密**。
# 解密密钥**随着大版本的安装包走，不进入小版本推包**」
# ⇒ 跟上面那个中台授权码是**同一条链**（照着抄的）：
#   ① 只在这里从本机 `.secrets/mail-key.json` 读出来、塞进**包根** `mail-key.json`；
#   ② 仓库 / git 里**始终没有**它 —— 仓库是**公开**的（自更新匿名读 api.github.com），
#      进去一次，附件加密就当场归零；
#   ③ 安装时 `bootstrap._seed_mail_key()` 把它**合并**进门店的 `.secrets/mail-key.json`；
#   ④ `.secrets/` 在 `selfupdate.NEVER_TOUCH` 里 ⇒ **小版本推包（自更新）永远碰不到它**，
#      这就是"不进小版本推包"的落地方式。
#
# ⚠ **反查断言（第一道）**：仓库根不许有 `mail-key.json`。
#   它和下面那几本开发者文档一个待遇 —— `--exclude` / `.gitignore` 是一道，
#   这儿反查是第二道。区别是文档进了包只是"没用"，密钥进了仓库是**加密作废**。
if [ -e "${ROOT}/mail-key.json" ]; then
  echo "  ✗ 仓库根出现了 mail-key.json —— 那是打包注入的产物，不该留在仓库里"
  echo "    （.gitignore 已排掉它，但文件还在这儿；删掉再打）"
  exit 1
fi
KEY_SRC="${ROOT}/.secrets/mail-key.json"
if [ -f "${KEY_SRC}" ]; then
  cp "${KEY_SRC}" "${STAGE}/mail-key.json"
  _kid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("current","?"))' \
          "${KEY_SRC}" 2>/dev/null || echo '?')"
  echo "  · 已把邮件密钥塞进包（当前 ${_kid}）—— 门店装包时播进 .secrets/，之后一直用它"
else
  if [ -n "${BETA_N}" ]; then
    echo "  ! 本机没有 .secrets/mail-key.json —— beta 包先这样（附件不会加密）"
  else
    echo "  ✗ 正式包必须带邮件密钥，否则门店发出去的附件全是明文。先生成一把："
    echo "      python -m src.cli mail-key-new"
    echo "    （只在**打包这台机器**上生成；它不进仓库、也不进小版本推包）"
    exit 1
  fi
fi

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

# ------------------------------------------------------------ 写入发版号
# 包内 version.py 的 VERSION = 本次 VER；正式包再写回仓库（push 后门店才看得到）。
if [ -f "${STAGE}/src/version.py" ]; then
  python3 - "${STAGE}/src/version.py" "${VER}" <<'PYVER'
import pathlib, re, sys
path, ver = sys.argv[1], sys.argv[2]
t = pathlib.Path(path).read_text(encoding="utf-8")
pat = r'(VERSION\s*=\s*")[^"]+(")'
t2, n = re.subn(pat, lambda m: m.group(1) + ver + m.group(2), t, count=1)
if n != 1:
    sys.exit("包内 version.py 改写 VERSION 失败")
pathlib.Path(path).write_text(t2, encoding="utf-8")
print("==> 包内 VERSION =", ver)
PYVER
fi
if [ "${BETA_N}" = "" ] && [ -f "${ROOT}/src/version.py" ]; then
  python3 - "${ROOT}/src/version.py" "${VER}" <<'PYVER'
import pathlib, re, sys
path, ver = sys.argv[1], sys.argv[2]
t = pathlib.Path(path).read_text(encoding="utf-8")
pat = r'(VERSION\s*=\s*")[^"]+(")'
t2, n = re.subn(pat, lambda m: m.group(1) + ver + m.group(2), t, count=1)
if n != 1:
    sys.exit("仓库 version.py 改写 VERSION 失败")
pathlib.Path(path).write_text(t2, encoding="utf-8")
print("==> 仓库 VERSION =", ver, "（正式包已写回，commit + push 才算发版）")
PYVER
fi

# ------------------------------------------------------------ 构建指纹
# 门店电脑上跑的往往是拷过去的旧版本 —— 没有这个，没人知道对面是哪一版，
# "我明明修好了 / 你那边怎么还这样" 一来回就是一轮。
#
# beta 包把标记也写进指纹：这样控制台标题下面那行会显示
# 「v2.1.0 · beta2 · 2026-09-17 18:52」—— 门店（或你自己）一眼就知道
# 手上这个是第几版测试包，不是正式版。
# ⚠ 标记里**必须留着 "beta" 这几个字母**：`dbmigrate` 靠它判断
#   "这是测试包，不许动门店的库"。改成纯数字的话那道门槛会**静默失效**。
BUILD_STAMP="$(date '+%Y-%m-%d %H:%M')"
if [ -n "${BETA_LABEL}" ]; then
  printf '%s · %s\n' "${BETA_LABEL}" "${BUILD_STAMP}" > "${STAGE}/BUILD.txt"
  echo "==> 构建指纹：${BETA_LABEL} · ${BUILD_STAMP}（测试包，不是正式版）"
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
python3 - "${STAGE}" "${VER}" "${BUILD_STAMP}" "${BETA_LABEL}" <<'RELNOTES'
import pathlib, sys

stage, ver, stamp, beta = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = f"""# CBG 报量对账 · 发布说明

**版本** v{ver}　**构建** {stamp}

---

## 这一版的变化

> **入口收成一个：只双击 `start.bat`。** 第一次会自动装依赖、问一句开机自启，
> 装完直接打开控制台 —— 不用再先 `install` 再 `start` 两趟。
> `install.bat` 还留着（只装依赖），日常用不到。
>
> **v2.1.1：修一个「把测试机卡住」的问题** —— 装过 beta 测试包的电脑
> 看不到正式版的更新提醒。
>
> **v2.1.0：四池对账 —— 玲珑和云商两边的账，一次对清。**
>
> * **已经在用 2.0.1 的电脑**：**看下面这一节就够了**，覆盖升级就行。
> * **从 1.x（1.6.1 及更早）升上来的**：这一节看完，
>   **再下面 2.0.0 那几节也要看** —— 里面有几件必须做的事
>   （尤其「定时任务改名字了，升级后请删掉旧的那条」）。
>   升级后第一次打开控制台会把这些弹给你，
>   大版本升级的话还会**推到邮箱/企业微信**，不用怕漏掉。

### v2.1.1 改了什么

**装过测试版（beta）的电脑，现在能自己升回正式版了。**

之前有个坑：beta 包和正式包的**版本号是一样的**
（beta 只表示「这一版正在测」，不是另一个版本）——
所以正式版发出来之后，程序一比「新版本号没有比我这台大」，
就认定已经是最新，**再也不会提示你升级**，那台机器就**卡在测试版上**了。

现在改成：**只要这台电脑跑的是测试包、而线上有同一版本的正式版，
就会提示「有新版本」**，点一下就能换成正式版。

⚠ 只有**主动装过 beta 测试包**的电脑跟这条有关；
一直用正式版的电脑一切照旧，不会有任何变化。

### v2.1.0 改了什么

**1. 新功能：四池对账。** 把四个数据摊在一起比 ——
玲珑（华为那个系统）的销售单和在库、云商的销售单和在库。
正常一个串号应该**同时在两边**；只在一边的，就是漏了：

* **玲珑报了、云商没报** —— 华为那边已经算卖了，云商库里还挂着这台货，
  **云商该出库没出**。
* **云商报了、玲珑没报** —— 云商已经卖了，玲珑还挂在店里，
  **门店该报量没报**。

两条各出一张清单，**每一条都能照着处理**：带串号 / 机型 / 门店 / 单号 /
时间 / 金额，出成 Excel（邮件和企微都带附件），
拿着单号就能去系统里找到那一单。
控制台第一个标签页从「报量排查」改成了「**四池比对**」，
能翻最近一个月的记录。

**2. 同一个串号不会每天重复强调。** 第一次出现标 `★ 新`，
之后再出现就弱化，写上「已推 N 次，首次 X 月 X 日」。
批发单那种（云商先报、玲珑过后才报）以前每天都当新的报，看两天就麻木了。
不想再提醒某一条：到「设置 → 出问题了？ → 清除推送记忆」。

**3. 「报量排查」整步下线了。** 它原来那个判据经实测**站不住**，
现在并进「四池对账」。所以：

* 「运行」页**只剩「整个项目」一个按钮**了。
  原来的「目标日」「高级（时间窗容差）」和分开执行的按钮都撤了 ——
  那几项**选什么都不影响结果**，留着只会让人以为「选今天就是只算今天」。
* 「设置」里「上报 bug」「清除推送记忆」挪到了「**检查更新**」下面
  （它们跟"每天几点跑"本来就没关系）。

**4. ⚠ 定时任务：「抓四池数据」不能取消了。**
它现在**每次都跑**，设置里那个勾去掉了 ——
不抓的话本地数据永远是旧的，POS 和四池对账都只是拿旧数据在算，
而界面上只会显示「跑完了」。
「POS 合规」「四池对账」两项照旧可以勾；**两项都不勾也允许**，
那就是「每天只抓数据，不算也不推」。

**5. ⚠ 升级后第一次跑会慢，「POS 合规」页会短暂空一阵。**
这一版会把老的订单库（`out` 下面那个 `cbg-<年>.db`）**改个名归档**，
然后**重新抓一遍今年的数据**：

* **老的库没有删**，只是改成了 `cbg-<年>.db.bak-2.1.0-<时间>`；
* 重抓要**几分钟**（云商销售单要分段拉），这段时间**别关窗口**；
* 在抓完之前，「POS 合规」页是**空的** —— 抓完就有了；
* 万一要退回老库：把那个 `.bak-2.1.0-…` 文件的名字改回 `cbg-<年>.db` 即可。

**6. 如果表里有老名字的定时任务**（`CBG报量对账-…`），
更新后第一次打开控制台会**主动弹一个框**帮你一键换掉 ——
Windows 上不同名就是**并存**，不处理的话两条会各自每天跑一遍。

---

> **下面是 2.0.1 / 2.0.0 的变化** —— **已经在用 2.0.1 的电脑可以跳过**；
> 从 1.x 升上来的必须看。

**一、以后更新不会再漏掉「要做什么」（v2.0.1）。**

* 每次更新后**第一次打开控制台**，会弹一个窗：这一版改了什么、
  **你需要做什么**。以前只有一本发布说明，没人会去翻。
* 检测到**大版本升级**（1.x → 2.x）时，这份提醒会**推到邮箱和企业微信** ——
  不用打开控制台也看得到。
* 「设置 → 检查更新」下面多了一行**升级记录**：什么时候从哪一版升上来的。
* ⚠ **装过 beta 测试包的电脑**：beta 的版本号跟正式版**同号**，
  程序自己认不出来 —— **这次更新会把它自动换成正式版**。

---

> **下面是 v2.0.0 带来的变化（从 1.x 升上来的必看）。**
>
> **v2.0.0 的主线：多了一个「POS 合规」标签页，程序开始在本地存一份订单数据。**
>
> * **从 v1.6.0 或更早升上来的**：第十节那几条（Win7 补丁、管理员权限、
>   验证码提示）仍然适用。
>
> ⚠ **这一版改了两个叫法**（店里原来的说法容易读歪）：
>
> | 以前叫 | 现在叫 | 意思 |
> |---|---|---|
> | 未报量 | **玲珑无但云商有** | 云商卖了、玲珑（华为那个系统）里没有 |
> | 调拨货查无出库 | **玲珑有但云商无** | 玲珑报了量、云商里查不到出库 |

**二、控制台多了一个「POS 合规」标签页。**

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

**三、「运行」页现在有四个按钮，控制台第一个标签页改叫「报量排查」。**

| 按钮 | 干什么 | 大概多久 |
|---|---|---|
| **整个项目** | 抓华为数据 → 报量排查 → POS 合规 | 最久（抓数据占大头） |
| **抓华为数据** | 只把订单拉回来存进本地 | 一两分钟起（首次补历史更久） |
| **报量排查** | 只做对账（用本地已有的数据） | 跟以前差不多 |
| **POS 合规** | 只算 POS（用本地已有的数据） | 几秒 |

* 单独执行的按钮**不会顺手抓数据** —— 想快就分开点。
* 想早上先看 POS、晚上再跑全套，就分开点，互不影响。
* 「抓华为数据」发现本地还没有数据时，会**自动把全部历史补一遍**。
* ⚠ 页面上那个「**目标日**」**只影响「报量排查」**：
  「抓华为数据」固定抓**当月**，「POS 合规」按**整月**算，都不看这个日期。
  （下面的「高级 · 时间窗容差」也一样只影响报量排查。）

**四、程序开始在本地存一份订单数据。**

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

**五、每天那次定时对账，现在跑三步。**

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

**六、华为会话过期时，程序会自己续一次。**

以前是「对账时顺手续」，现在挪到了第 1 步（抓数据之前）——
位置才对：**华为只在这一步被登录**。
续期是**无头**的，**不会弹浏览器窗口**；**失败也不会覆盖你手上那份好的会话**。
续不上才会报错，并让你手动登录一次。

**七、推送分成两条了；定时任务跑什么，设置里自己勾。**

* **报量排查**一条、**POS 合规**一条 —— 一条消息只讲一件事。
  报量排查是「今天有哪几台要赶紧补报」，POS 是「这个月做到多少」，
  混在一条里前者那份紧迫感会被后者的表格冲掉。
* POS 那条**不会 @所有人**（它是月度成绩，不是今天要干的活；
  每天 @ 会被屏蔽，连带把真正要紧的报量排查也一起屏蔽掉）。
* 到「设置 → 定时执行 → **自动化跑什么**」勾**三件事**：
  **抓华为数据**（默认勾着）/ **报量排查** / **POS 合规**。
  ⚠ **一件都不勾会被拒绝**（不是静默当成"都跑"）。
  ⚠ **「抓华为数据」建议一直勾着** —— 不勾的话本地数据不会更新，
  报量排查会以「库不新鲜」失败，POS 也只是拿旧数据在算。
* 改这个**不用重新注册定时任务，也不会弹 UAC** ——
  计划任务跑的是 `run.bat`，改的是它的内容，任务本身没变。
* 上面那个复选框列表**就是它到底跑什么** —— 和列表里那列「跑什么」完全一致。
  （「运行」页那四个按钮是**预设**：整个项目 / 抓华为数据 / 报量排查 / POS 合规，
  你在旁边看着，点"只算 POS"就真的只算 POS，快。）

**八、定时任务改名字了 —— 升级后请删掉旧的那条。**

| | |
|---|---|
| 以前叫 | `CBG报量对账-21点00` |
| 现在叫 | **`门店数据拉取与计算-21点00`** |

* 为什么改：它现在每天干三件事（抓数据 → 报量排查 → POS 合规），
  还叫"报量对账"就名不副实了 —— 在任务计划程序里看到一个叫"报量对账"的任务，
  不会想到它还管 POS。
* ⚠ **Windows 那边不同名就是并存，不是覆盖** —— 升级后你会有**两条**任务，
  **两条都会每天跑一遍**，等于一天跑两遍。
  「设置 → 定时执行」那里会**红字提示**哪几条是老名字的，
  点那一行的「删除」删掉旧的就对了（**留一条就够**）。
* 列表里的列也调了：以前那列叫「**跑什么**」但放的是命令全文（含路径），
  现在改名「**路径**」，另起一列「**跑什么**」写清楚每天实际干哪几件事。

**九、多了一个「上报 bug」按钮（在「设置 → 定时执行」下面）。**

* 出问题的时候点一下，它会**把现场打包**：
  执行日志 / 定时任务 / 门店配置 / 环境（版本、Python、系统、依赖）。
  然后**试着**发到邮箱和企业微信。
* ⚠ **包里没有任何能拿去登录的凭据** —— `.secrets\\` 整个不进包
  （云商账号、华为会话、邮箱授权码、企微 webhook、浏览器登录态全都不在里面）。
  **但有业务数据**（门店名 / 串号 / 金额），发之前确认收件人是自己人。
* ⚠ **发不出去也不影响**：包会留在 `out\report-bug-时间.zip`，
  界面上会把**路径**写出来，直接把这个文件发出去就行。
  **「推送失败」本身就是最常见的 bug —— 用它来上报它自己当然也不行**，
  所以这一步是「先落盘、再顺手发」，不依赖任何一条推送通道。

**十、如果你是从 v1.6.0 或更早升上来的**

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
- 双击 `start.bat` 时如果提示找不到 Python，就是这一步没做
- ⚠ **Windows 7 的电脑 —— 顺序不能反**：
  1. **先打 KB2533623 补丁**，再装 Python（没打的话装了也跑不起来）
  2. Python **只能装 3.8.10** —— 3.9 以上在 Win7 上装不上。
     到 <https://www.python.org/downloads/release/python-3810/>
     下 **Windows installer (64-bit)**
- ⚠ Win7 上**看不到版本提示也正常**：`.bat` 里故意不写版本号，
  双击之后由程序按系统告诉你该装哪个

**2. 这台电脑是哪个店**

- 包里的门店配置**是空的** —— 双击 `start.bat` 时会自动建一份
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

## 安装（三步）

1. 解压到**一个不会被挪走的目录**，比如 `D:\\cbg-reconcile`
   - 路径里**别带空格**，也别放桌面（用户目录名可能带空格）
2. 双击 `start.bat` —— **一个入口就够**
   - 第一次会**自动装依赖**，问一句要不要开机自启（**回车即开**），装完**自动打开控制台**
   - **不需要管理员权限，不会弹 UAC**
   - 开机自启用的是当前用户的注册表启动项，普通权限就够
3. 在控制台里依次配：**门店那三行** → 云商账号 → 邮件 / 企微
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
| `start.bat` / `stop.bat` | **日常唯一入口**：起 / 停后台服务（首次会自动装依赖） |
| `install.bat` | 只装依赖（一般用不到，`start.bat` 会顺带装） |
| `selftest.bat` | 逐项自检（第 0 节打印版本和 Python 版本） |
| `diagnose.bat` | 出问题时一键收集信息 |
| `uninstall.bat` | 卸载（**默认不删**报告和凭据） |
| `门店操作手册.md` | 门店操作手册（**五六步，有问题先翻这个**） |
| `发布说明.md` | 就是本文件 |
| `config\\stores.yaml` | 14 家店的映射表（**程序数据**，随版本更新） |

> ⚠ **`.secrets\\`、`out\\`、`config\\store-*.yaml` 这三样不在包里** ——
> 它们是这台电脑自己的东西（云商账号、历史报告、门店配置），
> **双击 `start.bat` 时会自动建好**（只建缺的，绝不覆盖已有的）。
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
        f"> ⚠️ **这是测试包（{beta}），不是正式版。** 只用来验证改动，"
        "别长期留在门店电脑上。\n\n---", 1)
    body = body.replace(f"v{ver} · {stamp}",
                        f"v{ver} · {beta} · {stamp}（测试包会显示 {beta}）")
pathlib.Path(stage, "发布说明.md").write_text(body, encoding="utf-8")
print("    ✓ 发布说明.md（v%s%s）" % (ver, (" · " + beta) if beta else ""))
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
check_absent "${STAGE}/tests"
# ⚠ 开发期「数字怎么算的」逆推稿 / 源表文件名 **不进正式包**（用户 2026-09-22）：
#   正式包只带运行时代码 + 门店手册；口径分析留在仓库 `.dsh/`（上面已整目录排除）。
#   只查**增值口径逆推**相关禁词；别误伤其它模块里无害的 `.dsh/docs/…` 设计指针。
if grep -R -I -n -E '汇机保数据统计|9月钢化膜数据目标|口径倒推|无忧会员权益-开发目标' \
    "${STAGE}/src" "${STAGE}/web" "${STAGE}/config" 2>/dev/null \
    | grep -v '开发逆推稿不进正式包' | head -20; then
  echo "    ✗ 包内出现开发逆推/源表文件名文案（正式包不要带这些）"
  fail=1
fi
for _junk_xlsx in "${STAGE}"/*汇机保* "${STAGE}"/src/**/*汇机保* \
                   "${STAGE}"/*钢化膜数据目标*; do
  [ -e "${_junk_xlsx}" ] || continue
  echo "    ✗ 包内混进源表 Excel：${_junk_xlsx#${STAGE}/}"
  fail=1
done
# 这几个目录是「这台电脑自己的东西」，进包 = 手工拷贝时冲掉门店设置
# ⚠ `in/`（2026-09-21 晚加）= **收进来的**东西（各店发来的上报包 / 收信库）——
#   跟 `out/` 一样，而且它里面是**别家店的业务数据**，更不该进包。
check_absent "${STAGE}/.secrets"
check_absent "${STAGE}/out"
check_absent "${STAGE}/in"
check_absent "${STAGE}/run.sh"
check_absent "${STAGE}/dist"
[ -f "${STAGE}/src/cli.py" ]      || { echo "    ✗ 缺 src/cli.py"; fail=1; }
[ -f "${STAGE}/web/index.html" ]  || { echo "    ✗ 缺 web/index.html"; fail=1; }
# ⚠ 库存盘点那一页（M16）**必须整份进包**：`web/inventory.html` 是壳，
#   五个 js + 一个 css 在 `web/inventory/` 下 —— 少任何一个，
#   门店点开「库存盘点」看到的是白板，而 Python 测试全绿。
#   （它**不是构建产物**：没有 `build.mjs`，六个文件照原样发，见该页头部注释。）
for _inv in inventory.html inventory/core.js inventory/api.js inventory/store.js \
            inventory/xlsx.js inventory/ui.js inventory/style.css \
            tools/price-tag/index.html tools/price-tag/js/app.js \
            tools/price-tag/css/style.css; do
  [ -f "${STAGE}/web/${_inv}" ] || { echo "    ✗ 缺 web/${_inv}（库存/小工具前端）"; fail=1; }
done
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
