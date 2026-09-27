"""无忧权益快照：失败保留旧结果，成功以完整文件替换。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.features.valueadd.benefit import compute


class TestBenefitSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        out = self.root / "out"
        out.mkdir()
        self.snapshot = out / "benefit.json"
        self.snapshot.write_text('{"old":true}', encoding="utf-8")

    def test_计算失败保留上一份快照(self):
        with mock.patch.object(compute, "load", return_value={"ok": False, "why": "缺数据"}):
            result = compute.run(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), '{"old":true}')

    def test_替换文件失败时旧快照仍完整(self):
        fresh = {"ok": True, "stores": [{"store": "甲店"}], "regions": [], "people": []}
        with mock.patch.object(compute, "load", return_value=fresh), \
             mock.patch.object(Path, "replace", side_effect=OSError("模拟替换失败")):
            result = compute.run(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), '{"old":true}')
        self.assertFalse((self.root / "out" / "benefit.json.tmp").exists())
