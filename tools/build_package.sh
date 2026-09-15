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

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
NAME="cbg-reconcile"                              # 包内顶层目录用 ASCII —— 少一层乱码风险
# 版本号从代码里读，别手写 —— 手写迟早跟 version.py 对不上
VER="$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/version.py")"
[ -n "${VER}" ] || { echo "读不出版本号（src/version.py）"; exit 1; }
ZIPNAME="cbg-reconcile-v${VER}-${STAMP}.zip"
DIST="${ROOT}/dist"
STAGE_ROOT="$(mktemp -d)"
STAGE="${STAGE_ROOT}/${NAME}"

echo "==> 打包 ${ZIPNAME}"
mkdir -p "${STAGE}" "${DIST}"

# ---------------------------------------------------------------- 复制源码
rsync -a \
  --exclude '.secrets/' \
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
  "${ROOT}/" "${STAGE}/"

# ---------------------------------------------------------------- 凭据
# ⚠ **包里一个真凭据都不带**。
#
# 以前这里会把开发机 ~/.dsh/secrets/erp.env 整个拷进去（含账号、密码，
# 甚至还有一条**还活着的 ERP_TOKEN**）。发到门店等于把公司账号抄送一遍；
# 而且这个项目要推到公开仓库，更不能有任何默认可用的凭据。
#
# 现在只放一份**空模板**：键名齐、值全空、注释说明去哪填。
# 门店在控制台「设置 → 云商账号」里填一次，会写进这份文件。
mkdir -p "${STAGE}/.secrets"
cat > "${STAGE}/.secrets/erp.env" <<'EOF'
# 盛联 ERP（云商）凭据 —— **本机专用，不要外传，也不要提交到 git**
#
# 怎么填（二选一）：
#   1. 打开控制台「设置 → 云商账号」，填账号 / 密码 / 公司代码，点「测试登录」
#      —— 这是推荐做法：会当场验证账号对不对，通过了才写进这个文件
#   2. 直接编辑本文件（键名不要改）
#
# ERP_TOKEN 不用手填，登录成功后自动写进来。
#
# 优先级：环境变量 > 这个文件 > ~/.dsh/secrets/erp.env

ERP_USERNAME=
ERP_PASSWORD=
ERP_COMPANY_CODE=

# 登录成功后自动填，不用手写
ERP_TOKEN=
EOF
echo "    ✓ 云商凭据：空的（门店在「设置 → 云商账号」里填，会当场验证）"

# 邮箱授权码 / 企微 webhook 同样不带 —— 到门店在界面上填（不回显）。
echo "    · 邮箱授权码、企微 webhook 默认不带（到门店「设置」里填）"
cat > "${STAGE}/.secrets/README.txt" <<'EOF'
这个目录放凭据，不要外传。

  erp.env                云商账号密码
  huawei.env             华为账号密码（自动登录用，界面上填）
  mail.env               邮箱 SMTP 授权码（界面上填）
  wecom.env              企业微信群机器人 webhook（界面上填）
  cbg-<门店码>.json      华为会话（在本机「会话」页登录后自动生成）
  session-check.json     上次会话自检的时间/结果（界面「当前会话」显示的那个）
  browser-profile/       浏览器 profile（登录态，自动续期靠它）
EOF

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
BUILD_STAMP="$(date '+%Y-%m-%d %H:%M')"
printf '%s\n' "${BUILD_STAMP}" > "${STAGE}/BUILD.txt"
echo "==> 构建指纹：${BUILD_STAMP}"

# ---------------------------------------------------------------- 发布说明
# ⚠ 用 Python 写而不是 heredoc：正文里有大量反引号（`install.bat` 这种），
#   不带引号的 heredoc 会把它们当**命令替换**执行掉（真踩了）。
python3 - "${STAGE}" "${VER}" "${BUILD_STAMP}" <<'RELNOTES'
import pathlib, sys

stage, ver, stamp = sys.argv[1], sys.argv[2], sys.argv[3]
body = f"""# CBG 报量对账 · 发布说明

**版本** v{ver}　**构建** {stamp}

---

## 这是什么

比对「云商里卖的、归属本店的串号」有没有都报量给华为，没报的列成差异清单。
装在**门店自己的电脑**上，每天定时跑一次，结果推邮件 / 企业微信。

## 装之前先确认三件事

**1. 这台电脑装了 Python 3.14**（3.9 以上都行）

- 装的时候**必须勾上** `Add python.exe to PATH`
- 双击 `install.bat` 时如果提示找不到 Python，就是这一步没做

**2. 这台电脑是哪个店**

- 包里的 `config/store-SCN231409.yaml` 是**青岛新业广场店**的配置
- 不是这家店的话，**启动后在控制台「设置 → 门店」改那三行**就行
  （`store_code` / `marker` / `erp_store_name`）—— 改完**立刻生效，不用重启**
  - 想装之前就改也可以，直接编辑 `config/store-SCN231409.yaml`，那三行有注释
- ⚠ **改了门店要重新登录华为**：会话文件是按店存的
  （`.secrets/cbg-<门店码>.json`），换店等于换了一份会话 ——
  到「会话」页用**那个店的账号**重新抓一次
- ⚠ 忘了改的话，会对到**别的店**的账上去，而且看起来一切正常。
  跑一次 `selftest.bat`，第 0 节会打印当前配的是哪个店

**3. 华为账号**

每个店用自己的账号。会话是按店的，拿 A 店的会话去查 B 店会报"没有权限"。

## 安装（四步）

1. 解压到**一个不会被挪走的目录**，比如 `D:\\cbg-reconcile`
   - 路径里**别带空格**，也别放桌面（用户目录名可能带空格）
2. 双击 `install.bat` —— 装依赖，然后问一句要不要开机自启（**回车即开**）
   - 会弹**一次** UAC（注册"以管理员身份启动"需要），之后每次开机都不再弹
3. 双击 `start.bat` —— 打开控制台
4. 在控制台里依次配：**门店那三行** → 云商账号 → 邮件 / 企微
   → 会话页抓一次登录 → 设置页加定时任务（建议设在**关门前**，比如 21:00）

   > 顺序不重要，**改完都立刻生效、不用重启**。只有一条：
   > **先把门店配对，再加定时任务** —— 不然到点那一跑会对到别的店账上去。

完整步骤看 **`安装部署指南.md`**。

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
| `安装部署指南.md` | 门店操作手册（**中文，先看这个**） |
| `发布说明.md` | 就是本文件 |
| `设计文档.md` | 技术设计，给维护的人看 |
| `config\\` | 门店配置（**记得改成自己店**） |
| `.secrets\\` | 凭据（云商账号等） |
| `out\\` | 跑出来的报告和日志 |

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
pathlib.Path(stage, "发布说明.md").write_text(body, encoding="utf-8")
print(f"    ✓ 发布说明.md（v{ver}）")
RELNOTES


# ---------------------------------------------------------------- 空目录
# out/ 被 gitignore 排除了，但门店电脑上要有个现成的（报告、日志都写这儿）
mkdir -p "${STAGE}/out"
cat > "${STAGE}/out/README.txt" <<'EOF'
对账产出的差异清单、运行日志都写在这个目录。

  差异_<日期>_<门店码>.xlsx    差异清单（每次跑都生成，没差异也生成）
  差异_<日期>_<门店码>.json    给控制台看的摘要
  run.log                      计划任务跑的日志（如果配了）
  autostart.log                开机自启的日志（如果有报错）
EOF

# ---------------------------------------------------------------- 指南与文档
# 仓库布局 == 安装布局，直接平铺（不再有 packaging/ 中间层）
cp "${ROOT}/安装部署指南.md" "${STAGE}/安装部署指南.md"
if [ -f "${ROOT}/../docs/superpowers/specs/2026-09-15-cbg-reconcile-design.md" ]; then
  # ⚠ 设计文档里带着开发机的绝对路径 —— 洗掉再发，别把作者的家目录带到门店
  sed 's#/Users/ashui/Documents/ds-chat/cbg-reconcile#<项目目录>#g; s#/Users/ashui#<开发机>#g' \
    "${ROOT}/../docs/superpowers/specs/2026-09-15-cbg-reconcile-design.md" \
    > "${STAGE}/设计文档.md"
fi
# 仓库里的 README 指向仓库内的 docs/，包里改成平级
python3 - "$STAGE/README.md" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
if p.exists():
    t = p.read_text(encoding="utf-8")
    t = t.replace("../docs/superpowers/specs/2026-09-15-cbg-reconcile-design.md", "设计文档.md")
    t = t.replace("门店测试指南.md", "安装部署指南.md")
    p.write_text(t, encoding="utf-8")
PY

# ---------------------------------------------------------------- 自检
echo "==> 打包前自检"
fail=0
check_absent() {
  if [ -e "$1" ]; then echo "    ✗ 不该出现：${1#${STAGE}/}"; fail=1; fi
}
check_absent "${STAGE}/.secrets/cbg-SCN231409.json"
check_absent "${STAGE}/.secrets/browser-profile"
check_absent "${STAGE}/.secrets/curl.txt"
check_absent "${STAGE}/run.sh"
check_absent "${STAGE}/dist"
# out/ 要有个空壳（报告写这儿），但**不能带真实数据过去**
if [ -d "${STAGE}/out" ]; then
  stray=$(find "${STAGE}/out" -type f ! -name 'README.txt' | head -3)
  if [ -n "${stray}" ]; then
    echo "    ✗ out/ 里带了真实数据："
    echo "${stray}" | sed 's/^/        /'
    fail=1
  fi
else
  echo "    ✗ 缺 out/ 目录"
  fail=1
fi
[ -f "${STAGE}/src/cli.py" ]      || { echo "    ✗ 缺 src/cli.py"; fail=1; }
[ -f "${STAGE}/web/index.html" ]  || { echo "    ✗ 缺 web/index.html"; fail=1; }
[ -f "${STAGE}/config/stores.yaml" ] || { echo "    ✗ 缺 config/stores.yaml"; fail=1; }
[ -f "${STAGE}/install.bat" ]     || { echo "    ✗ 缺 install.bat"; fail=1; }
[ -f "${STAGE}/start.bat" ]       || { echo "    ✗ 缺 start.bat"; fail=1; }
[ -f "${STAGE}/stop.bat" ]        || { echo "    ✗ 缺 stop.bat"; fail=1; }
[ -f "${STAGE}/boot.py" ]         || { echo "    ✗ 缺 boot.py（开机自启要用）"; fail=1; }
[ -f "${STAGE}/bootstrap.py" ]    || { echo "    ✗ 缺 bootstrap.py（所有 bat 都靠它）"; fail=1; }
[ -f "${STAGE}/uninstall.bat" ]   || { echo "    ✗ 缺 uninstall.bat"; fail=1; }
[ -f "${STAGE}/diagnose.bat" ]    || { echo "    ✗ 缺 diagnose.bat"; fail=1; }
[ -f "${STAGE}/run_check.py" ]    || { echo "    ✗ 缺 run_check.py（计划任务靠它记日志）"; fail=1; }
[ -f "${STAGE}/安装部署指南.md" ]  || { echo "    ✗ 缺 安装部署指南.md"; fail=1; }
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
# 包里不能有任何本机绝对路径
if grep -rIl "/Users/ashui" "${STAGE}" 2>/dev/null | grep -v '设计文档.md' | head -3 | grep -q .; then
  echo "    ✗ 有文件残留本机绝对路径："; grep -rIl "/Users/ashui" "${STAGE}" | grep -v '设计文档.md' | head -3
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
# 凭据目录只许放这两样，别把会话/浏览器 profile 带出去
if [ -d "${STAGE}/.secrets" ]; then
  # ⚠ `grep` 筛完没剩东西时返回 1，配 `set -e` 会把整个脚本干掉 —— 必须 || true
  extra=$(ls -A "${STAGE}/.secrets" | grep -vE '^(README\.txt|erp\.env)$' | head -5 || true)
  if [ -n "$extra" ]; then
    echo "    ✗ .secrets/ 里混进了别的文件（会话 / 浏览器登录态不能外发）："
    echo "$extra" | sed 's/^/       /'
    fail=1
  fi
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
