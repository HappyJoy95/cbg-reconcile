"""4.0.0 O2O 天猫插件 —— contract / matcher / generator 的单测。

测试名写"为什么这么设计"：每条红/坑对应一条钉子（开发目标「三·五」的三个坑 +
契约的两个坑），谁改阈值先看这些名字。
"""

import pathlib

import pytest

from src.app.o2o import contract, generator, matcher


class Test契约:
    def test_列头和列序与官方模板逐字一致(self):
        # 列序错了 = 平台按错列解析（A/C 对调会把 skuId 当 itemid）
        assert contract.HEADERS == [
            "商品itemid(必填)",
            "门店id(必填)",
            "商品SkuId(有SKU商品必填)",
            "商家货品编码",
            "sku 名称（非必填)",
            "库存数（导入后库存数更新",
        ]
        assert contract.SHEET_NAME == "门店商品库存"

    def test_导出文件的科学计数法id必须按E记法转整数(self):
        # 实测：平台导出 xlsx 原始 XML 就是 E 记法，直接当字符串必错
        assert contract.excel_id("1.030667544278E12") == "1030667544278"
        assert contract.excel_id("6.207058295085E12") == "6207058295085"
        assert contract.excel_id(1167502199.0) == "1167502199"
        assert contract.excel_id(1167502199) == "1167502199"
        assert contract.excel_id("1167502199") == "1167502199"

    def test_超出float精度的id宁可报错也不猜(self):
        # 猜错 id = 库存传到别人的链接（红线：歧义不猜）
        with pytest.raises(ValueError):
            contract.excel_id("1.030667544278123456789E19")
        with pytest.raises(ValueError):
            contract.excel_id("")
        with pytest.raises(ValueError):
            contract.excel_id(None)

    def test_行格式与整表校验(self):
        row = contract.build_row("1.030667544278E12", "1.167502199E9",
                                 "6.207058295085E12", "SPCG80003578",
                                 "陶瓷白", 3)
        assert row == ["1030667544278", "1167502199", "6207058295085",
                       "SPCG80003578", "陶瓷白", 3]
        assert contract.validate_rows([row]) is None
        # 库存负数当场拒（平台语义是"设绝对值"，负数只会被拒或写坏）
        with pytest.raises(ValueError):
            contract.build_row("1", "2", "3", None, None, -1)
        # 必填缺失 fail fast，别把格式错误留给平台回
        with pytest.raises(ValueError):
            contract.build_row("1", "2", "", None, None, 0)

    def test_校验抓坏行(self):
        assert contract.validate_rows([["1", "2", "abc", "", "", 0]]) is not None
        assert contract.validate_rows([["1", "2", "3", "", "", -5]]) is not None
        assert contract.validate_rows([["1", "2", "3"]]) is not None


class Test匹配器:
    def _index(self):
        return matcher.CloudIndex([
            ("9056591", "智能手机/华为/畅享90 Pro Max/黑"),
            ("9056592", "智能手机/华为/畅享90 Pro Max/白"),
            ("9219321", "智能手机/华为/畅享70X/蓝"),
            ("7459159", "配件/华为/真无线蓝牙耳机/-"),
            ("9413114", "配件/华为/FreeBuds Pro 5无线耳机/-"),
        ])

    def test_唯一强命中判auto且同型号多行归并(self):
        # 同型号 2 个 ProId（颜色拆行）必须归并成一个 model_key，否则误进待核
        r = matcher.match_item(self._index(), "1034774918045",
                               "【新品】华为畅享 90 Pro Max 8500mAh华为巨鲸大电池鸿蒙AI流畅丝滑")
        assert r.status == "auto"
        assert sorted(r.pro_ids) == ["9056591", "9056592"]
        assert r.model_key  # 型号段有值（括号/尾巴已剥）

    def test_泛型号黑名单不许auto(self):
        # 实测撞过：云商「真无线蓝牙耳机」同时接住 FreeBuds7 / 7i —— 匹错=红线
        r = matcher.match_item(self._index(), "1079973207013",
                               "【新品】HUAWEI FreeBuds7悦彰耳机 蓝牙耳机无线半入耳主动降噪")
        assert r.status == "review"
        assert any(p == "7459159" for p, _ in r.candidates)  # 泛行还在候选里给人核

    def test_无候选判miss(self):
        r = matcher.match_item(self._index(), "x", "完全不相干的标题 面条 键盘")
        assert r.status == "miss"

    def test_低分或无分差判review_歧义不猜(self):
        # 分数贴着阈值/第二名咬得紧 → review（宁可人工看一眼）
        r = matcher.match_item(self._index(), "y", "畅享")
        assert r.status in ("review", "miss")
        assert r.status != "auto"

    def test_型号key不在标题里不许auto_第4道闸(self):
        # 真数据实测：手环10 → 云商「4g通话」s=4/2 差点击 auto —— 分数够≠对得上号
        idx = matcher.CloudIndex([("555", "智能穿戴/华为/4g通话手表版/-")])
        title = "4g 通话 手表 表版 心率监测 铝合金机身"
        toks = matcher.title_tokens(title) & idx.inv.keys() if False else None  # (仅占位)
        r = matcher.match_item(idx, "i1", title)
        # 旧闸（分数/分差/同型号）全过的情况下，第 4 道闸按住
        assert r.status == "review"
        assert r.model_key not in matcher.normalize(title) or r.status != "auto"

    def test_结果不受哈希随机化影响_两进程必须一致(self):
        # 真数据实测：倒排集合遍历 + most_common 同分换序 ⇒ 8 连跑出 6/4 两个结果
        import os
        import subprocess
        import sys as _sys
        root = str(pathlib.Path(__file__).resolve().parents[1])
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from src.app.o2o import matcher;"
            "idx = matcher.CloudIndex(["
            "('1','智能手机/华为/畅享90 Pro Max/黑'),"
            "('2','智能手机/华为/畅享90 Pro Max/白'),"
            "('3','智能手机/华为/畅享70X/蓝')]);"
            "r = matcher.match_item(idx, 'i1', '华为畅享90 Pro Max 旗舰巨鲸电池');"
            "print(r.status, sorted(r.pro_ids), r.score, r.second)" % root
        )
        outs = set()
        for seed in ("0", "42"):
            p = subprocess.run(
                [_sys.executable, "-c", code],
                capture_output=True, text=True,
                env=dict(os.environ, PYTHONHASHSEED=seed))
            assert p.returncode == 0, p.stderr
            outs.add(p.stdout.strip())
        assert len(outs) == 1, "不同 hash seed 结果不一致: %r" % (outs,)

    def test_占位脏编码把auto降级为review(self):
        # 实测：HUAWEI WATCH Ultimate 2 编码 = 111111111111 —— 脏码商品人工确认
        assert matcher.code_is_placeholder("111111111111")
        assert matcher.code_is_placeholder("")
        assert matcher.code_is_placeholder(None)
        assert not matcher.code_is_placeholder("SPCG80002846GB")
        idx = matcher.CloudIndex([("1", "智能手机/华为/畅享90 Pro Max/黑")])
        res = matcher.match_all(idx, [
            ("i1", "华为畅享90 Pro Max 旗舰巨鲸电池", "111111111111"),
            ("i2", "华为畅享90 Pro Max 旗舰巨鲸电池", "SPCG123"),
        ])
        assert res["i1"].status == "review"   # 脏码降级
        assert res["i2"].status == "auto"

    def test_型号段归一化剥括号规格和渠道尾巴(self):
        # 同型号多行（颜色/渠道拆行）必须归并 —— 原型第 3 轮教训
        a = matcher.model_key("平板电脑/华为/MatePad Air Z AST-W10(12GB+256GB)云晰柔光屏-海岛蓝")
        b = matcher.model_key("平板电脑/华为/MatePad Air Z AST-W10(8GB+128GB)云晰柔光屏-珊瑚橙")
        assert a == b
        assert "(" not in a and "海岛蓝" not in a

    def test_停用词不参与打分(self):
        assert "华为" in matcher.STOPWORDS
        toks = matcher.title_tokens("华为官方旗舰店新品手机")
        assert "华为" not in toks and "旗舰店" not in toks


class Test生成器:
    def test_桥表_库存_门店id_拼成六列表(self):
        items = [
            ("1030667544278", "6.207058295085E12", "陶瓷白 限定", "SPCG1"),
            ("1030667544278", "6.207058295086E12", "摩登黑", "SPCG2"),
        ]
        bridge = {"6207058295085": "91000001", "6207058295086": "91000002"}
        stock = {"91000001": 5, "91000002": 0}
        rows, issues = generator.generate_rows(items, bridge, stock, "1.167502199E9")
        assert issues == []
        assert len(rows) == 2
        assert rows[0] == ["1030667544278", "1167502199", "6207058295085",
                           "91000001", "陶瓷白 限定", 5]
        assert rows[1][5] == 0              # 0 是合法库存（有数据=0）
        assert contract.validate_rows(rows) is None

    def test_没映射的sku不进表_进issues(self):
        # 漏传=停在旧值可补；传错=卖错货回不来 —— 宁漏勿错
        items = [("1", "11", "标题", "C1"), ("1", "12", "标题", "C2")]
        rows, issues = generator.generate_rows(items, {"11": "9001"}, {"9001": 2}, "2")
        assert len(rows) == 1
        assert [i[0] for i in issues] == ["unmapped_sku"]
        assert issues[0][1] == "12"

    def test_云商没有库存数据的编码不猜零(self):
        # 猜 0 = 平台下架；云商侧缺数据要人看（可能是编码录错）
        rows, issues = generator.generate_rows(
            [("1", "11", "t", "c")], {"11": "9001"}, {}, "2")
        assert rows == []
        assert issues[0][0] == "no_cloud_stock"

    def test_同sku重复只收第一条(self):
        items = [("1", "11", "a", "c"), ("2", "11", "b", "c")]
        rows, issues = generator.generate_rows(items, {"11": "9001"},
                                               {"9001": 1}, "2")
        assert len(rows) == 1
        assert issues[0][0] == "dup_sku"

    def test_摘要把issues归类计数(self):
        s = generator.summarize([("unmapped_sku", "1", ""), ("unmapped_sku", "2", ""),
                                 ("no_cloud_stock", "3", "")])
        assert "unmapped_sku×2" in s and "no_cloud_stock×1" in s
        assert generator.summarize([]) == "无"
