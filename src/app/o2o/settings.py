"""O2O 设置存储 —— `config/o2o/` 下三件套的读写（机器自己的，不进包）。

4.0.0 M-A3（开发目标「十一」）：

| 文件 | 内容 | 稀疏性 |
|---|---|---|
| `config/o2o/settings.yaml` | 全局：`store_name`（本店仓，快照分桶用） | 键值 |
| `config/o2o/mapping-<平台>.csv` | 映射表：itemid/sku_id/title/pro_id/status | 全量（导入产物） |
| `config/o2o/overrides-<平台>.csv` | 库存源覆盖：sku_id/source/value | ⭐ **稀疏** —— 只存手动行，切回云商 = 删行 |

规矩：

* 写盘一律 **tmp + os.replace**（半截文件比没文件好不了多少）；
* 读失败**显式抛**（`O2oSettingsError`），页面把 why 打出来 —— 不吞；
* CSV 表头固定，缺列 = 报错不猜（列错位会把 title 当 pro_id 用，静默错一片）；
* 路径只在本模块拼（root → config/o2o），别处别自己拼。
"""

from __future__ import annotations

import csv
import io
import os
import pathlib
from typing import Dict, List

import yaml

#: 映射表列（固定顺序）—— 2026-09-24 页面要显示云商名称+规格 ⇒ 两列**静态属性**
#: 进映射表（导入时从云商商品档案 / 全量规格表 join 出来，跟库存无关、不会过期）
MAPPING_FIELDS = ("itemid", "sku_id", "title", "cloud_name", "spec", "pro_id", "status")
#: 覆盖表列
OVERRIDE_FIELDS = ("sku_id", "source", "value")
#: 支持的平台（mapping-<平台>.csv 白名单；4.1 接入新平台时加一行）
#: 2026-09-26 京东按核对表 v1 接入（用户：「京东先按照这版进来」——
#: 直连 513 带 pro_id，待核/缺映射行 pro_id 为空 → 页面按缺映射闸住，核完回填重导）
PLATFORMS = ("tmall", "jd")

SUBDIR = "o2o"


class O2oSettingsError(RuntimeError):
    """设置文件缺失/损坏 —— 页面显式展示，不静默。"""


def _dir(root) -> pathlib.Path:
    return pathlib.Path(root) / "config" / SUBDIR


def _read_yaml(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(io.open(path, encoding="utf-8").read())
    except Exception as e:                                    # noqa: BLE001
        raise O2oSettingsError("读 %s 失败：%s" % (path.name, e))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise O2oSettingsError("%s 格式不对（应为键值）" % path.name)
    return data


def _write_yaml(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True,
                                  sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_csv(path: pathlib.Path, fields) -> List[dict]:
    if not path.exists():
        return []
    try:
        rows = list(csv.DictReader(io.open(path, encoding="utf-8-sig")))
    except Exception as e:                                    # noqa: BLE001
        raise O2oSettingsError("读 %s 失败：%s" % (path.name, e))
    if rows:
        head = tuple(rows[0].keys())
        if head != tuple(fields):
            raise O2oSettingsError("%s 表头不对：期望 %s，实际 %s"
                                   % (path.name, list(fields), list(head)))
    return rows


def _write_csv(path: pathlib.Path, rows: List[dict], fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)


def _check_platform(platform: str) -> str:
    p = str(platform or "").strip()
    if p not in PLATFORMS:
        raise O2oSettingsError("未知平台 %r（可选：%s）" % (platform, "/".join(PLATFORMS)))
    return p


# ------------------------------------------------------------------ settings

def load_settings(root) -> dict:
    """全局设置：`{"store_name": "…"}`（缺文件 = 全默认空）。"""
    d = _read_yaml(_dir(root) / "settings.yaml")
    return {"store_name": str(d.get("store_name") or "").strip()}


def save_settings(root, store_name: str) -> None:
    """只存已知键（别把整包 dict 原样写回去 —— 脏键会世代相传）。"""
    _write_yaml(_dir(root) / "settings.yaml",
                {"store_name": str(store_name or "").strip()})


# ------------------------------------------------------------------- mapping

def mapping_path(root, platform: str) -> pathlib.Path:
    return _dir(root) / ("mapping-%s.csv" % _check_platform(platform))


def load_mapping(root, platform: str) -> List[dict]:
    """映射表全量行。空文件 = 没导入过（页面提示先跑导入脚本），不算错。"""
    return _read_csv(mapping_path(root, platform), MAPPING_FIELDS)


def save_mapping(root, platform: str, rows: List[dict]) -> None:
    _write_csv(mapping_path(root, platform), rows, MAPPING_FIELDS)


# ----------------------------------------------------------------- overrides

def overrides_path(root, platform: str) -> pathlib.Path:
    return _dir(root) / ("overrides-%s.csv" % _check_platform(platform))


def load_overrides(root, platform: str) -> Dict[str, dict]:
    """`{sku_id: {"source","value"}}` —— 稀疏表全读成 dict。"""
    rows = _read_csv(overrides_path(root, platform), OVERRIDE_FIELDS)
    out: Dict[str, dict] = {}
    for r in rows:
        sku = str(r.get("sku_id") or "").strip()
        if not sku:
            continue
        out[sku] = {"source": str(r.get("source") or "").strip(),
                    "value": r.get("value")}
    return out


def save_overrides(root, platform: str, overrides: Dict[str, dict]) -> None:
    """写回稀疏表。**空值/非 manual 的条目直接不落**（cloud = 不存在于文件）。

    ⚠ 值原样存字符串（'12'），合法性交给 `source.decide` 判 ——
    这里不判，免得两处口径漂（invalid_manual 的显式报错只有一处）。
    """
    rows = []
    for sku in sorted(overrides):
        ov = overrides[sku] or {}
        src = str(ov.get("source") or "").strip()
        if src != "manual":
            continue                      # cloud → 从文件消失（稀疏）
        val = ov.get("value")
        rows.append({"sku_id": sku, "source": src,
                     "value": "" if val is None else val})
    _write_csv(overrides_path(root, platform), rows, OVERRIDE_FIELDS)
