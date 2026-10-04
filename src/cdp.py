"""兼容旧导入路径；实现位于 :mod:`src.integrations.cdp`。"""
import sys

from .integrations import cdp as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "cdp", _implementation)
