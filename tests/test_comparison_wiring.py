"""报量查询前端归属与页面生命周期回归。"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def test_comparison_page_has_its_own_registered_lifecycle():
    page = WEB / "features" / "compliance" / "comparison" / "page.js"
    assert page.is_file(), "报量查询页面逻辑应归入自己的业务目录"
    source = page.read_text(encoding="utf-8")
    assert "registerPage('pools'" in source
    assert "mountComparisonPage" in source
    assert "unmountComparisonPage" in source
    assert "signal: controller.signal" in source


def test_comparison_page_script_loads_before_app_and_loader_leaves_app():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    tag = re.search(r'<script src="(/features/compliance/comparison/page\.js\?v=\d+)"></script>', html)
    assert tag, "index.html 应明确加载报量查询页面脚本"
    assert html.index(tag.group(0)) < html.index('<script src="/app.js?v=')
    assert "async function renderPoolsHistory" not in app
    assert "async function openPoolsDay" not in app
    assert "pools: () => loadOverview()" not in app


def test_global_overview_does_not_fetch_comparison_history():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "renderPoolsHistory(" not in app
