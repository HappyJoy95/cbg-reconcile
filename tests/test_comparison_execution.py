"""报量查询的注册步骤直接执行比较业务模块，不依赖 CLI 派发。"""

from types import SimpleNamespace
import ast
import inspect

from src.features import registry


def test_registered_pools_step_generates_history_in_its_runtime_root(tmp_path):
    from src import pools_history

    (tmp_path / "out").mkdir()
    config = tmp_path / "config" / "store-test.yaml"
    config.parent.mkdir()
    config.write_text("store_code: SCN-TEST\n", encoding="utf-8")
    ctx = SimpleNamespace(
        root=tmp_path,
        config="config/store-test.yaml",
        args=SimpleNamespace(fetch=[], no_mail=True, no_push=True, quiet=True),
        emit=lambda _message: None,
    )

    step = registry.step_by_cmd("pools")
    assert step is not None and step.run is not None
    assert step.run(ctx) == 0

    history = pools_history.days(tmp_path)
    assert len(history) == 1
    assert history[0]["AD"] == 0 and history[0]["BC"] == 0
    assert list((tmp_path / "out").glob("双平台数据对比-*.xlsx"))


def test_cli_pools_analysis_adapter_delegates_without_running_fetch(monkeypatch):
    from src import cli
    from src.features.compliance.comparison import execution

    seen = {}

    def fake_run(ctx):
        seen["ctx"] = ctx
        return 6

    monkeypatch.setattr(execution, "run", fake_run)
    result = cli.cmd_pools(SimpleNamespace(config="config/store-test.yaml", fetch=[]))

    assert result == 6
    assert seen["ctx"].root == cli.ROOT
    assert seen["ctx"].config == "config/store-test.yaml"


def test_comparison_execution_has_no_cli_import_dependency():
    from src.features.compliance.comparison import execution

    tree = ast.parse(inspect.getsource(execution))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    assert not any(name == "src.cli" or name.endswith(".cli") for name in imported)
