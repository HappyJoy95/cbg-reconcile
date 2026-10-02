#!/usr/bin/env bash
# 把 dist/ 里的包发到**发行仓**（GitHub Releases）。
#
#   bash tools/publish_release.sh                    # 发 dist/ 里最新的那个包
#   bash tools/build_package.sh && bash tools/publish_release.sh
#   bash tools/publish_release.sh dist/cbg-reconcile-v26.0929.191128-20260929.zip
#   bash tools/publish_release.sh --dry-run          # 只做校验，不上传
#   bash tools/publish_release.sh --notes "一句话"   # 顺带写进 CHANGELOG
#
# 发行仓只放四样东西：更新包（asset）、`VERSION`、CHANGELOG、用户文档。
# **源码仓的事它一概不管** —— 打包（写版本号）还是 `tools/build_package.sh`，
# 推源码仓还是人自己 commit + push（见 AGENTS.md 发版那节）。
#
# ─────────────────────────────────────────────────────────────────────────────
# ⚠⚠ **这个脚本对着的是一个公开仓库，两道硬闸挡密钥**：
#
#    包里不许有 `central-mail.env`（中台邮箱授权码）和 `mail-key.json`
#    （邮件附件加密密钥）。那两样只随**私发的安装包**走（用户 2026-10-02：
#    「安装包只私发」）—— 一旦进了公开 Release，附件加密当场归零，
#    中台邮箱也跟着泄露。而且**泄露一次就收不回来**（GitHub 的 release 资产
#    虽然能删，但 CDN 缓存和别人的 clone 里可能还在）。
#
#    第一道：本脚本把它们**剔出**将要上传的 zip；
#    第二道：剔完**反查** —— 资产里还有任何一个就直接失败，不上传。
#    （写法照抄 `build_package.sh` 里那几条 `check_absent` 的思路：
#      断言要在**动作之前**拦下来，事后发现就晚了。）
# ─────────────────────────────────────────────────────────────────────────────
#
# 发行仓地址**不在这儿写死** —— 从 `src/selfupdate.py` 读，客户端和发布脚本
# 同一个来源（那边写错了，这边跟着错，而不是"改了一处忘了另一处"）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ------------------------------------------------------------------ 参数
ZIP_IN=""
DRY_RUN=""
NOTES=""
NOTES_FILE=""
for arg in "$@"; do
  case "${arg}" in
    --dry-run)   DRY_RUN=1 ;;
    --notes=*)   NOTES="${arg#--notes=}" ;;
    --notes)     echo "用 --notes=\"一句话\" 或 --notes-file=路径"; exit 1 ;;
    --notes-file=*) NOTES_FILE="${arg#--notes-file=}" ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    -*)          echo "不认识的参数：${arg}（--dry-run / --notes=… / --notes-file=…）"; exit 1 ;;
    *)           ZIP_IN="${arg}" ;;
  esac
done

# ------------------------------------------------------- 从代码里读发行仓配置
RELEASE_REPO="$(sed -n 's/^RELEASE_REPO *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/selfupdate.py")"
RELEASE_ASSET="$(sed -n 's/^RELEASE_ASSET *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/selfupdate.py")"
RELEASE_ASSET_SEALED="$(sed -n 's/^RELEASE_ASSET_SEALED *= *"\([^"]*\)".*/\1/p' "${ROOT}/src/selfupdate.py")"
if [ -z "${RELEASE_REPO}" ] || [ -z "${RELEASE_ASSET}" ] || [ -z "${RELEASE_ASSET_SEALED}" ]; then
  echo "✗ 读不出发行仓配置（src/selfupdate.py 里的 RELEASE_REPO / RELEASE_ASSET*）"
  exit 1
fi
# 上传的**最终**资产名 —— 路线 A：只发密文（`.sealed`）。
# 明文那个名字（`RELEASE_ASSET`）留给过渡期，客户端 `_ASSET_ORDER` 两个都认。
ASSET="${RELEASE_ASSET_SEALED}"
SHA_NAME="${ASSET}.sha256"

# ------------------------------------------------------------------ 挑包
if [ -z "${ZIP_IN}" ]; then
  # 最新的一个（按修改时间）—— beta 和正式包都会被挑中，所以要打之前先看清楚
  ZIP_IN="$(ls -t "${ROOT}"/dist/cbg-reconcile-v*.zip 2>/dev/null | head -1 || true)"
fi
if [ -z "${ZIP_IN}" ] || [ ! -f "${ZIP_IN}" ]; then
  echo "✗ 找不到要发的包。先跑：bash tools/build_package.sh"
  echo "  （或把 zip 路径作为参数传进来）"
  exit 1
fi
ZIP_IN="$(cd "$(dirname "${ZIP_IN}")" && pwd)/$(basename "${ZIP_IN}")"

# cbg-reconcile-v<VER>[-betaN]-<日期>.zip → VER / beta
BASE="$(basename "${ZIP_IN}" .zip)"
TAIL="${BASE#cbg-reconcile-v}"        # 26.0929.191128-20260929  /  2.2.1-beta0-20260926
VER_AND_BETA="${TAIL%-*}"             # 26.0929.191128          /  2.2.1-beta0
VER="${VER_AND_BETA%%-*}"
BETA=""
case "${VER_AND_BETA}" in
  *-beta*) BETA="${VER_AND_BETA#*-beta}" ;;
esac
if [ -z "${VER}" ]; then
  echo "✗ 从文件名里读不出版本号：${BASE}"
  exit 1
fi
TAG="v${VER}"
[ -n "${BETA}" ] && TAG="v${VER}-beta${BETA}"

if [ -n "${BETA}" ]; then
  KIND="测试包（beta${BETA}）"
else
  KIND="正式包"
fi

echo "==> 发行仓：${RELEASE_REPO}"
echo "==> 资产名：${ASSET}（密文）"
echo "==> 要发的包：${ZIP_IN}"
echo "    版本：${VER}   标签：${TAG}   类型：${KIND}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# ------------------------------------------- 剔敏感文件 → 加密 → 反查断言
echo "==> 剔除不该公开的文件，加密，再反查"
# `PLAIN` 是**中间产物**（剔完的明文 zip），加密完立刻删 —— 它留在 tmp 里
# 就等于把源码摆在硬盘上，谁 `ls /tmp` 都能看见。
OUT_ZIP="${WORK}/${ASSET}"
PLAIN="${WORK}/update-plain.zip"
python3 - "${ZIP_IN}" "${PLAIN}" "${VER}" "${ROOT}" "${OUT_ZIP}" <<'PYSTRIP'
import os
import sys
import zipfile

src, dst, ver, repo_root, out = sys.argv[1:6]

#: 这些是**打包时注入的密钥**，只随私发的安装包走，绝不能出现在公开资产里
#: （⚠ 就算整包已经加密，也照样剔 —— 密钥一旦泄露，公开仓上那份密文
#:   谁都能解了，防线只剩一层）。
BAN_NAMES = {"central-mail.env", "mail-key.json", "release.key"}
#: **按整条路径**禁发（文件名本身无害，内容有害）：
#:   `config/managers.yaml` = 区长名单 —— 里面是**云商登录名和私人邮箱**。
#:   它在 `selfupdate.NEVER_TOUCH` 里（`config/` 整个目录），更新本来就不会覆盖它，
#:   所以从更新包里拿掉对已装机器**零影响**；要给区长机器配名单走私发的安装包。
BAN_PATHS = {"config/managers.yaml"}
#: 「这台电脑自己的东西」和门店配置 —— 公开资产里一个都不该有
BAN_DIRS = (".secrets/", "out/", "in/", "dist/", ".dsh/", "tools/")
#: 更新器的锚点 —— 缺了它就不是我们的包（见 selfupdate.ANCHORS）
ANCHORS = ("src/cli.py", "bootstrap.py")

kept, dropped = [], []
with zipfile.ZipFile(src) as zin:
    names = [n for n in zin.namelist() if not n.endswith("/")]
    for n in names:
        # 包内顶层是 cbg-reconcile/ —— 比较用它下面的相对路径
        rel = n.split("/", 1)[1] if "/" in n else n
        parts = rel.split("/")
        if parts[-1] in BAN_NAMES:
            dropped.append(rel)
            continue
        if rel in BAN_PATHS:
            dropped.append(rel)
            continue
        if any(rel.startswith(d) for d in BAN_DIRS):
            dropped.append(rel)
            continue
        kept.append((n, rel))

    # ---- 反查断言（第二道闸）：剔完之后再验一遍，过了才允许写出资产
    bad = [r for _, r in kept
           if r.split("/")[-1] in BAN_NAMES or r in BAN_PATHS]
    bad += [r for _, r in kept if any(r.startswith(d) for d in BAN_DIRS)]
    missing = [a for a in ANCHORS if a not in {r for _, r in kept}]
    store_cfg = [r for _, r in kept
                 if r.startswith("config/store-") and not r.endswith("stores.yaml")]
    if bad or missing or store_cfg:
        print("    ✗ 反查没过 —— 不生成资产：")
        for r in bad:
            print(f"        还在：{r}")
        for r in store_cfg:
            print(f"        门店自己的配置混进来了：{r}")
        for a in missing:
            print(f"        缺锚点：{a}（这不像我们的包）")
        sys.exit(1)

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        for info in zin.infolist():
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else info.filename
            parts = rel.split("/")
            if (parts[-1] in BAN_NAMES or rel in BAN_PATHS
                    or any(rel.startswith(d) for d in BAN_DIRS)):
                continue
            # ⚠ 必须强制成 DEFLATED：直接复用 zin 的 info 会把**原来的压缩方式**
            #   一起带过来（有的条目是 STORED），包会莫名其妙变大。
            info.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(info, zin.read(info.filename))

print(f"    ✓ 保留 {len(kept)} 个文件；剔除 {len(dropped)} 个")
for r in dropped:
    print(f"        剔除：{r}")
if not dropped:
    print("        （这个包本来就没带密钥 —— 说明它是自更新包，正常）")

# ---------------------------------------------------------- 加密（路线 A）
# ⚠ 走 `mailcrypto`（现成的 Encrypt-then-MAC），**不自己写加密**。
#   用 `RELEASE_KEY_REL` 那把 —— 跟邮件那把分开，用错不会报"钥匙错了"，
#   只会报"校验不过"，非常难查，所以必须写死在这里。
sys.path.insert(0, repo_root)
from src import mailcrypto                                   # noqa: E402

with open(dst, "rb") as f:
    plain = f.read()
sealed, how = mailcrypto.seal(plain, root=repo_root,
                              rel=mailcrypto.RELEASE_KEY_REL)
if how.get("state") != "sealed":
    print("    ✗ 加密没成 —— %s" % (how.get("why") or "未知原因"))
    print("      先在打包机上生成一把：python -m src.cli release-key-new")
    sys.exit(1)
with open(out, "wb") as f:
    f.write(sealed)

# ---- 第三道闸：出来的**必须是密文**，而且当 zip 打不开
if not mailcrypto.is_sealed(sealed):
    sys.exit("    ✗ 产物不是我们的密文格式 —— 不上传")
try:
    with zipfile.ZipFile(out):
        pass
except zipfile.BadZipFile:
    pass
else:
    sys.exit("    ✗ 密文居然能当 zip 打开 —— 等于没加密，不上传")

os.remove(dst)                                # 明文中间产物：加密完立刻删
print(f"    ✓ 已加密（密钥 {how.get('key_id')}）：{len(sealed)} 字节")
print("    ✓ 密文当 zip 打不开（公开仓上读不出源码）")
PYSTRIP

# ------------------------------------------------------- 资产校验和
( cd "${WORK}" && (shasum -a 256 "${ASSET}" 2>/dev/null || sha256sum "${ASSET}") \
    > "${SHA_NAME}" )
echo "    sha256：$(cut -d' ' -f1 "${WORK}/${SHA_NAME}")"

if [ -n "${DRY_RUN}" ]; then
  echo
  echo "==> --dry-run：到此为止，不上传"
  echo "    资产就绪：${OUT_ZIP}"
  exit 0
fi

# ------------------------------------------------------------------ 发 Release
if ! command -v gh >/dev/null 2>&1; then
  echo "✗ 需要 GitHub CLI（gh）—— https://cli.github.com/"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "✗ gh 没登录 —— 先跑：gh auth login"
  exit 1
fi

TITLE="盛联门店数据平台 v${VER}"
if [ -n "${NOTES_FILE}" ]; then
  NOTES_BODY="$(cat "${NOTES_FILE}")"
elif [ -n "${NOTES}" ]; then
  NOTES_BODY="${NOTES}"
else
  NOTES_BODY="## ${TITLE}

更新包：\`${ASSET}\`（**加密**，校验和见同名 \`.sha256\`）

> 下载下来是密文，程序会自己解 —— 门店不用做任何事。
> 解不开说明这台机器缺更新包密钥：把 \`release.key\` 放进 \`.secrets\`
> 目录再点一次更新，或者拿一次完整安装包。

* 安装 / 升级步骤 → [门店操作手册](./门店操作手册.md)
* 这一版改了什么 → [发布说明](./发布说明.md) · [CHANGELOG](./CHANGELOG.md)
* 版本号 → [\`VERSION\`](./VERSION)"
fi
[ -n "${BETA}" ] && NOTES_BODY="> ⚠️ **这是测试包（beta${BETA}），不是正式版。**

${NOTES_BODY}"

# ⚠ 参数用数组攒着，别写 `$( [ -n "$BETA" ] && echo --prerelease )`：
#   这台机器是 bash 3.2（macOS 自带），那种写法在 `set -e` 下很脆，
#   而且空数组 `"${arr[@]}"` 在 `set -u` 下会直接报 unbound variable。
GH_ARGS=(--repo "${RELEASE_REPO}" --title "${TITLE}" --notes "${NOTES_BODY}")
if [ -n "${BETA}" ]; then
  GH_ARGS+=(--prerelease)
fi

echo "==> 发 Release：${TAG}"
if gh release view "${TAG}" --repo "${RELEASE_REPO}" >/dev/null 2>&1; then
  echo "    标签已存在 —— 覆盖资产（同一个版本重发）"
  gh release upload "${TAG}" "${OUT_ZIP}" "${WORK}/${SHA_NAME}" \
    --repo "${RELEASE_REPO}" --clobber
  if [ -n "${BETA}" ]; then
    gh release edit "${TAG}" "${GH_ARGS[@]}" --prerelease
  else
    gh release edit "${TAG}" "${GH_ARGS[@]}" --latest
  fi
else
  gh release create "${TAG}" "${OUT_ZIP}" "${WORK}/${SHA_NAME}" \
    "${GH_ARGS[@]}"
fi
echo "    ✓ https://github.com/${RELEASE_REPO}/releases/tag/${TAG}"

# -------------------------------------------------- 同步 VERSION / 文档 / CHANGELOG
# ⚠ 走 **GitHub Contents API**（api.github.com），不走 `git push`：
#   实测本机 `github.com` 直连 curl 20s 超时，而 `api.github.com` 0.35s ——
#   发版脚本在这种网络上必须走稳的那条路（门店客户端也是同一个理由）。
put_file() {                       # $1=仓库内路径  $2=本地文件  $3=提交说明
  local path="$1" file="$2" msg="$3" sha=""
  sha="$(gh api "repos/${RELEASE_REPO}/contents/${path}?ref=main" --jq '.sha' 2>/dev/null || true)"
  local b64
  b64="$(base64 < "${file}" | tr -d '\n')"
  if [ -n "${sha}" ]; then
    gh api -X PUT "repos/${RELEASE_REPO}/contents/${path}" \
      -f message="${msg}" -f content="${b64}" -f sha="${sha}" -f branch="main" >/dev/null
  else
    gh api -X PUT "repos/${RELEASE_REPO}/contents/${path}" \
      -f message="${msg}" -f content="${b64}" -f branch="main" >/dev/null
  fi
  echo "    ✓ ${path}"
}

echo "==> 同步文档到发行仓"
# 用户文档：直接用仓库里那份（它随包发给门店，是同源的）
put_file "门店操作手册.md" "${ROOT}/门店操作手册.md" "同步门店操作手册 ${TAG}"
put_file "发布说明.md" "${ROOT}/发布说明.md" "同步发布说明 ${TAG}"

# VERSION —— **只在正式包时改**（beta 不动它，否则门店会看到一个假的"有新版"）
# ⚠ 这一行是客户端 `remote_version()` 的第一源，写错/没写 = 门店看不到更新。
if [ -z "${BETA}" ]; then
  printf 'VERSION = "%s"\n' "${VER}" > "${WORK}/VERSION"
  put_file "VERSION" "${WORK}/VERSION" "发版 ${TAG}"
else
  echo "    · beta 包不动 VERSION（线上版本号保持不变）"
fi

# CHANGELOG —— 在第一个 \`## \` 之前插一节
CHANGELOG="${WORK}/CHANGELOG.md"
if gh api "repos/${RELEASE_REPO}/contents/CHANGELOG.md" --jq '.content' 2>/dev/null \
    | base64 -d > "${CHANGELOG}" 2>/dev/null && [ -s "${CHANGELOG}" ]; then
  NOTES="${NOTES:-详见 [发布说明](./发布说明.md)}"
  python3 - "${CHANGELOG}" "${VER}" "${NOTES}" <<'PYCL'
import pathlib, re, sys
path, ver, note = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
body = path.read_text(encoding="utf-8")
entry = f"## {ver}\n\n- {note}\n\n"
m = re.search(r"(?m)^## ", body)
if m:
    body = body[:m.start()] + entry + body[m.start():]
else:
    body = body.rstrip() + "\n\n---\n\n" + entry
path.write_text(body, encoding="utf-8")
PYCL
  put_file "CHANGELOG.md" "${CHANGELOG}" "CHANGELOG 加 ${TAG}"
else
  echo "    ! 读不到 CHANGELOG.md，跳过（不阻塞发版）"
fi

echo
echo "==> 完成"
echo "    资产：https://github.com/${RELEASE_REPO}/releases/tag/${TAG}"
echo "    客户端下次「检查更新」读到的版本：$( [ -n "${BETA}" ] && echo '（beta 不改变线上版本号）' || echo "${VER}" )"
echo
echo "    ⚠ 还没完（见 AGENTS.md 发版那节）："
echo "      正式包的 src/version.py 已由 build_package.sh 写回，需要 commit + push。"
echo "      ⚠ beta 包**不要 push**。"
