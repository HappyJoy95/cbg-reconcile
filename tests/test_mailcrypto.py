"""邮件附件加密（`src/mailcrypto.py`）测试。

**测什么、为什么这么测**（每条都对应设计文档里一条"会出事的地方"）：

* **往返** —— 各种长度，含 0 和 428 KB（真实上报包的大小，
  见 M18 实测：975 行 / 428 KB）；
* **篡改解不开，而且返回的是"空"不是垃圾** —— 这是"先验 tag 再解密"
  唯一能测到的证据（顺序反了就会返回一段看着像数据的垃圾）；
* **`key_id` 必须落在 tag 覆盖范围内** —— 把密文头的 key_id 换掉必须失败，
  否则等于允许"换头"（模块顶部那四条不许动的细节之一）；
* **明文老包原样过** —— 升级是渐进的（门店升了、区长没升，或者反过来），
  两边的包必须**都能收**；
* **没密钥不许抛** —— 发信/收信路径上的东西，抛出去的表现是"门店今天没上报"
  /"区长的包收不下来"；
* **播种是「合并 + current 跟着包走」** —— ⚠ 这跟中台授权码那条**不一样**
  （那边是"已有键绝不覆盖"）：不更新 current ⇒ 换了密钥等于没换；
  不留老 key ⇒ `pending/` 里的历史包永久解不开。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src import mailcrypto as mc


def _b(s):
    """UTF-8 字节字面量。

    ⚠ Python 3.8/3.9 里 `b"中文"` 是**语法错误**（bytes 字面量只许 ASCII）——
    这个项目三头都要跑（门店 Win7 只能 3.8.10），中文一律走这里。
    """
    return s.encode("utf-8")

#: 仓库根 —— 用来查"密钥文件绝不许出现在仓库里"
REPO = Path(__file__).resolve().parents[1]


class _Base(unittest.TestCase):
    """带一个临时 root 的脚手架（`addCleanup` 收尾，跟项目里别的测试一个写法）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def sub(self, name):
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p


# ------------------------------------------------------------------ 密钥文件

class Test密钥文件(_Base):

    def test_生成一把之后就是_current(self):
        got = mc.new_key(self.root, note="3.0.0 随包")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["key_id"], "m1")
        d = mc.describe(self.root)
        self.assertTrue(d["ok"])
        self.assertEqual(d["key_id"], "m1")
        self.assertEqual(d["keys"], ["m1"])

    def test_没有密钥时说清怎么办(self):
        """⚠ 这句话是门店/区长唯一能看到的线索，必须能照着做。"""
        d = mc.describe(self.root)
        self.assertFalse(d["ok"])
        self.assertIn(mc.KEY_REL.replace("/", os.sep).split(os.sep)[-1], d["why"])
        self.assertIn("不会", d["why"], "要说清后果：附件不会被加密")

    def test_describe_绝不带密钥本体(self):
        """⚠⚠ 这个字典会进自检输出 / 日志 / 界面 —— 带出去一次就等于泄露一次。"""
        mc.new_key(self.root)
        raw = json.loads((self.root / mc.KEY_REL).read_text(encoding="utf-8"))
        secret = raw["keys"]["m1"]["key"]
        self.assertTrue(secret, "前置条件：文件里确实有密钥")
        blob = json.dumps(mc.describe(self.root), ensure_ascii=False)
        self.assertNotIn(secret, blob, "describe() 不许把密钥本体带出来")

    def test_密钥文件坏了不许抛(self):
        p = self.root / mc.KEY_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(mc.load(self.root)["keys"], {})
        self.assertFalse(mc.describe(self.root)["ok"])
        data, how = mc.seal(b"hello", root=self.root)        # 也不许抛
        self.assertEqual(how["state"], "plain")
        self.assertEqual(data, b"hello")

    def test_半截条目当不存在(self):
        """密钥长度不对 / base64 坏了的那条要**丢掉**。

        ⚠ 留着它的后果：`seal` 挑中它然后失败，而失败点离"文件写坏了"
          这件事很远 —— 查起来要绕一大圈（模块注释里写了）。
        """
        p = self.root / mc.KEY_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "current": "m1",
            "keys": {"m1": {"alg": 1, "key": "短"},                  # 长度不对
                     "m2": {"alg": 1, "key": "!!!不是 base64!!!"},   # 解不开
                     "m3": {"alg": 99, "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}},
        }, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(mc.load(self.root)["keys"], {})

    def test_换密钥时老的那把留着(self):
        """⚠ 不留就解不开历史包（`pending/` 补发 + 邮箱里的老邮件）。"""
        mc.new_key(self.root)
        first = json.loads((self.root / mc.KEY_REL).read_text(encoding="utf-8"))
        old_secret = first["keys"]["m1"]["key"]
        got = mc.new_key(self.root, note="大版本换的")
        self.assertEqual(got["key_id"], "m2")
        now = mc.describe(self.root)
        self.assertEqual(now["keys"], ["m1", "m2"])
        self.assertEqual(now["key_id"], "m2", "current 要指向新的那把")
        raw = json.loads((self.root / mc.KEY_REL).read_text(encoding="utf-8"))
        self.assertEqual(raw["keys"]["m1"]["key"], old_secret, "老密钥本体一个字都不许改")

    def test_同一个_key_id_不许重复生成(self):
        mc.new_key(self.root)
        got = mc.new_key(self.root, key_id="m1")
        self.assertFalse(got["ok"])
        self.assertIn("已经存在", got["why"])


# ------------------------------------------------------------------ 加解密

class Test加解密(_Base):

    def setUp(self):
        super().setUp()
        self.assertTrue(mc.new_key(self.root)["ok"])

    def _round(self, n):
        pt = os.urandom(n)
        ct, how = mc.seal(pt, root=self.root)
        self.assertEqual(how["state"], "sealed", how)
        self.assertTrue(mc.is_sealed(ct))
        back, how2 = mc.unseal(ct, root=self.root)
        self.assertEqual(how2["state"], "opened", how2)
        self.assertEqual(back, pt)
        return ct

    def test_各种长度都能往返(self):
        # 0 / 1 是边界；31/32/33 卡 keystream 的块边界（SHA256 每块 32 字节）；
        # 428 KB = 真实上报包的大小（M18 实测 975 行 / 428 KB）。
        for n in (0, 1, 31, 32, 33, 1000, 428 * 1024):
            ct = self._round(n)
            self.assertEqual(len(ct), n + 10 + 2 + mc.NONCE_LEN + mc.TAG_LEN,
                             "密文 = 明文 + 固定头（key_id m1 是 2 字节）")

    def test_同内容两次加密结果不同(self):
        """nonce 每次随机 —— 否则"同一份包"在邮件里一眼就认出来（也更危险）。"""
        a, _ = mc.seal(_b("同样的内容"), root=self.root)
        b, _ = mc.seal(_b("同样的内容"), root=self.root)
        self.assertNotEqual(a, b)
        self.assertEqual(mc.unseal(a, root=self.root)[0], _b("同样的内容"))
        self.assertEqual(mc.unseal(b, root=self.root)[0], _b("同样的内容"))

    def test_篡改一个字节就解不开_而且不给垃圾(self):
        """⚠ 返回**空**而不是一段看着像数据的垃圾 —— 这是"先验 tag 再解密"的证据。

        顺序反了的话（先解后验），篡改中间某字节会让那一段变成乱码，
        但函数**照样返回数据**，调用方一不留神就把垃圾写进库里了。
        """
        pt = b"SQLite format 3\x00" + os.urandom(500)
        ct, _ = mc.seal(pt, root=self.root)
        # ⚠ 不测 offset 8（alg 字节）—— 改它会被"算法编号不认识"先拦下，
        #   走的是另一条分支（那条在 test_不认识的算法编号被拒 里）。
        for where in (40, len(ct) // 2, len(ct) - 1):           # nonce / 正文 / tag
            bad = bytearray(ct)
            bad[where] ^= 0x01
            got, how = mc.unseal(bytes(bad), root=self.root)
            self.assertEqual(how["state"], "failed", "第 %d 字节改动没被发现" % where)
            self.assertEqual(got, b"", "解不开时必须返回空，不许给半截数据")
            self.assertIn("校验不过", how["why"])

    def test_key_id_在_tag_覆盖范围内(self):
        """把密文头的 key_id 换成另一把（**同长度**，结构不变）⇒ 必须失败。

        ⚠ 这条钉的是模块顶部"四条不许动"的第 4 条。没覆盖的话，
          换头之后会用**另一把密钥**去解，解出来是垃圾却**不报错** ——
          那才是最难查的一种失败。
        """
        mc.new_key(self.root, key_id="m9")          # 造一把同长度的（2 字节）
        pt = _b("要保护的内容")
        ct, _ = mc.seal(pt, root=self.root)         # 用 current=m9 加的
        self.assertTrue(ct[10:12] == b"m9", "前置条件：头里写的 key_id 是 m9")
        bad = bytearray(ct)
        bad[10:12] = b"m1"                          # 换成第一把
        got, how = mc.unseal(bytes(bad), root=self.root)
        self.assertEqual(how["state"], "failed")
        self.assertEqual(got, b"")
        self.assertEqual(how["key_id"], "m1", "要报出来它试的是哪一把")

    def test_不认识的算法编号被拒(self):
        ct, _ = mc.seal(_b("内容"), root=self.root)
        bad = bytearray(ct)
        bad[8] = 99
        got, how = mc.unseal(bytes(bad), root=self.root)
        self.assertEqual(how["state"], "failed")
        self.assertIn("算法编号", how["why"])
        self.assertEqual(got, b"")

    def test_截断的密文报得清(self):
        ct, _ = mc.seal(b"x" * 100, root=self.root)
        for cut in (mc.MIN_SEALED - 1, mc.MIN_SEALED, len(ct) - 1):
            got, how = mc.unseal(ct[:cut], root=self.root)
            self.assertEqual(how["state"], "failed", "截到 %d 字节没报错" % cut)
            self.assertEqual(got, b"")
            self.assertTrue(how["why"], "必须说清为什么")

    def test_明文老包原样返回(self):
        """⭐⭐ 向后兼容：升级是渐进的，**明文老包必须照旧能收**。

        库里那两份（本机 `.db` / 邮箱里的老邮件）在升级前后都还在，
        收信侧要是因为"没有魔数"就报错，那几天的数据就断了。
        """
        for plain in (b"SQLite format 3\x00", b"PK\x03\x04",
                      json.dumps({"a": 1}, ensure_ascii=False).encode("utf-8"),
                      b"", b"CBGENC0", b"CBGENC02"):        # 末两个：魔数差一位
            self.assertFalse(mc.is_sealed(plain), plain)
            got, how = mc.unseal(plain, root=self.root)
            self.assertEqual(how["state"], "plain", plain)
            self.assertEqual(got, plain, "明文必须原样返回")


class Test不许抛(_Base):
    """发信 / 收信路径上的东西 —— 抛出去的表现是"门店今天没上报"。"""

    def test_没密钥时照发原文并说清(self):
        data, how = mc.seal(_b("要发的内容"), root=self.root)
        self.assertEqual(how["state"], "plain")
        self.assertEqual(data, _b("要发的内容"), "没密钥也必须发得出去（业务优先）")
        self.assertTrue(how["why"], "但必须说清为什么没加密")
        self.assertFalse(mc.is_sealed(data))

    def test_current_指向不存在的密钥时说清(self):
        mc.new_key(self.root)
        p = self.root / mc.KEY_REL
        d = json.loads(p.read_text(encoding="utf-8"))
        d["current"] = "m7"
        p.write_text(json.dumps(d), encoding="utf-8")
        d2 = mc.describe(self.root)
        self.assertFalse(d2["ok"])
        self.assertIn("m7", d2["why"])
        data, how = mc.seal(b"x", root=self.root)
        self.assertEqual(how["state"], "plain")

    def test_收信侧缺密钥的提示要能照着做(self):
        """⚠ 区长机器上看到的就是这一行 —— 得告诉他"怎么办"，不是"坏了"。"""
        other = self.sub("has-key")
        mc.new_key(other)
        ct, _ = mc.seal(_b("包内容"), root=other)

        bare = self.sub("bare")
        got, how = mc.unseal(ct, root=bare)
        self.assertEqual(how["state"], "failed")
        self.assertEqual(got, b"")
        self.assertIn("m1", how["why"], "要说清缺的是哪一把")
        self.assertIn("完整安装包", how["why"], "要给一条能照着做的出路")

    def test_乱喂东西也不抛(self):
        mc.new_key(self.root)
        ct, _ = mc.seal(_b("正常内容"), root=self.root)
        for junk in (b"", b"\x00", mc.MAGIC, mc.MAGIC + b"\x01",
                     mc.MAGIC + b"\x01\xff" + b"x" * 10,        # klen=255，后面不够
                     b"\xff" * 200, os.urandom(300)):
            got, how = mc.unseal(junk, root=self.root)
            self.assertIn(how["state"], ("plain", "failed"), junk[:20])
            self.assertIsInstance(got, bytes)
        # 正常那条还得能解（证明上面那些没把状态搞坏）
        self.assertEqual(mc.unseal(ct, root=self.root)[0], _b("正常内容"))


# ------------------------------------------------------------------ 随包播种

class Test随包播种(_Base):
    """安装时把包根那份 `mail-key.json` 播进 `.secrets/`。

    ⚠⚠ 跟中台授权码那条**有一处实质差别**（`bootstrap._seed_central_mail`
      是"已有键绝不覆盖"）：这边 **current 必须跟着包走、老 key 必须留着**。
      照抄那边的话 —— 换了密钥等于没换，而且历史包再也解不开。
    """

    def _pack(self, src_root, dst_root):
        """模拟 `tools/build_package.sh` 干的事：把密钥拷到包根。"""
        shutil.copy(str(src_root / mc.KEY_REL), str(dst_root / mc.PACK_NAME))

    def test_包里没带就什么都不做(self):
        got = mc.seed_from_pack(self.root)
        self.assertTrue(got["ok"])
        self.assertEqual(got["state"], "none")
        self.assertFalse((self.root / mc.KEY_REL).exists(), "不该凭空建出密钥文件")

    def test_播种之后能解开包里的密文(self):
        """端到端：打包机加密 → 包里带上密钥 → 门店装 → 解开了。"""
        packer, store = self.sub("packer"), self.sub("store")
        mc.new_key(packer)
        ct, how = mc.seal(_b("门店发出去的那个包"), root=packer)
        self.assertEqual(how["state"], "sealed")
        self._pack(packer, store)

        got = mc.seed_from_pack(store)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["current"], "m1")
        self.assertEqual(got["state"], "updated")
        back, how2 = mc.unseal(ct, root=store)
        self.assertEqual(how2["state"], "opened", how2)
        self.assertEqual(back, _b("门店发出去的那个包"))

    def test_current_跟着包更新_老_key_留着(self):
        """⚠⚠ 这就是和中台授权码那条的差别，两个方向都要钉住。"""
        packer = self.sub("packer")
        mc.new_key(packer)                              # m1（老的大版本）
        store = self.sub("store")
        self._pack(packer, store)
        mc.seed_from_pack(store)
        old_ct, _ = mc.seal(_b("老包"), root=packer)

        mc.new_key(packer, note="3.1.0 换的")            # m2（新的大版本）
        self._pack(packer, store)
        got = mc.seed_from_pack(store)
        self.assertTrue(got["ok"])
        self.assertEqual(got["current"], "m2", "current 必须跟着包走")
        self.assertEqual(got["was"], "m1")
        self.assertEqual(got["added"], ["m2"])
        self.assertEqual(mc.describe(store)["keys"], ["m1", "m2"], "老 key 必须留着")
        # 老包（m1 加的）在换过密钥之后**照样能解**
        back, how = mc.unseal(old_ct, root=store)
        self.assertEqual(how["state"], "opened", "换了密钥之后老包必须还能解")
        self.assertEqual(back, _b("老包"))
        # 新的用新密钥发
        new_ct, how2 = mc.seal(_b("新包"), root=store)
        self.assertEqual(how2["key_id"], "m2")

    def test_重复装同一个包是幂等(self):
        packer, store = self.sub("packer"), self.sub("store")
        mc.new_key(packer)
        self._pack(packer, store)
        mc.seed_from_pack(store)
        got = mc.seed_from_pack(store)
        # ⚠ 这里用「」不用嵌套双引号 —— 这个项目为它红过四次（开发指南 §七）
        self.assertEqual(got["state"], "same", "同一个包再装一次不该报「更新了」")
        self.assertEqual(mc.describe(store)["keys"], ["m1"])

    def test_包里的密钥文件坏了也不抛(self):
        (self.root / mc.PACK_NAME).write_text("{坏了", encoding="utf-8")
        got = mc.seed_from_pack(self.root)
        self.assertTrue(got["ok"])
        self.assertEqual(got["state"], "none")


# ------------------------------------------------------------------ 仓库卫生

class Test密钥绝不进仓库(unittest.TestCase):
    """⚠ 仓库是**公开**的（自更新匿名读 `api.github.com`，实测 200）。
    密钥 commit 进去 = 全世界可见 = 加密当场归零。
    """

    def test_gitignore_排掉它(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(mc.PACK_NAME, gi, "打包产出的密钥文件绝不能进 git")

    def test_仓库根不许有它(self):
        self.assertFalse((REPO / mc.PACK_NAME).exists(),
                         "仓库根出现了 %s —— 千万别 commit 它" % mc.PACK_NAME)

    def test_密钥文件没被_git_跟踪(self):
        """⚠ 就算文件在（打了包的开发机上本来就在），也**绝不许**被 git 跟踪
        —— 仓库是公开的，跟踪一次就等于把密钥公布出去。"""
        for rel in (mc.PACK_NAME, mc.KEY_REL):
            if (REPO / rel).exists():
                self.assertFalse(_tracked(rel),
                                 "%s 被 git 跟踪了 —— 赶紧把它撤下来" % rel)


def _tracked(rel):
    """这个路径有没有被 git 跟踪（读不出来就当没跟踪）。"""
    try:
        import subprocess
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                             cwd=str(REPO), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=10)
        return out.returncode == 0
    except Exception:                                          # noqa: BLE001
        return False


# ------------------------------------------------------------------ 说明文案

class Test给人看的那一行(_Base):

    def test_加密时说的话(self):
        mc.new_key(self.root)
        _, how = mc.seal(b"x", root=self.root)
        line = mc.note_for(how)
        self.assertIn("已加密", line)
        self.assertIn("m1", line, "要说清用的哪把密钥（核对时只比号，不比内容）")
        self.assertIn("打不开", line, "要提前解释「双击打不开」是正常的")

    def test_没加密时要报警(self):
        """⚠ AGENTS 坑 13/15 都是"静默"栽的 —— 这条是那个教训的落点。"""
        _, how = mc.seal(b"x", root=self.root)
        line = mc.note_for(how)
        self.assertIn("没有加密", line)
        self.assertTrue(line.startswith("⚠"), "要显眼")

    def test_没有附件时不用说(self):
        self.assertEqual(mc.note_for({}), "")
        self.assertEqual(mc.note_for({"state": "opened"}), "")

    def test_正文是纯文本_不许出现_markdown_星号(self):
        """⚠ 邮件正文走 `set_content(charset="utf-8")`，星号会**字面**显示给收件人。

        第一版这两句里写了 `**没有加密**`，收件人看到的就是那两个星号
        （写这个模块时当场踩的，跟「字符串里别套双引号」是同一类毛病）。
        """
        _, how = mc.seal(b"x", root=self.root)                 # 没密钥
        self.assertNotIn("**", mc.note_for(how))
        mc.new_key(self.root)
        _, how2 = mc.seal(b"x", root=self.root)                # 有密钥
        self.assertNotIn("**", mc.note_for(how2))

    def test_没加密那行要说清怎么办(self):
        """⚠ 门店/区长看到这一行，得知道下一步做什么 —— 不能只说「出问题了」。"""
        _, how = mc.seal(b"x", root=self.root)
        self.assertIn("完整安装包", mc.note_for(how))


# ------------------------------------------------------------------ 接线
#
# 用户 2026-09-21：「这个**加密功能算在推送模块里**，**解密功能做在 fetch 模块里**」
# ⇒ 下面这两类就是钉那句话的：**往外发的问 notify，往里收的问 fetch**。
# ⚠ 算法本体只有一份（`src/mailcrypto.py`）—— 那正是它们不会走散的原因。


class _FakeMailConfig:
    """够 `_send_mail` 用就行（它读 recipients / from_addr / cc 等）。"""

    enabled = True
    recipients = ["someone@example.com"]
    subject_prefix = "[报量对账]"
    from_addr = "me@example.com"
    cc = []


class Test推送模块管加密(_Base):
    """`notify.seal_attachment()` —— 往外发的东西怎么保护。"""

    def _send(self, attachments, content_extra=None):
        """走一遍 `notify.send("mail", …)`，把 `mailer.send` 收到的东西抓下来。"""
        from unittest import mock
        from src import mailer
        from src.modules import notify
        seen = {}

        def fake_send(mc_, subject, body, attachments=(), prefix=None, to=None):
            seen.update(subject=subject, body=body,
                        attachments=list(attachments), prefix=prefix, to=to)

        content = {"subject": "主题", "body": "正文", "attachments": attachments}
        content.update(content_extra or {})
        # 2026-09-22：notify 改走 load_mail_paths 扇出 —— 桩要跟着换
        paths = [("fake", _FakeMailConfig())]
        with mock.patch.object(mailer, "send", fake_send), \
                mock.patch.object(mailer, "load_mail_paths",
                                  lambda cfg, r=None: paths):
            res = notify.send("mail", content, cfg={}, root=self.root)
        return res, seen

    def test_推一件附件会被加密(self):
        from src.modules import notify
        mc.new_key(self.root)
        f = self.root / "cbg-SCN1-2026-09-21.db"
        raw = b"SQLite format 3\x00" + os.urandom(300)
        f.write_bytes(raw)

        (name, data), how = notify.seal_attachment(f, root=self.root)
        self.assertEqual(how["state"], "sealed")
        self.assertEqual(name, f.name, "文件名要原样保留（收信侧靠魔数分流，不靠后缀）")
        self.assertTrue(data.startswith(mc.MAGIC), "发出去的应该是密文")
        self.assertNotIn(b"SQLite format 3", data, "明文头不许留在密文里")
        self.assertEqual(mc.unseal(data, root=self.root)[0], raw)

    def test_走一遍_send_附件真的是密文(self):
        mc.new_key(self.root)
        f = self.root / "包.db"
        f.write_bytes(b"SQLite format 3\x00" + os.urandom(100))
        res, seen = self._send([f])
        self.assertEqual(res["state"], "sent", res)
        (name, data), = seen["attachments"]
        self.assertEqual(name, "包.db")
        self.assertTrue(mc.is_sealed(data), "送到 mailer 手上的必须是密文")
        self.assertEqual(mc.unseal(data, root=self.root)[0], f.read_bytes())

    def test_加密了要在正文里说一声(self):
        """⚠ 附件双击是乱码，得让收件人知道这是**正常**的。"""
        mc.new_key(self.root)
        f = self.root / "包.db"
        f.write_bytes(b"x" * 40)
        _, seen = self._send([f])
        self.assertIn("正文", seen["body"], "原来的正文不许丢")
        self.assertIn("已加密", seen["body"])

    def test_没密钥时照发_但正文里要说清(self):
        """⚠⚠ 业务连续性优先（上报是门店的日常），但**不许静默**降级。"""
        f = self.root / "包.db"
        f.write_bytes(b"SQLite format 3\x00" + os.urandom(50))
        res, seen = self._send([f])
        self.assertEqual(res["state"], "sent", "没密钥也必须发得出去")
        (name, data), = seen["attachments"]
        self.assertEqual(data, f.read_bytes(), "没密钥就照发原文")
        self.assertIn("没有加密", seen["body"], "必须在正文里说出来（AGENTS 坑 13/15）")
        self.assertIn("完整安装包", seen["body"], "并且要说清怎么办")

    def test_几件附件只写一次提示(self):
        """三份附件同一个状态，正文里不要重复三遍。"""
        mc.new_key(self.root)
        fs = []
        for i in range(3):
            p = self.root / ("包%d.db" % i)
            p.write_bytes(b"x" * 20)
            fs.append(p)
        _, seen = self._send(fs)
        self.assertEqual(seen["body"].count("已加密"), 1)
        self.assertEqual(len(seen["attachments"]), 3)

    def test_读不出来的附件跳过_不报错(self):
        """老行为：文件不在就当没这个附件（`build_message` 里那句 `is_file()`）。"""
        mc.new_key(self.root)
        res, seen = self._send([self.root / "根本没有这个文件.db"])
        self.assertEqual(res["state"], "sent")
        self.assertEqual(seen["attachments"], [])

    def test_prefix_给空串就不许套前缀(self):
        """⚠⚠ 这是接加密时**顺带修掉的一个真 bug**。

        `bugreport` 传的是 `prefix=""`（它主题自己带了 `[bug 上报]`，不想再套一层
        `[报量对账]`），而 `_send_mail` 原来写的是 `content.get("prefix") or None`
        —— 空串被 `or` 吃掉 ⇒ 主题上又套回了配置里那个前缀。
        （它原来直接调 `mailer.send`，所以一直没露出来；改走 notify 才现形。）
        """
        _, seen = self._send([], {"prefix": ""})
        self.assertEqual(seen["prefix"], "", "空串必须原样传下去")

    def test_没给_prefix_才是_None(self):
        _, seen = self._send([])
        self.assertIsNone(seen["prefix"], "没给 = None（用配置里那个前缀）")

    def test_密钥状态从推送模块问(self):
        from src.modules import notify
        self.assertFalse(notify.mail_key(root=self.root)["ok"])
        mc.new_key(self.root)
        d = notify.mail_key(root=self.root)
        self.assertTrue(d["ok"])
        self.assertEqual(d["key_id"], "m1")


class Test收信模块管解密(_Base):
    """`fetch.unseal_attachment()` —— 收进来的东西怎么还原。"""

    def test_密文解得开(self):
        from src.modules import fetch
        mc.new_key(self.root)
        ct, _ = mc.seal("门店发来的包".encode("utf-8"), root=self.root)
        got, how = fetch.unseal_attachment(ct, root=self.root)
        self.assertEqual(how["state"], "opened")
        self.assertEqual(got, "门店发来的包".encode("utf-8"))

    def test_明文原样过(self):
        """⭐ 向后兼容：升级是渐进的，**明文老包必须照旧能收**。"""
        from src.modules import fetch
        plain = b"SQLite format 3\x00" + os.urandom(50)
        got, how = fetch.unseal_attachment(plain, root=self.root)
        self.assertEqual(how["state"], "plain")
        self.assertEqual(got, plain)

    def test_缺密钥时给的是空_调用方必须看_state(self):
        from src.modules import fetch
        other = self.sub("has-key")
        mc.new_key(other)
        ct, _ = mc.seal(b"payload", root=other)
        got, how = fetch.unseal_attachment(ct, root=self.root)
        self.assertEqual(how["state"], "failed")
        self.assertEqual(got, b"", "⚠ 解不开时是空字节 —— 不许当内容写下去")
        self.assertIn("完整安装包", how["why"])


class Test邮件底层的两种附件(_Base):
    """`mailer.build_message`：**路径**（老样子）和 **`(名字, 字节)`**（加密后给的）。"""

    def _msg(self, attachments):
        from src import mailer
        return mailer.build_message(_FakeMailConfig(), "主题", "正文",
                                    attachments=attachments)

    def test_元组形态(self):
        msg = self._msg([("包.db", b"\x00\x01\x02\x03")])
        parts = list(msg.iter_attachments())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), "包.db")
        self.assertEqual(parts[0].get_payload(decode=True), b"\x00\x01\x02\x03")

    def test_路径形态照旧(self):
        p = self.root / "差异清单.xlsx"
        p.write_bytes(b"PK\x03\x04" + b"pretend-xlsx")
        msg = self._msg([p])
        parts = list(msg.iter_attachments())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), "差异清单.xlsx")
        self.assertEqual(parts[0].get_payload(decode=True), p.read_bytes())
        self.assertEqual(parts[0].get_content_type(),
                         "application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet", "xlsx 的 maintype 不许被改掉")

    def test_两种形态能混着用(self):
        p = self.root / "a.db"
        p.write_bytes(b"AAA")
        msg = self._msg([p, ("b.db", b"BBB")])
        got = {x.get_filename(): x.get_payload(decode=True)
               for x in msg.iter_attachments()}
        self.assertEqual(got, {"a.db": b"AAA", "b.db": b"BBB"})


class Test收信落库前解密(_Base):
    """`report_inbox._unsealed()` —— M18/M19 那条链上「收进来」的那一步。"""

    def _call(self, data):
        from src.app import report_inbox
        out = {"problems": []}
        said = []
        got = report_inbox._unsealed({"filename": "包.db", "data": data},
                                     self.root, out, said.append)
        return got, out, said

    def test_密文解开了才往下走(self):
        mc.new_key(self.root)
        ct, _ = mc.seal("包里真正的内容".encode("utf-8"), root=self.root)
        got, out, said = self._call(ct)
        self.assertEqual(got, "包里真正的内容".encode("utf-8"))
        self.assertEqual(out["problems"], [])
        self.assertEqual(said, [])

    def test_明文老包原样往下走(self):
        plain = b"SQLite format 3\x00" + os.urandom(60)
        got, out, said = self._call(plain)
        self.assertEqual(got, plain)
        self.assertEqual(out["problems"], [], "明文老包不该报任何问题")

    def test_解不开时返回_None_并记进_problems(self):
        """⚠ 这一条钉的是"别把空字节当内容写下去"：
        写下去会造出一个 0 行的空包，而 `import_package` 那边「看着成功了」。"""
        other = self.sub("has-key")
        mc.new_key(other)
        ct, _ = mc.seal(b"payload", root=other)

        got, out, said = self._call(ct)
        self.assertIsNone(got, "解不开必须是 None，不能是空字节")
        self.assertEqual(len(out["problems"]), 1)
        self.assertIn("包.db", out["problems"][0])
        self.assertIn("完整安装包", out["problems"][0], "要说清怎么办")
        self.assertTrue(said, "还要 emit 一行（运行日志里看得见）")
