"""兼容旧导入路径；实现位于 :mod:`src.integrations.cbg`。"""
import sys

from .integrations import cbg as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "cbg", _implementation)
