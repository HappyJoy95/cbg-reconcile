"""兼容旧导入路径；实现位于 :mod:`src.integrations.pmall`。"""
import sys

from .integrations import pmall as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "pmall", _implementation)
