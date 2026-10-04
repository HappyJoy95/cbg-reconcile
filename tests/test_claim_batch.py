# -*- coding: utf-8 -*-
"""权益领取 · **多选 + 批量自动领取**（2.2.4）的前端接线。

用户 2026-09-29：「每个条目前面加个多选框，可以批量自动领取」；
形态两条拍板：**全选只选当前页** · **批量前一次确认**。

⚠ 这一层的坑全在浏览器里才炸（勾得上点不了 / 点了没反应 / 连点提交两轮 /
  重画把勾冲掉），Python 测试看不见 —— 所以钉的是**源码接线**：

* 工具条的 id 真的在 `index.html` 里（`$('#x')` 拿到 null = 整块哑火）；
* 单条按钮与批量勾**共用同一份判据**（两份迟早走散，AGENTS 坑 12）；
* 批量走的是**单条那个提交端点** —— 不许为批量开第二条写路径（坑 18：
  范围校验 / 86 码拦截 / 无链接拦截 / 成功自动标已领全在那条路里）；
* **先确认再提交**、跑批防重入、失败不中断且失败的留在勾选里。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js",
                              "features/tools/claim/page.js", "app.js"))
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
WEB_PY = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """取一个顶层函数的函数体（到第一个顶格 `}` 为止）。"""
    i = src.index("function %s" % name)
    j = src.index("\n}\n", i)
    return src[i:j]


class Test接线(unittest.TestCase):
    def test_工具条的id都在HTML里(self):
        for i in ("claim-batch-bar", "claim-select-page", "claim-batch-info",
                  "claim-batch-claim", "claim-batch-clear", "claim-batch-result"):
            self.assertIn('id="%s"' % i, INDEX_HTML, "index.html 缺 id=" + i)

    def test_app里用到的claim批量id都能找到(self):
        used = set(re.findall(r"""\$\(\s*['"]#(claim-(?:batch|select)[a-z-]*)['"]\s*\)""",
                              APP_JS))
        self.assertTrue(used, "app.js 一个批量 id 都没用到？那这段代码是死的")
        for i in sorted(used):
            self.assertIn('id="%s"' % i, INDEX_HTML,
                          "app.js 引用了 index.html 里没有的 id：%s（点了没反应）" % i)

    def test_行首多了勾选列_表头和行都加了(self):
        """少一边就是**列错位** —— 勾会勾到别人那格上（这种错页面上看着只是怪）。"""
        self.assertIn("claimPickHtml", APP_JS)
        self.assertIn("claim-col-pick", APP_JS)
        self.assertIn("const head = ['', '门店'", APP_JS)
        self.assertIn("{ html: claimPickHtml(r), cls: 'claim-col-pick' },", APP_JS)

    def test_可领判据只有一份(self):
        """单条按钮和批量勾**必须**都调 `claimRowUi` —— 两份迟早走散（坑 12），
        走散的表现是「勾得上但点不了」或「能点却勾不上」。"""
        self.assertIn("claimRowUi(r)", _fn(APP_JS, "claimActionsHtml"),
                      "单条按钮没用共用判据")
        self.assertIn("claimRowUi(r)", _fn(APP_JS, "claimPickHtml"),
                      "批量勾没用共用判据")
        self.assertIn("claimRowUi(r)", _fn(APP_JS, "claimPageSelectableKeys"),
                      "「选本页」没用共用判据")

    def test_不可领的行是灰的且写明原因(self):
        body = _fn(APP_JS, "claimPickHtml")
        self.assertIn("disabled", body, "不能领的行必须 disabled")
        self.assertIn("title=", body, "灰掉要有原因，鼠标停上去得看得见")
        self.assertIn("ui.reason", body)

    def test_选择按status_key存_重画能恢复(self):
        """表格是 innerHTML 重画的 —— 不按 key 存，翻页/刷新就把勾冲没了。"""
        self.assertIn("const _claimSelected = new Set();", APP_JS)
        self.assertIn("_claimSelected.has(k)", _fn(APP_JS, "claimPickHtml"))

    def test_表头和列class一样长(self):
        """少一边就是**整行错位**：勾会点到别人的格上，页面上看着只是"怪"。"""
        seg = APP_JS[APP_JS.index("function renderClaimPendingTable"):]
        head = re.search(r"const head = \[(.*?)\];", seg, re.S).group(1)
        cls = re.search(r"const colCls = \[(.*?)\];", seg, re.S).group(1)
        n_head = len(re.findall(r"'[^']*'", head))
        n_cls = len(re.findall(r"'[^']*'", cls))
        self.assertEqual(n_head, n_cls, "表头列数和列 class 数必须一致")
        self.assertEqual(n_head, 10, "首列勾选 + 原来 9 列 = 10")

    def test_表头nth_child跟着首列加了一格(self):
        """`.claim-col-*` 只挂在 `<td>` 上，`<th>` 是 `table()` 用纯字符串拼的、
        没有 class ⇒ **表头只能靠 nth-child 定位**。首列插了勾选格，
        那 9 条列宽规则必须整体 +1，否则表头比内容错一格（CSS 里的隐性耦合）。"""
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        block = css[css.index("#claim-pending-table table .claim-col-pick"):]
        block = block[:block.index(".claim-st {")]
        pick = block[:block.index(".claim-col-store")]
        self.assertIn("th:nth-child(1)", pick, "首列（勾选）要占 nth-child(1)")
        acts = block[block.index(".claim-col-acts"):]
        self.assertIn("th:nth-child(10)", acts, "操作列现在是第 10 格")
        self.assertIn("width: 34px", pick, "勾选列要定宽，别被内容撑开")


class Test批量执行(unittest.TestCase):
    def test_批量走的是单条提交端点(self):
        """**不许为批量开第二条写路径**（坑 18）：范围校验、86 码拦截、
        无链接拦截、成功后自动标已领，全在单条端点里 —— 绕过就是权限洞。"""
        self.assertIn("/api/claim/submit", APP_JS)
        self.assertNotIn("/api/claim/batch", APP_JS)
        self.assertNotIn('path == "/api/claim/batch"', WEB_PY)

    def test_先确认再提交且防重入(self):
        seg = _fn(APP_JS, "runClaimBatch")
        self.assertLess(seg.index("confirm("), seg.index("/api/claim/submit"),
                        "必须先确认，不能点了就跑（真提交不可撤回）")
        self.assertIn("_claimBatchRunning", seg, "要防重入 —— 连点 = 两轮并发提交")
        self.assertIn("async function runClaimBatch()", APP_JS)

    def test_一条失败不中断_失败的留在勾选里(self):
        seg = _fn(APP_JS, "runClaimBatch")
        self.assertIn("out.fail++", seg, "失败要计数")
        self.assertEqual(seg.count("out.fails.push"), 2,
                         "两条失败分支（响应失败 / 抛异常）都要逐条记原因")
        self.assertIn("CLAIM_BATCH_GAP_MS", seg, "条间要有间隔，别连环炮打网关")
        self.assertIn("领取中 ", seg, "要有进度，否则页面像卡死")
        self.assertEqual(seg.count("_claimSelected.delete(key)"), 2,
                         "只有「成功」「华为已领」两条分支移出勾选 —— 失败的必须留着重试")
        self.assertIn("loadClaimPending()", seg, "跑完要重读，成功的变已领")


class Test两条并行(unittest.TestCase):
    """并发度 = 2（用户 2026-09-29：「改成两条并行吧」）。

    ⚠ 并行的前提是**后端状态文件加了锁** —— `claim-status.json` 是整份读改写，
    两个线程同时写 = 后写的把先写的**整份盖掉**（表现：领成功了却还在待领）。
    """

    def test_并行度写死两条(self):
        self.assertIn("const CLAIM_BATCH_LANES = 2;", APP_JS)

    def test_两条车道真的并发跑(self):
        seg = _fn(APP_JS, "runClaimBatch")
        self.assertIn("claimBatchLanes(todo, CLAIM_BATCH_LANES)", seg)
        self.assertIn("Promise.all", seg, "两条车道要并发，不是串行接力")

    def test_同一个SN不许拆到两条车道(self):
        """同一台机器并发查询/提交会互撞（华为可能回 E05 或互相顶掉）。"""
        fn = _fn(APP_JS, "claimBatchLanes")
        self.assertIn("onlineSn", fn, "要按 SN 分组")
        self.assertIn("i % lanes", fn, "组轮流分车道")
        self.assertIn(".push(r)", fn, "组内两行必须留在同一条车道")

    def test_失败清单按原顺序排回去(self):
        self.assertIn("out.fails.sort", _fn(APP_JS, "runClaimBatch"),
                      "两条车道回来是乱序的，得按勾选顺序排")


class Test状态写入加锁(unittest.TestCase):
    """两条并行 = 两个线程写同一份 `out/claim-status.json`（**整份读改写**）。"""

    SRC = (ROOT / "src" / "features" / "tools" / "claim" / "pending"
           / "status.py").read_text(encoding="utf-8")

    def test_锁包住了读改写这一段(self):
        self.assertIn("_LOCK", self.SRC)
        i = self.SRC.index("def set_status")
        body = self.SRC[i:]
        self.assertLess(body.index("with _LOCK:"), body.index("data = load(root)"),
                        "锁必须在 load 之前拿到，否则照样互相盖")

    def test_并发写状态一条都不丢(self):
        """24 个线程同时改**不同的 key** —— 没锁时后写的会把先写的整份覆盖掉，
        少几条是概率性的（正是最难查的那种错）。"""
        import threading
        from src.features.tools.claim.pending import status as st
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            errs = []

            def write(i):
                try:
                    r = st.set_status(root, "k%02d" % i, "claimed", by="t")
                    if not r.get("ok"):
                        errs.append(r)
                except Exception as e:                # noqa: BLE001
                    errs.append(e)

            ths = [threading.Thread(target=write, args=(i,)) for i in range(24)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            self.assertFalse(errs, "有写入失败：%s" % errs[:3])
            data = st.load(root)
            self.assertEqual(len(data), 24,
                             "并发写丢了 %d 条状态 —— set_status 没上锁？"
                             % (24 - len(data)))


class Test双线程同时提交(unittest.TestCase):
    """**两条并行的真实后端路径** —— 12 个线程同时走 `App.claim_submit_online`
    （写 `claim-status.json` 的两条路之一），断言**落盘一条不丢**。

    ⚠ 上面那些字符串断言证明不了"写得进去"，这条才证明：
    * 门禁放行、状态合并、文件写**全是真的**，只把**华为网关** mock 掉（不外网）；
    * `fake_claim` 里那个 `sleep` 是故意的 —— 把并发窗口撑开，没锁时必然互相盖。
    """

    def setUp(self):
        from src import web as web_mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛永旺东部店"\nstore_code: "CNSCN162188"\n',
            encoding="utf-8")
        with mock.patch.object(web_mod.service, "find_running", lambda *a, **k: None):
            self.app = web_mod.App(self.root, "config/store-X.yaml")
        self.app.server = None

    def test_12个线程同时提交_状态一条不丢(self):
        from src.features.tools.claim import submit as claim_submit
        # 两道门禁要扫待领清单（要库/网络）—— 本测试只盯"并行写"，放行
        self.app._claim_sn_allowed = lambda sn: True
        self.app._claim_key_allowed = lambda key: True

        def fake_claim(sn, privilege_codes=None):
            time.sleep(0.01)                     # 撑开并发窗口（没锁必丢）
            return {"ok": True, "code": "200", "sn": sn, "claimed": 1,
                    "popup": "恭喜，您已成功领取权益"}

        results = {}
        with mock.patch.object(claim_submit, "claim", fake_claim):
            def worker(i):
                results[i] = self.app.claim_submit_online(
                    "SN%08d" % i, "", "key%02d" % i)
            ths = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()

        self.assertEqual(len(results), 12, "有线程没跑完")
        bad = [r for r in results.values() if not r.get("ok")]
        self.assertFalse(bad, "有提交失败：%s" % bad[:2])
        f = self.root / "out" / "claim-status.json"
        self.assertTrue(f.is_file(), "状态文件根本没写出来")
        data = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 12,
                         "12 个线程同时写只落盘 %d 条 —— 少的 %d 条就是被并行盖掉的"
                         % (len(data), 12 - len(data)))
        self.assertTrue(all(v.get("status") == "claimed" for v in data.values()),
                        "有状态不是 claimed：%s"
                        % [k for k, v in data.items() if v.get("status") != "claimed"])

    def test_两个线程写同一个key_不损坏(self):
        """同 key 撞车必须是**串行的覆盖**，不是半截文件/丢字段。"""
        from src.features.tools.claim.pending import status as st
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def w(i):
                st.set_status(root, "same",
                              "claimed" if i else "pending",
                              by="t%d" % i, note="n%d" % i)

            ths = [threading.Thread(target=w, args=(i,)) for i in range(2)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            data = st.load(root)
            self.assertEqual(list(data), ["same"], "同 key 只该有一条")
            self.assertIn(data["same"]["status"], ("claimed", "pending"))
            self.assertTrue(data["same"]["by"], "字段不能被写丢")


if __name__ == "__main__":
    unittest.main()
