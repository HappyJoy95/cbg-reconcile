#!/usr/bin/env bash
# 打一个 **exe 安装包**（onedir：一个文件夹 + 一个 cbg-reconcile.exe）。
#
#   bash tools/build_exe.sh
#
# ⚠ **只能在 Windows 上跑** —— PyInstaller 不支持交叉编译，所以这一步
#   放在 GitHub Actions 的 `windows-2022` 里（见 `.github/workflows/build-exe.yml`）。
#   本机（macOS）想验证打包配置，跑它也行：出来的 macOS 二进制**跑不了门店的活**，
#   但能验依赖齐不齐、数据文件全不全、冻结后的路径对不对。
#
# 产出（dist-exe/ 下）：
#   cbg-reconcile/                       安装目录（直接覆盖到 D:\cbg-reconcile 即可）
#   cbg-reconcile-v<版本>-<日期>.zip      发给门店的包
#   cbg-reconcile-v<版本>-<日期>.sha256   校验和
#
# 和 `tools/build_package.sh`（源码包）的区别：
#   * 源码编进 exe —— 包里**读不到 .py**；
#   * **不注入密钥**（central-mail.env / mail-key.json）——
#     那两样是"随大版本安装包走"的，走的是 build_package.sh 那条私发的路；
#     exe 包要往哪发、发之前要不要补密钥，由人决定，脚本不替你塞。
#   * 不带 `tests/`、`tools/`、`.secrets/`、`config/store-*.yaml`。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/dist-exe"
WORK="${OUT}/build"
STAMP="$(date +%Y%m%d)"
PY="${PYTHON:-python3}"
command -v "${PY}" >/dev/null 2>&1 || PY=python

VER="$("${PY}" - "$ROOT/src/version.py" <<'PYV'
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'^VERSION\s*=\s*"([^"]+)"', t, re.M)
if not m:
    sys.exit("读不出版本号（src/version.py）")
print(m.group(1))
PYV
)"
[ -n "${VER}" ] || { echo "✗ 读不出版本号"; exit 1; }

ZIPNAME="cbg-reconcile-v${VER}-${STAMP}.zip"
echo "==> exe 包 v${VER}  （${STAMP}）"

# ---------------------------------------------------------------- 构建指纹
# ⚠ **写到产物目录，不写仓库根** —— 仓库根那个 `BUILD.txt` 是"**这台安装**
#   是什么时候打的"，写到开发仓里会把 `version.build_id()` 的指纹污染成
#   今天的日期（`git main@…` 就看不见了），而且 `dbmigrate` 那道
#   「不是发版包就别动数据库」的门槛会**误判成发版包** ——
#   2026-10-02 实测踩到：本机一跑 build_exe.sh，全量测试当场红一条。
#   spec 从 `CBG_BUILD_FILE` 读它；CI 上没有这个变量就退回仓库根（干净 checkout）。
printf '%s\n' "$(date '+%Y-%m-%d %H:%M')" > "${OUT}/BUILD.txt"
export CBG_BUILD_FILE="${OUT}/BUILD.txt"
echo "    构建指纹：$(cat "${OUT}/BUILD.txt")"

# ---------------------------------------------------------------- 打包
rm -rf "${OUT}/cbg-reconcile" "${WORK}"
mkdir -p "${OUT}"
echo "==> PyInstaller（onedir）"
"${PY}" -m PyInstaller --noconfirm --clean \
  --distpath "${OUT}" --workpath "${WORK}" \
  "${ROOT}/tools/exe.spec"

DIST="${OUT}/cbg-reconcile"
EXE="${DIST}/cbg-reconcile.exe"
[ -f "${EXE}" ] || EXE="${DIST}/cbg-reconcile"     # 非 Windows 上试跑时没有 .exe

# ---------------------------------------------------------------- 自检
echo "==> 打包后自检"
fail=0
# ⚠ 用 `-f` 不用 `-e`：`-e` 认目录 —— 上一版就因此把"一个叫 BUILD.txt 的目录"
#   当成指纹通过了，而 `version.build_id()` 按文件读，读不出来。
check() { if [ -f "$2" ]; then echo "    ✓ $1"; else echo "    ✗ 缺 $1（$2）"; fail=1; fi; }
check "可执行入口"            "${EXE}"
check "控制台前端"            "${DIST}/web/index.html"
check "库存盘点那一页"         "${DIST}/web/inventory.html"
check "门店映射表"            "${DIST}/config/stores.yaml"
check "门店配置模板"           "${DIST}/src/store-config.default.yaml"
check "构建指纹"              "${DIST}/BUILD.txt"
if [ -d "${DIST}/BUILD.txt" ]; then
  echo "    ✗ BUILD.txt 是个**目录** —— spec 里 datas 的第二项写错了"
  fail=1
fi

# 这些一个都不许出现 —— 出现就说明打包配置漏了排除
for bad in tests .secrets out in dist dist-exe tools .git; do
  if [ -e "${DIST}/${bad}" ]; then
    echo "    ✗ 不该进包：${bad}"; fail=1
  fi
done
# 门店自己的配置（CI 上是干净 checkout，本来就没有 —— 这道是防本机试跑带上去）
_stray="$(ls "${DIST}"/config/store-*.yaml 2>/dev/null || true)"
if [ -n "${_stray}" ]; then
  echo "    ✗ 混进了门店配置："; echo "${_stray}" | sed 's/^/       /'; fail=1
fi
# 包里不许有 .py 源码（这是 exe 包存在的全部意义）
_py="$(find "${DIST}" -name '*.py' 2>/dev/null | head -5 || true)"
if [ -n "${_py}" ]; then
  echo "    ✗ 包里有 .py 源码 —— 这跟『读不到源码』的目标直接冲突："
  echo "${_py}" | sed 's/^/       /'
  fail=1
fi
[ "${fail}" = "0" ] || { echo "打包中止"; exit 1; }
echo "    ✓ 全部通过"

# ---------------------------------------------------------------- 压缩
# ⚠ 不能用命令行 zip：macOS/部分环境的 zip 不设 UTF-8 标志位，
#   中文文件名（门店操作手册.md、发布说明.md）解到 Windows 上会乱码。
echo "==> 压缩 ${ZIPNAME}"
rm -f "${OUT}/${ZIPNAME}" "${OUT}/${ZIPNAME%.zip}.sha256"
"${PY}" - "${OUT}" "${ZIPNAME}" <<'PYZIP'
import hashlib, os, pathlib, sys, zipfile

out, name = sys.argv[1], sys.argv[2]
base = pathlib.Path(out) / "cbg-reconcile"
n = 0
with zipfile.ZipFile(pathlib.Path(out) / name, "w",
                     zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f == ".DS_Store":
                continue
            p = pathlib.Path(root) / f
            z.write(p, p.relative_to(base.parent).as_posix())
            n += 1
print(f"    ✓ 写入 {n} 个文件")

data = (pathlib.Path(out) / name).read_bytes()
(pathlib.Path(out) / (name[:-4] + ".sha256")).write_text(
    hashlib.sha256(data).hexdigest() + "  " + name + "\n", encoding="utf-8")

# 中文文件名的 UTF-8 标志位 —— 不设就是 Windows 上一片乱码
with zipfile.ZipFile(pathlib.Path(out) / name) as z:
    bad = [i.filename for i in z.infolist()
           if any(ord(c) > 127 for c in i.filename) and not (i.flag_bits & 0x800)]
    if bad:
        sys.exit("    ✗ 中文文件名没设 UTF-8 标志位：" + ", ".join(bad[:3]))
    print("    ✓ 中文文件名 UTF-8 标志位正常")
PYZIP

echo
echo "==> 完成"
echo "    目录：${DIST}"
echo "    包：  ${OUT}/${ZIPNAME}"
echo "    sha256：$(cut -d' ' -f1 "${OUT}/${ZIPNAME%.zip}.sha256")"
echo "    大小：$(du -sh "${DIST}" 2>/dev/null | cut -f1)"
