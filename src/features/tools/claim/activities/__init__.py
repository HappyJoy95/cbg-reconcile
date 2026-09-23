# -*- coding: utf-8 -*-
"""活动一览 —— 展示 `config/benefit-claim.yaml` 里的赠送活动。"""

from __future__ import annotations

from ....registry import Sub

from . import catalog  # noqa: F401

SUB = Sub(key="claim-activities", label="活动一览", order=10)
