"""兼容旧导入路径；实现位于 :mod:`src.integrations.erp_stub`。"""
import sys

from .integrations import erp_stub as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "erp_stub", _implementation)
