"""邮件附件的对称加密 —— **所有走邮件渠道的推送，附件都过这里**。

⚠ **这是"防顺手"级别的加密，不是"抗有动机的攻击者"级别的。**

档位是用户 2026-09-21 定的（三选一里的第一条）：要防的是
「外人 / 邮件被转发或公共邮箱里**被顺手看到**」。
对应的现实是：每封业务邮件都抄送到中台那个邮箱，而附件里的
`.db` / `.xlsx` 是**裸的 SQLite / 裸的 zip** —— 谁点开谁就看见
串号、会员号、金额。

⚠⚠ **它防不了门店**：密钥就在门店那台机器上（`.secrets/mail-key.json`），
门店能解密、也能伪造。**要防门店只能走非对称**（公钥随包、私钥只在平台）——
那是另一版的事，别拿这个模块当"防篡改"用。

## 为什么是 HMAC 拼的，而不是 AES

标准库**没有 AES**（`hashlib` 里只有哈希），而项目的规矩是**不引新依赖**
（`mailer.py` 顶部那条：门店电脑上少装一个包是一个；Win7 上能不能装
`cryptography` 的 wheel 也没验过）。所以用 `hmac` 当 PRF 拼：

    ek  = HMAC-SHA256(K, b"cbgenc/enc" + nonce)     # 加密密钥（域分离）
    mk  = HMAC-SHA256(K, b"cbgenc/mac" + nonce)     # 认证密钥（域分离）
    ct  = pt XOR keystream(ek, nonce)
    tag = HMAC-SHA256(mk, header || ct)             # Encrypt-then-MAC

keystream 的第 i 块 = `HMAC-SHA256(ek, nonce || i)` —— 这就是
**NIST SP 800-108「KDF in Counter Mode」那一族的做法**，不是自创算法。

> 用户原话是「设计一个加密算法」—— **算法不用设计**，自己设计才是风险。
> 这个模块真正要设计的是另外三件事：**密钥怎么发、版本怎么管、解不开时怎么办**。
> 那三件才是这个项目里会出事的地方（M18 的 `pending/` 补发、升级过渡期、
> 门店和区长两边版本不一致，都踩在它们上面）。

⚠⚠ **四条不许动**（每条都有测试钉着，改了都是静默降级）：

1. **Encrypt-then-MAC**，不是 MAC-then-encrypt；
2. **先验 tag 再解密** —— 顺序反了等于把没验过的数据当明文用；
3. `hmac.compare_digest` 比对 tag，**不许写 `==`**（时序侧信道）；
4. `alg` 和 `key_id` **必须在 tag 覆盖范围内**（否则能把密文头换掉还不被发现）。

## 密钥怎么到门店（最容易被误解的一处）

| 谁 | 路径 | 谁生成 |
|---|---|---|
| 打包机 | `.secrets/mail-key.json` | 人工：`python -m src.cli mail-key-new` |
| 包根（投递用） | `mail-key.json` | `tools/build_package.sh` 从上一行拷进去 |
| 门店 / 区长机器 | `.secrets/mail-key.json` | 安装时 `bootstrap._seed_mail_key()` 播种 |

**第二把：更新包的**（路线 A，2026-10-02）—— 同一条链，但钥匙分开：

| 谁 | 路径 | 谁生成 |
|---|---|---|
| 打包机 | `.secrets/release.key` | 人工：`python -m src.cli release-key-new` |
| 包根（投递用） | `release.key` | `tools/build_package.sh` 从上一行拷进去 |
| 门店机器 | `.secrets/release.key` | 安装时 `bootstrap._seed_release_key()` 播种 |
| 用它的地方 | 公开 Release 上的 `.zip.sealed` | `publish_release.sh` 加密 / `selfupdate` 解密 |

⚠ **两把钥匙互不通用**（`Test更新包密钥随包播种::test_两把钥匙各是各的` 钉着）。
用错的那一把不会报"钥匙错了"，只会报「校验不过 —— 密钥不对，或者内容被改过」
—— 这正是 `selfupdate._unseal_in_place` 要把话补成"能照做"的原因。

**「随大版本安装包走、不进小版本推包」是这条路子的天然性质**：
自更新（门店唯一的自动升级通道）是从**公开** GitHub 仓库拉 zip
（`selfupdate.REPO`，匿名读）⇒ 密钥**绝不能进 git**，进去了等于公开；
反过来，仓库里没有它 ⇒ 自更新的 zip 里也没有 ⇒ **覆盖不到、也删不掉**，
装一次之后一直有效。

⚠ **代价（必须让门店知道）**：3.0.0 之前就在跑、靠自更新升上来的机器
**是没有密钥的** —— 得人工把完整包再拷一次。所以这件事**三处可见**：
`selftest` 会报、没密钥发信时邮件正文里写一行（见 `mailer`）、
发布说明里写明。**别让它静默**（AGENTS 坑 13/15 都是"静默"栽的）。

## 「绝不抛」

`seal()` / `unseal()` 一律返回 `(数据, 说明)`，**不抛异常**。
调用它们的是发信路径和收信路径，在那里抛出去的表现是
「门店今天没上报」/「区长的包收不下来」—— 而这两件事都不该由加密来决定
（和 `split.send_report` 的"附件失败不挡正文"是同一条规矩）。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

# ⚠ 项目根只从 `paths` 取（全项目唯一一处知道"自己在第几层"）。
#   用 `from … import ROOT` 这种写法 —— 测试按**模块属性**打补丁
#   （`mock.patch.object(x, "ROOT", tmp)`），`from … import` 让每个模块
#   仍然有自己的 `ROOT` 名字，那些测试一行都不用改（`paths.py` 顶部写的）。
from .paths import ROOT

#: 密文魔数。**收信侧就靠它判"这要不要解"** —— 见 `is_sealed()`。
#:
#: ⚠ 为什么靠魔数、**不靠文件名后缀**（这个决定影响兼容性）：
#:   升级是渐进的（门店升了、区长还没升，或者反过来），收信侧必须
#:   **两种都能收**。只看前 8 字节 ⇒ 明文老包原样走原路，一行判断搞定；
#:   要是改成"文件名加 `.enc`"，`report_inbox` 里那两处
#:   `endswith(".db")` / `endswith(".json")` 就全得改，而且老包会被漏掉。
#:   代价是人下载附件双击打不开（Excel 会报"格式不对"）⇒ 由邮件正文里
#:   那行说明兜着（`mailer` 写的「附件已加密」）。
MAGIC = b"CBGENC01"

#: 算法编号。以后换算法**新开一个号**，老号永远能解（跟 key_id 一个道理）。
ALG_H2CTR_ETM = 1

NONCE_LEN = 16
TAG_LEN = 32
KEY_LEN = 32                 # 密钥本体 32 字节（HMAC-SHA256 的输出长度）
HEAD_LEN = 10                # magic(8) + alg(1) + klen(1)
MIN_SEALED = HEAD_LEN + NONCE_LEN + TAG_LEN     # 58：再短的密文肯定不是我们产的

#: 本机密钥文件。⚠ 放 `.secrets/`（`selfupdate.NEVER_TOUCH` 里的）——
#: 自更新**一根手指都不碰**它，所以"装一次之后一直有效"。
KEY_REL = ".secrets/mail-key.json"

#: 包里那份（**包根**，跟 `central-mail.env` 平级）—— 只用于安装时播种。
#: ⚠ 它**不在仓库里**（`.gitignore` 排掉 + 有测试盯着），是打包脚本塞进去的。
PACK_NAME = "mail-key.json"

# ── 第二把钥匙：**更新包的**（路线 A，2026-10-02）────────────────────────
#
# 用户定的：公开 Release 上只放**密文**，门店下载后本地解密再照常铺。
# 于是需要一把"加密公开包"的密钥，而它跟邮件那把**必须分开**：
#
#   * 邮件密钥泄露 ⇒ 别人能读邮件附件（业务数据）；
#   * 更新包密钥泄露 ⇒ **公开仓上的包人人能解** ⇒ 源码回到裸奔，
#     整条路线当场作废。爆炸半径差一个量级，不许共用一把。
#
# ⚠ **文件格式跟 `mail-key.json` 一模一样**（`{"current", "keys"}`）——
#   `load/_load_file/_keys_for` 原样复用，老 key 也一直留着能解历史包。
#   所有碰密钥的函数都多收一个 `rel=`（默认还是邮件那把），
#   **默认值不变 ⇒ 现有调用点一行都不用改**，也就不会静默改到邮件那条路。
# ⚠ 位置在 `.secrets/` ⇒ `selfupdate.NEVER_TOUCH` 覆盖它 ⇒ 自更新覆盖不到、
#   也删不掉，装一次一直有效（和邮件密钥同一条性质）。
RELEASE_KEY_REL = ".secrets/release.key"
#: 包里那份（**包根**）—— 安装时播种，跟 `PACK_NAME` 同一条路。
RELEASE_PACK_NAME = "release.key"


class MailKeyError(RuntimeError):
    """密钥文件本身有问题（坏了 / 版本对不上）。⚠ 只给 `cli` 和自检用，
    `seal` / `unseal` **不抛它** —— 那两条路不许因为密钥的问题中断业务。"""


# ------------------------------------------------------------------ 路径与读写

def key_file(root=None, rel: str = KEY_REL) -> Path:
    """本机密钥文件。`rel` 默认是邮件那把；更新包那把传 `RELEASE_KEY_REL`。"""
    return Path(root or ROOT) / rel


def pack_file(root=None, name: str = PACK_NAME) -> Path:
    """包里那份密钥（包根）—— 只在安装时读一次。"""
    return Path(root or ROOT) / name


def _load_file(path: Path) -> Dict:
    """读一份密钥文件 → `{"current": str, "keys": {id: {...}}}`。

    **读不到 / 读坏了都给空**，绝不抛 —— 给一个坏 JSON 就崩，
    表现是"门店今天没发信"，代价完全不成比例。
    """
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"current": "", "keys": {}}
    if not isinstance(d, dict):
        return {"current": "", "keys": {}}
    ks = d.get("keys")
    if not isinstance(ks, dict):
        return {"current": "", "keys": {}}
    out = {}
    for k, v in ks.items():
        # ⚠ 只收**能用**的那种条目（有 alg + 合法的 base64 密钥）：
        #   半截条目留着的话，`seal` 会挑中它然后失败 —— 而失败点离
        #   "文件写坏了"这件事很远，查起来要绕一大圈。
        item = _parse_item(v)
        if item:
            out[str(k)] = item
    return {"current": str(d.get("current") or ""), "keys": out}


def _parse_item(v) -> Dict:
    """一条密钥记录 → `{"alg","key","created","note"}`；不合法给 `{}`。"""
    if not isinstance(v, dict):
        return {}
    try:
        raw = base64.b64decode(str(v.get("key") or ""), validate=True)
    except (ValueError, TypeError):
        return {}
    if len(raw) != KEY_LEN:
        return {}
    try:
        alg = int(v.get("alg") or ALG_H2CTR_ETM)
    except (TypeError, ValueError):
        return {}
    if alg != ALG_H2CTR_ETM:
        return {}
    return {"alg": alg, "key": raw,
            "created": str(v.get("created") or ""),
            "note": str(v.get("note") or "")}


def load(root=None, rel: str = KEY_REL) -> Dict:
    """本机密钥（已解析）。返回 `{"current": str, "keys": {id: {...}}}`。"""
    return _load_file(key_file(root, rel))


def describe(root=None, rel: str = KEY_REL) -> Dict:
    """**给人看**的密钥状态（自检 / `cli mail-key-show` / 界面）。

    ⚠ **绝不返回密钥本体** —— 这个字典会被打进自检输出、日志、界面，
    带出去一次就等于密钥泄露一次。要核对密钥请比 `key_id`，别比内容。
    """
    p = key_file(root, rel)
    d = load(root, rel)
    ids = sorted(d["keys"])
    cur = d["current"]
    if not ids:
        return {"ok": False, "key_id": "", "count": 0, "keys": [], "path": str(p),
                "why": "没有密钥（%s 不存在）—— 附件不会被加密" % rel}
    if not cur:
        return {"ok": False, "key_id": "", "count": len(ids), "keys": ids, "path": str(p),
                "why": "密钥文件里没有 current（不知道该用哪把）"}
    if cur not in d["keys"]:
        return {"ok": False, "key_id": cur, "count": len(ids), "keys": ids, "path": str(p),
                "why": "current=%s 在 keys 里找不到（文件写坏了？）" % cur}
    return {"ok": True, "key_id": cur, "count": len(ids), "keys": ids, "path": str(p),
            "why": ""}


def save(root=None, current: str = "", keys: Optional[Dict] = None,
         merge: bool = True, rel: str = KEY_REL) -> bool:
    """写密钥文件。`merge=True` 时**与已有内容合并**（老 key 留着 —— 见 `seed_from_pack`）。

    写不成返回 `False`（不抛）。
    """
    p = key_file(root, rel)
    old = load(root, rel) if merge else {"current": "", "keys": {}}
    all_keys = dict(old["keys"])
    for k, v in (keys or {}).items():
        all_keys[str(k)] = v
    body = {"current": str(current or old["current"] or ""),
            "keys": {k: _dump_item(v) for k, v in sorted(all_keys.items())}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")
        try:                                   # 密钥文件别让同机器其他用户读
            os.chmod(str(p), 0o600)
        except OSError:
            pass                               # Windows 上 chmod 基本是空操作
        return True
    except OSError:
        return False


def _dump_item(v: Dict) -> Dict:
    return {"alg": int(v.get("alg") or ALG_H2CTR_ETM),
            "key": base64.b64encode(v["key"]).decode("ascii"),
            "created": str(v.get("created") or ""),
            "note": str(v.get("note") or "")}


# ------------------------------------------------------------------ 生成 / 播种

def _next_id(keys: Dict) -> str:
    """下一个 key_id：`m1` / `m2` / …（已有的最大号 +1）。"""
    n = 0
    for k in keys:
        k = str(k)
        if k.startswith("m") and k[1:].isdigit():
            n = max(n, int(k[1:]))
    return "m%d" % (n + 1)


def new_key(root=None, key_id: str = "", note: str = "", rel: str = KEY_REL) -> Dict:
    """生成一把新密钥并**设为 current**（老 key 留着）。

    返回 `{"ok","key_id","path","why"}`。⚠ 生成之后**别再改它** ——
    改一把已经发出去的密钥 = 换密钥 = 所有老包解不开。
    """
    d = load(root, rel)
    kid = str(key_id or "").strip() or _next_id(d["keys"])
    if kid in d["keys"]:
        return {"ok": False, "key_id": kid, "path": str(key_file(root, rel)),
                "why": "key_id %s 已经存在（换密钥请用一个新的号）" % kid}
    item = {"alg": ALG_H2CTR_ETM, "key": os.urandom(KEY_LEN),
            "created": datetime.date.today().isoformat(), "note": str(note or "")}
    if not save(root, current=kid, keys={kid: item}, rel=rel):
        return {"ok": False, "key_id": kid, "path": str(key_file(root, rel)),
                "why": "写不进去（目录权限？）"}
    return {"ok": True, "key_id": kid, "path": str(key_file(root, rel)), "why": ""}


def seed_from_pack(root=None, rel: str = KEY_REL, name: str = PACK_NAME) -> Dict:
    """**安装时**把包根那份密钥合并进 `.secrets/` 对应的那个文件。

    用户 2026-09-21 定的路子跟中台授权码同构（见 `bootstrap._seed_central_mail`）：
    打包时注入 → 安装时播种 → `.secrets/` 在 `NEVER_TOUCH` 里 ⇒ 一直有效。

    ⚠⚠ **和中台授权码那条有一处实质差别，别照抄那句话**：
      那边是「**已有键绝不覆盖**」（门店自己配的邮箱是他的设置，不该被包冲掉）；
      这边是「**current 必须跟着包更新，老 key 必须留着**」——
      * 不更新 current ⇒ 换了密钥等于没换（门店还在用旧的那把发）；
      * 不留老 key ⇒ `pending/` 里压着的历史包、邮箱里的老邮件**永久解不开**。

    返回 `{"ok","why","added":[...],"was","current","state"}` —— 调用方
    （`bootstrap`）拿它打印一行。**绝不抛**（那是启动路径上的东西，
    见 `ensure_layout` 的规矩）。
    """
    pack = _load_file(pack_file(root, name))
    if not pack["keys"]:
        return {"ok": True, "why": "", "added": [], "was": "", "current": "",
                "state": "none"}          # 包里没带 —— 什么都不做
    cur = load(root, rel)
    added = [k for k in sorted(pack["keys"]) if k not in cur["keys"]]
    cur_id = str(pack["current"] or cur["current"] or sorted(pack["keys"])[-1])
    if not save(root, current=cur_id, keys=pack["keys"], merge=True, rel=rel):
        return {"ok": False, "why": "密钥写不进 .secrets/（目录权限？）",
                "added": added, "was": cur["current"], "current": cur_id,
                "state": "failed"}
    state = "same" if (cur_id == cur["current"] and not added) else "updated"
    return {"ok": True, "why": "", "added": added, "was": cur["current"],
            "current": cur_id, "state": state}


# ------------------------------------------------------------------ 加解密本体

def is_sealed(data: bytes) -> bool:
    """这段字节是不是**我们产的密文**（只看魔数）。

    ⚠ 收信侧用它分流：不是密文 ⇒ **原样按老路走**（向后兼容，
      明文老包必须照旧能收 —— 升级是渐进的，两边版本会不一致）。
    """
    return bytes(data or b"").startswith(MAGIC)


def _keystream(ek: bytes, nonce: bytes, n: int) -> bytes:
    """`HMAC-SHA256(ek, nonce || i)` 逐块拼到 n 字节（计数器模式）。

    ⚠ 计数器**大端 8 字节、从 0 开始**：改了它，所有历史密文都解不开。
    """
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(ek, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def _xor(data: bytes, ks: bytes) -> bytes:
    """异或。⚠ 走大整数是**为了快**：428 KB 的包逐字节 Python 循环要几百毫秒，
    转成 int 一次异或是个位数毫秒（这是每天都要跑的那条路）。"""
    if not data:
        return b""
    return (int.from_bytes(data, "big")
            ^ int.from_bytes(ks[:len(data)], "big")).to_bytes(len(data), "big")


def _keys_for(d: Dict, kid: str) -> Optional[bytes]:
    item = d["keys"].get(kid)
    return item["key"] if item else None


def seal(data: bytes, *, root=None, rel: str = KEY_REL) -> Tuple[bytes, Dict]:
    """加密。返回 `(要发出去的字节, 说明)`。

    说明：`{"state": "sealed"|"plain", "key_id": str, "why": str}`

    ⚠ **没密钥 / 出错时返回原文 + `state="plain"`**，不抛、也不挡业务 ——
      业务连续性优先（上报是门店的日常），但调用方**必须把 `state` 说出来**
      （`mailer` 会往正文里写一行）。静默降级是这个项目最忌讳的一类错。

    ⚠ `rel` 选哪把钥匙：邮件附件走默认（`KEY_REL`）；**公开 Release 上的
      更新包必须传 `RELEASE_KEY_REL`** —— 用错钥匙不会报错，只会让
      门店解不开（而那时包已经在公开仓上了）。
    """
    data = bytes(data or b"")
    d = load(root, rel)
    kid = d["current"]
    K = _keys_for(d, kid) if kid else None
    if not K:
        why = describe(root, rel)["why"] or "密钥文件里没有 current"
        return data, {"state": "plain", "key_id": "", "why": why}
    try:
        kb = kid.encode("ascii")
    except UnicodeEncodeError:
        return data, {"state": "plain", "key_id": kid,
                      "why": "key_id 里有非 ASCII 字符"}
    if len(kb) > 255:
        return data, {"state": "plain", "key_id": kid, "why": "key_id 太长"}
    nonce = os.urandom(NONCE_LEN)
    ek = hmac.new(K, b"cbgenc/enc" + nonce, hashlib.sha256).digest()
    mk = hmac.new(K, b"cbgenc/mac" + nonce, hashlib.sha256).digest()
    ct = _xor(data, _keystream(ek, nonce, len(data)))
    head = MAGIC + bytes([ALG_H2CTR_ETM, len(kb)]) + kb
    body = head + nonce + ct
    tag = hmac.new(mk, body, hashlib.sha256).digest()
    return body + tag, {"state": "sealed", "key_id": kid, "why": ""}


def unseal(data: bytes, *, root=None, rel: str = KEY_REL) -> Tuple[bytes, Dict]:
    """解密。返回 `(原始字节, 说明)`。

    说明：`{"state": "opened"|"plain"|"failed", "key_id": str, "why": str}`

    * `plain` —— **不是我们的密文**（明文老包走这条）⇒ 原样返回，调用方照旧处理；
    * `opened` —— 解开了；
    * `failed` —— 是密文但解不开（缺密钥 / 密钥不对 / 被改过）。⚠ 这时返回的是
      **空字节**，调用方必须看 `state`，**不许把空字节当内容写下去**。

    **绝不抛**（收信路径上的东西）。
    """
    data = bytes(data or b"")
    if not is_sealed(data):
        return data, {"state": "plain", "key_id": "", "why": ""}
    if len(data) < MIN_SEALED:
        return b"", {"state": "failed", "key_id": "",
                     "why": "密文只有 %d 字节（短于 %d，不是完整的包）"
                            % (len(data), MIN_SEALED)}
    alg = data[8]
    klen = data[9]
    if alg != ALG_H2CTR_ETM:
        return b"", {"state": "failed", "key_id": "",
                     "why": "算法编号 %d 不认识（这个版本不支持）" % alg}
    off = HEAD_LEN + klen
    if len(data) < off + NONCE_LEN + TAG_LEN:
        return b"", {"state": "failed", "key_id": "",
                     "why": "密文头长度不对（klen=%d）" % klen}
    try:
        kid = data[HEAD_LEN:off].decode("ascii")
    except UnicodeDecodeError:
        return b"", {"state": "failed", "key_id": "",
                     "why": "key_id 不是 ASCII（密文头坏了）"}
    nonce = data[off:off + NONCE_LEN]
    ct = data[off + NONCE_LEN:-TAG_LEN]
    tag = data[-TAG_LEN:]

    d = load(root, rel)
    K = _keys_for(d, kid)
    if not K:
        # ⚠ 这句要能回答"我该怎么办" —— 门店/区长看到的就这一行。
        #   两把钥匙的补救动作不一样（邮件：装完整安装包；更新包：把
        #   release.key 放进 .secrets\），所以按 `rel` 分开说。
        if rel == RELEASE_KEY_REL:
            why = ("本机没有更新包密钥 %s（这台机器上的：%s）—— "
                   "把 %s 放进 .secrets\\ 再点一次更新，或者拿一次完整安装包"
                   % (kid, "、".join(sorted(d["keys"])) or "一把都没有",
                      RELEASE_PACK_NAME))
        else:
            why = ("本机没有密钥 %s（这台机器上的密钥：%s）—— "
                   "需要用带密钥的完整安装包装一次"
                   % (kid, "、".join(sorted(d["keys"])) or "一把都没有"))
        return b"", {"state": "failed", "key_id": kid, "why": why}
    mk = hmac.new(K, b"cbgenc/mac" + nonce, hashlib.sha256).digest()
    # ⚠ **先验 tag 再解密**（顺序不许换）+ `compare_digest`（不许 `==`）
    if not hmac.compare_digest(hmac.new(mk, data[:-TAG_LEN], hashlib.sha256).digest(),
                               tag):
        return b"", {"state": "failed", "key_id": kid,
                     "why": "校验不过 —— 密钥不对，或者内容被改过"}
    ek = hmac.new(K, b"cbgenc/enc" + nonce, hashlib.sha256).digest()
    return _xor(ct, _keystream(ek, nonce, len(ct))), {"state": "opened",
                                                      "key_id": kid, "why": ""}


def note_for(how: Dict) -> str:
    """把 `seal()` 的说明翻成**给人看的一行**（空串 = 不用说什么）。

    两种要说的情况：加密了（让人知道附件打不开是正常的）、
    没加密（让人知道这份是明文出去的）。**中间那种"悄悄地"不存在。**
    """
    st = str((how or {}).get("state") or "")
    if st == "sealed":
        return "附件已加密（密钥 %s）：双击打不开是正常的，看数据请在控制台里看。" \
               % how.get("key_id")
    if st == "plain":
        # ⚠⚠ 邮件正文是**纯文本**（`mailer` 里那句 `set_content(charset="utf-8")`）——
        #   写 Markdown 星号，收件人看到的就是字面的 `**`。
        # ⚠ 具体原因（文件不存在 / 文件写坏了）**不写进正文**：那句话在 `selftest`
        #   和 `cli mail-key-show` 里都有。正文只留"没加密"和"怎么办"两手，
        #   收件人扫一眼就该知道该做什么（跟 `upgrade.build_message` 一个道理）。
        return ("⚠ 本次附件没有加密 —— 本机还没装到邮件密钥，数据是明文发出去的。"
                "（把带密钥的完整安装包再拷一次就好了）")
    return ""
