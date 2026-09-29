# -*- coding: utf-8 -*-
"""权益赠送**活动目录** —— 配置优先，内置 9 月那份兜底（零业务 IO）。

配置：`config/benefit-claim.yaml`（用户改这份换月）。
"""

from __future__ import annotations

import datetime
from typing import List, Optional

from .....config_io import load_raw
from .....paths import ROOT

#: 内置 9 月服务产品权益赠送活动（源：用户图「9月服务产品权益赠送活动汇总」）。
#: ⚠ 改活动优先改 `config/benefit-claim.yaml`；这份只是**缺配置时的兜底**。
DEFAULT_ACTIVITIES: List[dict] = [
    {
        "id": "care-fold-202609",
        "privilege_codes": ["8813043801", "8813043798", "8813043795", "8813045289", "8813045286", "8813043804"],
        "category": "手机",
        "title": "金秋礼遇限时赠送Care+（X6/X7/XTs/Pura X Max/Pura X系列）",
        "match": ["Mate X6", "Mate X7", "Mate XTs", "Pura X Max", "Pura X"],
        # ⚠ `Pura X` 子串会误伤 **Pura X View / VDE**（不是本折叠屏活动机型）——
        #   2026-09-23 用户：「Pura X View 不是这个系列，筛选要排除一下」。
        # View 机型码 VOL-AL00 / VDE-… —— 子串 `Pura X` 会误伤，一律排除
        "exclude": [
            "Pura X View", "Pura X VDE", "Pura X VDE-",
            "VOL-AL00", "VDE-AL00", "Pura X View ",
        ],
        "start": "2026-09-01",
        "end": "2026-09-30",
        "benefit": "HUAWEI Care+（一年期）",
        "content": "Mate X6/X7、Pura X系列：内外屏各1次屏幕保障；Mate XTs：1次屏幕保障；"
                   "Pura X Max：2次意外损坏保障+1年电池焕新服务",
        "value": "至高2699元",
        "fee": "①X6/X7系列：内屏468元，外屏268元 ②XTs：屏幕468元 "
               "③Pura X Max：内屏/主板468元 外屏/后壳/摄像头168元 "
               "④Pura X：内屏/主板268元 外屏/后壳/摄像头168元 其余免费",
        "url": "https://consumer.huawei.com/cn/support/receive-huawei-care/",
        "after_claim": "支持办理延长服务宝；不支持办理Care+",
        "details": "HUAWEI Care+（一年期）①X6/X7系列：内外屏各1次碎屏保障 "
                   "②XTs：1次碎屏保障 ③Pura X Max：2次意外保障+1年电池焕新 "
                   "④Pura X：内外屏各1次碎屏保障",
    },
    {
        "id": "nova16-gift-202609",
        "privilege_codes": ["8813044900", "8813044893", "8813044886"],
        "category": "手机",
        "title": "nova 16 / Pro / Ultra 无忧大礼包",
        "match": ["nova 16 Ultra", "nova 16 Pro", "nova 16"],
        # ⚠ 「nova 16」是 **`nova 16 SE` 的前缀**，而匹配是**纯子串**（大小写不敏感）
        #   ⇒ 同一台 SE 会同时挂两条活动，nova16 那行去华为一查就是
        #   「该 SN 下没有匹配到可领权益」—— **假待领**。
        #   2026-09-29 实测炸出来：**100 行**、批量领取第一条就撞上。
        #   `exclude` 先于 match（同 FreeBuds 7 / Pura X 那两处）⇒ 挡掉；
        #   SE 自己有 `nova16se-gift-202609`（码 8813045771）。
        "exclude": ["nova 16 SE"],
        "start": "2026-08-01",
        "end": "2026-10-07",
        "benefit": "nova无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 进水保障；1年1次 后摄保障；"
                   "1年1次 后盖保障；2年 电池保障；2年 官方质保",
        "value": "至高1295元（nova 16价值985元 / Pro 1165元 / Ultra 1295元）",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/nova16-care/",
        "after_claim": "支持办理HUAWEI Care+（一年期）（注意！主推「无忧大礼包」，"
                       "仅被动响应用户的办理Care+需求。办理后权益独立存在互不影响。）"
                       "不支持办理HUAWEI Care+（两年期）及延长服务宝",
        "details": "1年屏幕保障：有效期内，设备因意外碰撞、跌落、挤压等原因"
                   "造成屏幕破碎或者开裂时，可免费更换一次原装屏幕组件。"
                   "权益自领取之日起生效，有效期一年。"
                   "*1年 进水保障 / 后摄保障 / 后盖保障 / 2年电池保障 见活动细则",
    },
    {
        "id": "nova16se-gift-202609",
        "privilege_codes": ["8813045771"],
        "category": "手机",
        "title": "nova 16 SE 无忧大礼包",
        "match": ["nova 16 SE"],
        "start": "2026-09-01",  # 首销日按当月首销近似；可改 yaml
        "end": "2026-10-07",
        "benefit": "nova无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 进水保障；1年1次 后摄保障；"
                   "1年1次 后盖保障；2年 电池保障；2年 官方质保",
        "value": "价值985元",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/nova16se-care/",
        "after_claim": "支持办理HUAWEI Care+（一年期）（主推无忧大礼包）"
                       "不支持办理HUAWEI Care+（两年期）及延长服务宝",
        "details": "同 nova 无忧大礼包保障项；权益自领取之日起生效",
    },
    {
        "id": "enjoy90pm-gift-202609",
        "privilege_codes": ["8813043775"],
        "category": "手机",
        "title": "畅享90 Pro Max 畅享无忧大礼包",
        "match": ["畅享90 Pro Max"],
        "start": "2026-09-01",
        "end": "2026-10-07",
        "benefit": "畅享无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 进水保障；2年1次 后摄保障；"
                   "2年1次 后盖保障；2年 电池保障；2年 官方质保",
        "value": "价值938元",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/changxiang90series/",
        "after_claim": "支持办理HUAWEI Care+（一年期）（主推无忧大礼包）"
                       "不支持办理HUAWEI Care+（两年期）及延长服务宝",
        "details": "同畅享无忧大礼包保障项",
    },
    {
        "id": "matepad-air-2026",
        "privilege_codes": ["8813045220", "8813045215", "8813045210", "8813045205", "8813045200"],
        "category": "平板",
        "title": "MatePad Air 12英寸鸿蒙焕新版 无忧大礼包",
        "match": ["MatePad Air 12"],
        # ⚠ 同款**前缀碰撞**（nova 16 / SE 那次的同构问题）：「MatePad Air 12」
        #   是 `MatePad Air 12英寸（第三代）` 的前缀，第三代有自己的一条活动。
        #   2026-09-29 扫出来的**结构隐患**（当前库里还没这种机型的成交，先堵上）。
        "exclude": ["MatePad Air 12英寸（第三代）"],
        "start": "2026-07-08",
        "end": "2026-12-31",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 后摄保障；2年1次 电池保障",
        "value": "至高856元（Air价值856元）",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/tablets-care/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "1年屏幕保障 / 后壳保障 / 后摄保障 / 2年电池保障",
    },
    {
        # 用户 2026-09-23：MatePad Air 12 第三代 **独立领取页**（不是 tablets-care）
        "id": "matepad-air-gen3-2026",
        "privilege_codes": ["8813047641"],
        "category": "平板",
        "title": "MatePad Air 12英寸（第三代）无忧大礼包",
        "match": ["MatePad Air 2025", "MatePad Air 12英寸（第三代）", "第三代） MatePad Air"],
        "start": "2026-01-01",
        "end": "2026-10-11",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 后摄保障；2年1次 电池保障",
        "value": "以官网活动页为准",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/worry-free-care-package/matepad-air/benefit-claim/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "HUAWEI MatePad Air 12英寸（第三代）无忧大礼包限时领取（至 2026-10-11 24点）",
    },
    {
        "id": "matepad-115s-2026",
        "privilege_codes": ["8813045220", "8813045215", "8813045210", "8813045205", "8813045200"],
        "category": "平板",
        "title": "MatePad 11.5 S 鸿蒙焕新版 无忧大礼包",
        "match": ["MatePad 11.5 S"],
        "start": "2026-07-08",
        "end": "2026-12-31",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 后摄保障；2年1次 电池保障",
        "value": "至高856元（11.5 S价值726元）",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/tablets-care/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "同无忧大礼包保障项",
    },
    {
        "id": "matepad-115-2026",
        "privilege_codes": ["8813045220", "8813045215", "8813045210", "8813045205", "8813045200"],
        "category": "平板",
        "title": "MatePad 11.5 鸿蒙焕新版 / SE 焕新版 无忧大礼包",
        "match": ["MatePad 11.5 鸿蒙焕新版", "MatePad SE 焕新版", "MatePad 11.5"],
        # ⚠ 同款**前缀碰撞**：「MatePad 11.5」是 `MatePad 11.5 S` 的前缀。
        #   2026-09-29 扫出来的结构隐患 —— 库里现在的名字是 `MatePad 11.5s 2025款`
        #   （小写且**没空格**，匹配不上「MatePad 11.5 S」）所以还没炸，先堵上；
        #   exclude 是大小写不敏感的子串 ⇒ 也**不会**误伤 `11.5s` 那批。
        "exclude": ["MatePad 11.5 S"],
        "start": "2026-07-27",
        "end": "2026-12-31",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 后摄保障；2年1次 电池保障",
        "value": "至高686元（11.5价值686元 / SE价值562元）",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/tablets-care/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "同无忧大礼包保障项",
    },
    {
        "id": "matepad-mini-2026",
        "privilege_codes": ["8813045220", "8813045215", "8813045210", "8813045205", "8813045200"],
        "category": "平板",
        "title": "MatePad mini 鸿蒙焕新版 无忧大礼包",
        "match": ["MatePad mini"],
        "start": "2026-08-10",
        "end": "2026-12-31",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 后摄保障；2年1次 电池保障",
        "value": "896元",
        "fee": "免费",
        "url": "https://consumer.huawei.com/cn/support/tablets-care/",
        "after_claim": "Care+（一年期）Care+（两年期）延长服务宝",
        "details": "同无忧大礼包保障项",
    },
    {
        "id": "matepad-pro12-2026",
        "category": "平板",
        "title": "MatePad Pro 12 无忧大礼包",
        "match": ["MatePad Pro 12"],
        "start": "2026-09-01",  # 首销日-9.13
        "end": "2026-09-13",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 摄像头保障；2年1次 电池保障",
        "value": "986元",
        "fee": "①屏幕268元 ②后壳/后摄/电池免费",
        "url": "https://consumer.huawei.com/cn/support/matepad-pro-care/",
        "after_claim": "Care+（一年期）Care+（两年期）延长服务宝",
        "details": "同无忧大礼包保障项",
    },
    {
        "id": "wooki-air12-2026",
        "category": "平板",
        "title": "Wooki（MatePad Air 12下一代）无忧大礼包",
        "match": ["Wooki"],
        "start": "2026-09-01",
        "end": "2026-10-31",  # 首销日-10.XX 按月底近似
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 摄像头保障；2年1次 电池保障",
        "value": "936元",
        "fee": "①屏幕268元 ②后壳/后摄/电池免费",
        "url": "输出中",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "同无忧大礼包保障项",
    },
    {
        "id": "matebook-pro-2026",
        "category": "PC",
        "title": "Matebook Pro (Harden R) / Matebook Pro S 无忧大礼包",
        "match": ["Matebook Pro", "MateBook Pro"],
        "start": "2026-08-14",
        "end": "2026-09-13",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 充电器五折换新；2年1次 电池保障；"
                   "2年2次 键帽换新；2年2次 拆机清洁",
        "value": "1506元",
        "fee": "①屏幕268元 ②充电器备件RRP五折 ③电池/键帽换新/拆机清洁免费",
        "url": "https://consumer.huawei.com/cn/support/matebook-pro-s-care/",
        "after_claim": "Care+（三年期）延长服务宝",
        "details": "同PC无忧大礼包保障项",
    },
    {
        "id": "matebook-fold-2026",
        "category": "PC",
        "title": "HUAWEI MateBook Fold 非凡大师 非凡尊享服务",
        "match": ["MateBook Fold"],
        "start": None,  # 全生命周期随设备捆绑
        "end": None,
        "benefit": "非凡尊享服务",
        "content": "2年1次 屏幕惠换；2年2次 上门服务；2年4次 深度清洁；"
                   "2年4次 屏幕保养；门店礼遇",
        "value": "/",
        "fee": "①使用屏幕惠换权益，更换屏幕按全新屏五折收费 "
               "②深度清洁/上门服务/屏幕保养/门店礼遇免费",
        "url": "不涉及",
        "after_claim": "Care+（三年期）延长服务宝",
        "details": "非凡尊享服务权益",
    },
    {
        "id": "freearc-lost-202609",
        "privilege_codes": ["8813045784"],
        "category": "音频",
        "title": "FreeArc 丢失无忧",
        "match": ["FreeArc"],
        "start": "2026-08-01",
        "end": "2026-09-30",
        "benefit": "丢失无忧",
        "content": "1年1次丢失无忧",
        "value": "68元",
        "fee": "单只耳机丢失时，可享一次单只耳机备件建议零售价的5折优惠购买",
        "url": "https://consumer.huawei.com/cn/support/freearc/",
        "after_claim": "Care+（一年期）Care+（两年期）",
        "details": "一年一次丢失无忧服务：服务有效期内，单只耳机丢失可5折购备件",
    },
    {
        "id": "freeclip2-lost-202609",
        "privilege_codes": ["8813045785"],
        "category": "音频",
        "title": "FreeClip 2 典藏版 丢失无忧",
        # ⚠ 必须带「典藏版」—— 只写 FreeClip 2 会把普通版/所有颜色全吸进来
        "match": ["FreeClip 2 典藏版"],
        "start": "2026-08-01",
        "end": "2026-09-30",
        "benefit": "丢失无忧",
        "content": "1年1次丢失无忧",
        "value": "88元",
        "fee": "单只耳机丢失时，可享一次单只耳机备件建议零售价的5折优惠购买",
        "url": "https://consumer.huawei.com/cn/support/freeclip2s/",
        "after_claim": "Care+（一年期）",
        "details": "一年一次丢失无忧服务",
    },
    {
        "id": "freebuds7-lost-202609",
        "privilege_codes": ["8813046626"],
        "category": "音频",
        "title": "FreeBuds 7 丢失无忧",
        "match": ["FreeBuds 7"],
        # ⚠ 子串 `FreeBuds 7` 会吸进 **FreeBuds 7i**（T0025）——
        #   2026-09-26 用户：「7i 会混在 FreeBuds 7 里面，这个权益应该只有 buds7」。
        #   和 `Pura X` 吸 View 是同一类坑，一样用 exclude 挡（先于 match 判）。
        #   ⚠ 写 `FreeBuds 7i` 而不是 `7i`：后者会误伤 FreeBuds 6i。
        "exclude": ["FreeBuds 7i"],
        "start": "2026-08-24",
        "end": "2026-10-07",
        "benefit": "丢失无忧",
        "content": "1年1次丢失无忧",
        "value": "88元",
        "fee": "单只耳机丢失时，可享一次单只耳机备件建议零售价的5折优惠购买",
        "url": "https://consumer.huawei.com/cn/support/freebuds7/",
        "after_claim": "Care+（一年期）",
        "details": "一年一次丢失无忧服务",
    },
    {
        "id": "watch-ultimate2-202609",
        "category": "穿戴",
        "title": "WATCH Ultimate 2 峻岭灰/雪域 无忧大礼包",
        "match": ["WATCH Ultimate 2", "Watch Ultimate 2"],
        "start": "2026-09-17",  # 领取入口开放
        "end": "2026-09-30",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 表冠保障；"
                   "1年1次 充电底座五折换新；2年1次 电池保障",
        "value": "1668元",
        "fee": "①更换屏幕、后壳、表冠268元/部件 "
               "②更换充电底座备件RRP五折 ③更换电池、表冠免费",
        "url": "输出中",
        "after_claim": "Care+（两年期）延长服务宝",
        "details": "1、1年1次 屏幕保障：服务有效期内，设备在正常使用过程中"
                   "因意外碰撞、跌落、挤压等情况造成屏幕破碎或者开裂时可享受一次",
    },
    {
        "id": "watch6-202609",
        "privilege_codes": ["8813047456", "8813047468", "8813047462", "8813047455"],
        "category": "穿戴",
        "title": "WATCH 6 系列 无忧大礼包",
        "match": ["WATCH 6", "Watch 6"],
        "start": "2026-09-07",
        "end": "2026-10-31",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 表冠保障；"
                   "1年1次 充电底座五折换新；2年1次 电池保障",
        "value": "至高668元（数字系列568元 / Pro 668元）",
        "fee": "①更换屏幕、后壳68元/部件 ②更换充电底座备件RRP五折 ③更换电池、表冠免费",
        "url": "https://consumer.huawei.com/cn/support/worry-free-care-package/watch6/benefit-claim/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "同穿戴无忧大礼包保障项",
    },
    {
        "id": "watch-gt7-202609",
        "privilege_codes": ["8813045839", "8813045833", "8813045827",
                            "8813045821", "8813045815", "8813045809"],
        "category": "穿戴",
        "title": "HUAWEI WATCH GT 7 系列 无忧大礼包",
        # ⚠ 库里商品名是 `WATCH GT7`（**无空格**）—— 两种写法都要 match
        "match": ["WATCH GT 7", "Watch GT 7", "WATCH GT7", "Watch GT7",
                  "GT7 Pro", "GT 7 Pro"],
        "start": "2026-08-05",
        "end": "2026-09-30",
        "benefit": "无忧大礼包",
        "content": "1年1次 屏幕保障；1年1次 后壳保障；1年1次 表冠保障；"
                   "1年1次 充电底座五折换新；2年1次 电池保障",
        "value": "至高588元（数字系列468元 / Pro 588元）",
        "fee": "①屏幕、后壳、表冠68元 ②充电底座备件RRP五折 ③电池免费",
        "url": "https://consumer.huawei.com/cn/support/watch-gt-7/",
        "after_claim": "Care+（一年期）延长服务宝",
        "details": "同穿戴无忧大礼包保障项",
    },
]

def load_activities(root=None) -> List[dict]:
    """活动列表：`config/benefit-claim.yaml` 的 `activities`，缺则用内置。"""
    from pathlib import Path
    root = Path(root or ROOT)
    over = load_raw(root / "config" / "benefit-claim.yaml") or {}
    acts = (over or {}).get("activities")
    if isinstance(acts, list) and acts:
        return [a for a in acts if isinstance(a, dict) and a.get("id")]
    return list(DEFAULT_ACTIVITIES)


def activity_by_id(aid: str, root=None) -> Optional[dict]:
    for a in load_activities(root):
        if str(a.get("id") or "") == str(aid):
            return a
    return None


def _parse_day(s) -> Optional[datetime.date]:
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def in_window(act: dict, day) -> bool:
    """`day`（date 或 ISO 字符串）是否落在活动赠送期内（含首尾）。

    `start`/`end` 为空 = 不限日期（全生命周期那类）。
    """
    if isinstance(day, str):
        day = _parse_day(day)
    if day is None:
        return False
    start = _parse_day((act or {}).get("start"))
    end = _parse_day((act or {}).get("end"))
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def matches_model(act: dict, product_name: str) -> bool:
    """商品名是否命中活动的 `match` 子串列表（大小写不敏感）。

    ``exclude`` 里任一子串命中则**直接不认**（先于 match）——
    例如折叠屏 Care+ 要排除 ``Pura X View``。
    """
    name = (product_name or "").lower()
    for ex in (act or {}).get("exclude") or []:
        needle = str(ex or "").strip().lower()
        if needle and needle in name:
            return False
    for m in (act or {}).get("match") or []:
        needle = str(m or "").strip().lower()
        if needle and needle in name:
            return True
    return False


def matches_model_any(activities, product_name: str) -> bool:
    """任一活动的机型子串命中（退货旁注用，**不看日期**）。"""
    return any(matches_model(a, product_name) for a in activities or ())
