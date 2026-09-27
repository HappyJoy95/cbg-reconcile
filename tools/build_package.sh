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
if command -v git >/dev/null 2>&1 && git -C "${ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  _dirty="$(git -C "${ROOT}" status --porcelain 2>/dev/null || true)"
  if [ -n "${_dirty}" ]; then
    echo "    ! 工作区不干净 —— 打出来的包会跟远端仓库不一致："
    echo "${_dirty}" | sed 's/^/       /'
    if [ -n "${BETA_N}" ]; then
      echo "    beta 测试包可供门店手工安装测试；测试期间不要执行自更新。"
    else
      echo "    正式包先 commit（或 stash）再打；确要跳过就设 CBG_ALLOW_DIRTY=1。"
      [ "${CBG_ALLOW_DIRTY:-}" = "1" ] || exit 1
      echo "    （CBG_ALLOW_DIRTY=1，继续打包 —— 这份包只能自己测，别发门店）"
    fi
  fi
fi

# ---------------------------------------------------------------- 复制源码
# ───────────── 生活馆版：按 edition.PRUNE 追加排除（单源，python 读出来） ─────────────
# 一份表两处用：这里（打包 rsync 排除）+ 自更新 `selfupdate._targets()` ——
# 谁也不许另抄一张（抄的那份迟早跟 edition.PRUNE 走散，而"走散"的表现是
# 打包裁了、自更新又铺回来，只有装到门店机器上才看得出来）。
# ⚠ EDITION 缺失 / 读不出 / 内容不是 lifehall → 一律按**主包**跑
#   （跟 `src/edition.py` 同一个默认：宁可当主包，别半疯）。
# ⚠ 下面的 `${PRUNE_ARGS[@]+...}` 是 macOS bash 3.2 + `set -u` 的兼容写法：
#   空数组 `"${PRUNE_ARGS[@]}"` 直接展开会报 unbound variable。
if [ -f "${ROOT}/EDITION" ]; then
  EDITION_VAL="$(tr -d '[:space:]' < "${ROOT}/EDITION")"
else
  EDITION_VAL="full"
fi
PRUNE_ARGS=()
if [ "${EDITION_VAL}" = "lifehall" ]; then
  echo "==> 生活馆版（EDITION=lifehall）—— 按 src/edition.py::PRUNE 裁剪"
  while IFS= read -r p; do
    if [ -n "$p" ]; then
      PRUNE_ARGS+=(--exclude "$p")
    fi
  done < <(cd "${ROOT}" && python3 -c "from src.edition import PRUNE; print('\n'.join(PRUNE))")
fi
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
  --exclude '.git' \
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
  --exclude '/.playwright-cli/' \
  --exclude '/claim-guide-preview.html' \
  --exclude '/claim-guide.css' \
  --exclude '/claim-guide.js' \
  --exclude '*.xlsx' \
  ${PRUNE_ARGS[@]+"${PRUNE_ARGS[@]}"} \
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
if [ "${EDITION_VAL}" = "lifehall" ]; then
  echo "  · 生活馆版不使用邮件推送，跳过中台邮箱授权码"
elif grep -q '^MAIL_CENTRAL_PASSWORD=' "${CENTRAL_SRC}" 2>/dev/null; then
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
if [ "${EDITION_VAL}" = "lifehall" ]; then
  echo "  · 生活馆版不使用邮件附件，跳过邮件密钥"
elif [ -f "${KEY_SRC}" ]; then
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
# ⚠ 2026-09-23（2.2.1 起）：正文**住在仓库里**（根目录 `发布说明.md`），这里只拷进包。
#   原来整段正文嵌在本脚本的 Python heredoc 里生成 —— 而本脚本**不下发门店**，
#   于是走自更新升级的机器，`发布说明.md` 永远停在当初拷包那一版
#   （AGENTS「仓库 ≠ 包内容」那节原来就记着这个差；用户 2026-09-23 选定修掉）。
#   入了 git ⇒ zipball 带它 ⇒ `selfupdate._targets` 对根目录文件照原样铺 ⇒ 自更新零改动。
#   ⚠ 改发布说明 = 改根目录那个文件 + commit，**不再改本脚本**；
#     顺带 f-string 反斜杠转义坑随正文一起消失（原来那条
#     test_release_notes_python_has_no_invalid_escapes 已改钉新形状）。
# ⚠ 生活馆版**不带**这份文档（`edition.PRUNE` 单源）：rsync 已经把它排除了，
#   这里的 `cp` 不跟着跳过就等于又拷回来 —— 两道闸自相矛盾，最后靠下面那条
#   反查断言才发现。所以 cp / beta 横幅整段进 else。
if [ "${EDITION_VAL}" = "lifehall" ]; then
  echo "    ✓ 发布说明.md：生活馆版不带（edition.PRUNE —— 它讲的是被裁功能）"
else
cp "${ROOT}/发布说明.md" "${STAGE}/发布说明.md"

# beta 包在正文第一个 `---` 后插一条"这是测试包"的横幅（改 stage 副本，仓库文件不动）
if [ -n "${BETA_LABEL}" ]; then
  python3 - "${STAGE}" "${BETA_LABEL}" <<'BETABANNER'
import pathlib, sys
stage, beta = sys.argv[1], sys.argv[2]
p = pathlib.Path(stage, "发布说明.md")
body = p.read_text(encoding="utf-8")
banner = ("> ⚠️ **这是测试包（%s），不是正式版。** "
          "只用来验证改动，别长期留在门店电脑上。\n\n---" % beta)
body = body.replace("---", banner, 1)
p.write_text(body, encoding="utf-8")
BETABANNER
fi
echo "    ✓ 发布说明.md（来自仓库${BETA_LABEL:+ · ${BETA_LABEL}}）"
fi


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
# ⚠ 生活馆版同样不带它（`edition.PRUNE`，理由同上一段的 cp）。
if [ "${EDITION_VAL}" = "lifehall" ]; then
  echo "    ✓ 门店操作手册.md：生活馆版不带（edition.PRUNE —— 生活馆另发自己的指引）"
else
cp "${ROOT}/门店操作手册.md" "${STAGE}/门店操作手册.md"
fi

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
# ⚠ 生活馆版**没有这一页**（`edition.PRUNE` 裁掉）—— 所以"必须在"的断言
#   只在主包跑；生活馆侧由下面那条反查（它必须**不在**）守。
if [ "${EDITION_VAL}" != "lifehall" ]; then
  for _inv in inventory.html inventory/core.js inventory/api.js inventory/store.js \
              inventory/xlsx.js inventory/ui.js inventory/style.css; do
    [ -f "${STAGE}/web/${_inv}" ] || { echo "    ✗ 缺 web/${_inv}（库存前端）"; fail=1; }
  done
fi
# 小工具前端**两个版都要**（price-tag 不在 PRUNE 里，生活馆也保留那三个工具页）
for _inv in tools/price-tag/index.html tools/price-tag/js/app.js \
            tools/price-tag/css/style.css; do
  [ -f "${STAGE}/web/${_inv}" ] || { echo "    ✗ 缺 web/${_inv}（小工具前端）"; fail=1; }
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
# ───────── 按版反查（两道闸的第二道）─────────
# 生活馆包里被裁的东西必须真的**不在**（rsync 排除 + 反查断言，老规矩），
# EDITION 文件必须**在**（自更新和安装都靠它认版 —— 没有它，下一次自更新
# 会把这台机器当主包处理）。
# ⚠ 主包的原断言（手册 / 发布说明必须在）原样挪进 else —— 少了任何一条，
#   生活馆包会因为"缺 门店操作手册.md"必挂，而主包会因为没人查而漏发手册。
if [ "${EDITION_VAL}" = "lifehall" ]; then
  for _p in src/erp.py src/reconcile.py src/app/pos.py src/features/compliance \
            config/managers.yaml 门店操作手册.md 发布说明.md web/inventory.html; do
    if [ -e "${STAGE}/${_p}" ]; then
      echo "    ✗ 生活馆包里混进了该裁的文件：${_p}"
      fail=1
    fi
  done
  [ -f "${STAGE}/EDITION" ] || { echo "    ✗ 缺 EDITION 文件（自更新要靠它认版）"; fail=1; }
else
  [ -f "${STAGE}/门店操作手册.md" ] || { echo "    ✗ 缺 门店操作手册.md"; fail=1; }
  [ -f "${STAGE}/发布说明.md" ]      || { echo "    ✗ 缺 发布说明.md"; fail=1; }
fi
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
# ⚠ `*.xlsx` 也是垃圾：开发期源表（rsync 已排，这里再钉一道）。
for junk in '.pytest_cache' '__pycache__' 'dist' 'tools' 'packaging' \
            'run.sh' 'run.bat' 'run-now.sh' 'run-now.bat' '.gitignore' \
            '.playwright-cli' 'claim-guide-preview.html' 'claim-guide.css' \
            'claim-guide.js'; do
  if [ -e "${STAGE}/${junk}" ]; then
    echo "    ✗ 包里混进了开发文件：${junk}"
    fail=1
  fi
done
_stray_xlsx="$(find "${STAGE}" -name '*.xlsx' 2>/dev/null | head -5 || true)"
if [ -n "${_stray_xlsx}" ]; then
  echo "    ✗ 包里混进了源表 Excel："
  echo "${_stray_xlsx}" | sed 's/^/       /'
  fail=1
fi
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
