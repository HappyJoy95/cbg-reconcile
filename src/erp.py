"""兼容旧导入路径；实现位于 :mod:`src.integrations.erp`。"""
import sys

from .integrations import erp as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "erp", _implementation)
